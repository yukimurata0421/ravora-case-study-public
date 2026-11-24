"""Aggregate public broadcast observations inside Dell's read-only transaction."""
from collections import defaultdict
from datetime import datetime
import json
import sqlite3

from ravora.ray_public_contract import (
    API_COUNTS, CHAT_RANK_COVERAGE, DAILY, MAX_BROADCASTS, PROVIDERS, SCHEMA, SLOTS, WEEKLY_SHOWS,
)
from ravora.studio_report import JST, legacy_sessions, report, timing
from ravora.twitch_schedule import DURATION, SAMPLE_PERIOD, next_slot, slot_at

PUBLIC_PLATFORMS = ('twitch', 'youtube')


def _sum(rows, key):
    values = [r[key] for r in rows if r[key] is not None]
    return sum(values) if values else None


def _api(usage):
    if usage is None:
        return None
    buckets = defaultdict(list)
    for operation in usage['by_operation']:
        name = operation['provider']
        buckets[name if name in PROVIDERS else 'other'].append(operation)
    groups = {}
    for name in PROVIDERS:
        ops = buckets[name]
        group = {key:sum(op.get(key, 0) for op in ops) for key in (*API_COUNTS, 'unpriced_calls', 'tokens_reported_calls')}
        for key in ('input_tokens', 'output_tokens', 'characters', 'estimated_usd_known'):
            values = [op[key] for op in ops if op.get(key) is not None]
            group[key] = sum(values) if values else None
        groups[name] = group
    return {**{key:sum(group[key] for group in groups.values()) for key in API_COUNTS}, 'by_provider':groups}



def _timing(c, key, *, gap=False):
    if c is None:
        return None
    timing = c[key]
    fields = ['p50_ms', 'p95_ms', 'sample_p95_ms', 'n', 'missing']
    if gap:
        fields += ['idle_excluded', 'first_in_session', 'previous_end_unknown', 'no_program_signal']
    result = {field:timing[field] for field in fields}
    if gap:
        result['missing'] += timing['previous_end_unknown'] + timing['no_program_signal']
    return result


def _broadcast(b):
    sessions = b['sessions']
    start = b['broadcast_start_jst'] or (sessions[0]['started_jst'] if sessions else None)
    if not start or any(s['identity_conflict'] for s in sessions):
        return None
    when = datetime.fromisoformat(start)
    usage, aud, chat = b['api'], b['audience'], b['chat_coverage']
    observed = chat['status'] in ('complete', 'partial')
    c = b['comments'] if observed else None
    counts = c or {}
    versions = {e['policy'] for e in b['speech_policy_events']}
    if any(not any(e['session']==row['id'] and e['first_frame']==0 for e in b['speech_policy_events']) for row in sessions):
        versions.add('unrecorded')
    version = 'mixed' if len(versions)>1 else next(iter(versions),'unrecorded')
    modes = {s['prefetch'] for s in sessions}
    prefetch = {frozenset(): 'unknown', frozenset({True}): 'prefetch',
                frozenset({False}): 'serial'}.get(frozenset(modes), 'mixed')
    seconds = b['broadcast_seconds'] if b['broadcast_seconds'] is not None else b['program_seconds']
    minutes_source = 'summary' if b['broadcast_seconds'] is not None else ('program' if seconds is not None else None)
    if seconds is None and b['broadcast_start_jst'] and b['last_confirmed_at'] is not None:
        seconds = max(0, b['last_confirmed_at'] - when.timestamp())
        minutes_source = 'last_live'
    cost = usage['estimated_usd'] if usage else None
    peak_source = 'summary' if aud['reported_peak'] is not None else ('samples' if aud['sample_count'] else None)
    average_source = 'summary' if aud['reported_average'] is not None else ('samples' if aud['sample_count'] else None)
    return {
        'platform': b['platform'], 'slot': 'off-plan', 'cohort': None, 'market': None, 'started_jst': when.isoformat(timespec='minutes'),
        'speech_policy':version if version in ('short-turn-v1','mixed') else 'unrecorded',
        'response_sli':({k:c['response_sli'][k] for k in ('eligible','started','within_3s','within_5s','budget_blocked')}
                        if c is not None else None),
        'audience_health': b['audience_health'],
        'state': b['state'], 'state_source': b['state_source'], 'live_until': b['live_until'],
        'minutes_source':minutes_source, 'last_confirmed_at':b['last_confirmed_at'],
        'latest_viewers':aud['latest_viewers'], 'last_audience_at':aud['last_audience_at'],
        'minutes': round(seconds / 60, 2) if seconds is not None else None,
        'unique_viewers': aud['unique_viewers'], 'average_source':average_source,
        'average_viewers':aud['reported_average'] if average_source == 'summary' else aud['sample_average'], 'viewer_samples': aud['sample_count'], 'peak_source': peak_source,
        'peak_viewers': aud['reported_peak'] if peak_source == 'summary' else aud['sample_peak'],
        'speakers': counts.get('speakers'), 'comments': counts.get('received'), 'replied': counts.get('played'),
        'selected':counts.get('selected'), 'prefetched':counts.get('prefetched'),
        'prefetched_ready':counts.get('prefetched_audio_ready'), 'prefetched_discarded':counts.get('prefetched_discarded'),
        'audio_ready':_timing(c, 'received_to_audio_ready'),
        'unplayed': counts.get('not_played'), 'budget_stopped': counts.get('budget_blocked'),
        'spoke_again': counts.get('returning_after_reply'), 'chat': chat, 'prefetch': prefetch,
        'gap': _timing(c, 'ray_gap_waiting_comment', gap=True), 'reply': _timing(c, 'received_to_program'),
        'player': _timing(c, 'event_to_player'), 'api': _api(usage),
        'cost_usd': round(cost, 5) if cost is not None else None,
        'cost_usd_known':usage['estimated_usd_known'] if usage is not None else None,
        'unpriced_calls':usage['unpriced_calls'] if usage is not None else None,
        'cost_partial': cost is None and usage is not None and usage['estimated_usd_known'] is not None,
    }


def _people(db, membership):
    # Select the first observed group before aggregating either finalized or provisional cohorts.
    first, own_reply, again = {}, {}, set()
    days, ended_days = defaultdict(set), defaultdict(set)
    has_kind = any(col[1]=='reply_kind' for col in db.execute('PRAGMA table_info(studio_inputs)'))
    individual_played = "CASE WHEN i.reply_kind='group' THEN NULL ELSE i.played_at END" if has_kind else 'i.played_at'
    events = db.execute(f"""SELECT i.session,i.viewer,i.received,{individual_played} FROM studio_inputs i
        JOIN studio_sessions s ON s.session=i.session
        WHERE s.platform IN ('twitch','youtube') AND i.source=s.platform AND i.test=0
          AND COALESCE(i.intake_reason,'')!='startup_history' ORDER BY i.received,i.receipt""")
    for session, viewer, received, played in events:
        group = membership.get(session)
        if group is None:
            continue
        day = datetime.fromtimestamp(received, JST).date()
        days[viewer].add(day)
        if group[1] == 'ended':
            ended_days[viewer].add(day)
        if first.setdefault(viewer, group) != group:
            continue
        if received > own_reply.get(viewer, float('inf')):
            again.add(viewer)
        if played is not None:
            own_reply[viewer] = min(played, own_reply.get(viewer, played))
    by_group = defaultdict(lambda: dict(first_seen=0, replied=0, spoke_again=0))
    for viewer, group in first.items():
        counts = by_group[group]
        counts['first_seen'] += 1
        counts['replied'] += viewer in own_reply
        counts['spoke_again'] += viewer in again
    def returning(accounts):
        return {'speakers':len(accounts), 'on_two_or_more_days':sum(len(d) >= 2 for d in accounts.values())}
    return by_group, returning(days), returning(ended_days)


def _cohort(rows):
    groups = [r['cohort'] for r in rows if r['cohort'] is not None]
    return {key:sum(g[key] for g in groups) for key in ('first_seen','replied','spoke_again')} if groups else None


def _pooled_gap(rows, samples):
    observed = [r for r in rows if r['gap'] is not None]
    if not observed:
        return None
    pool = timing(list(samples))
    for key in ('missing','idle_excluded','first_in_session','previous_end_unknown','no_program_signal'):
        pool[key] = sum(r['gap'][key] for r in observed)
    return {key:pool[key] for key in observed[0]['gap']}


def _activity(rows, samples):
    return {'broadcasts':len(rows), 'live':sum(r['state']=='live' for r in rows),
            'last_seen':sum(r['state']=='unknown' for r in rows),
            'chat_broadcasts':sum(r['comments'] is not None for r in rows), 'cohort':_cohort(rows),
            'gap':_pooled_gap(rows, samples),
            **{key:_sum(rows,key) for key in ('speakers','comments','replied','spoke_again')}}


def _totals(rows, repeat):
    apis = [r['api'] for r in rows if r['api'] is not None]
    minutes = _sum(rows,'minutes')
    return {'broadcasts':len(rows), 'minutes':round(minutes,2) if minutes is not None else None,
            'unique_viewers':_sum(rows,'unique_viewers'),'viewer_summaries':sum(r['unique_viewers'] is not None for r in rows),
            'chat_broadcasts':sum(r['comments'] is not None for r in rows), 'api_broadcasts':len(apis),
            'cohort':_cohort(rows), 'repeat_viewers':repeat if any(r['comments'] is not None for r in rows) else None,
            **{k:_sum(rows,k) for k in ('speakers','comments','replied','budget_stopped','spoke_again')},
            'api':{k:sum(a[k] for a in apis) for k in API_COUNTS} if apis else None,
            'cost_usd_known':_sum(rows,'cost_usd_known'), 'unpriced_calls':_sum(rows,'unpriced_calls'),
            'cost_usd':round(sum(r['cost_usd'] for r in rows),6) if rows and all(r['cost_usd'] is not None for r in rows) else None}


def _market(windows, now):
    """Only consecutive, independently observed endpoints with stable identities cover an interval."""
    expected = complete = partial = failed = channels = viewers = top5 = 0
    covered = overlap = 0.0
    for start, samples, stream in windows:
        expected += max(0, min(DURATION // SAMPLE_PERIOD, int((now-start)//SAMPLE_PERIOD) + 1))
        previous = None
        for row in samples:
            if 0 <= row['tick'] < DURATION // SAMPLE_PERIOD:
                if row['status'] == 'complete':
                    complete += 1
                    channels += row['channels']
                    viewers += row['viewers']
                    top5 += row['top5_viewers']
                elif row['status'] == 'partial':
                    partial += 1
                else:
                    failed += 1
            stamp = row['peers_at']
            if (previous is not None and stream and row['ray_stream'] == previous['ray_stream'] == stream
                    and row['rival_stream'] == previous['rival_stream'] and previous['tick'] + 1 == row['tick']
                    and previous['peers_at'] is not None and stamp is not None
                    and 0 < stamp - previous['peers_at'] <= SAMPLE_PERIOD + 90):
                interval = max(0, min(start+DURATION, stamp) - max(start, previous['peers_at']))
                covered += interval
                if row['rival_stream'] is not None:
                    overlap += interval
            previous = row
    return {'expected_samples':expected, 'complete_samples':complete, 'partial_samples':partial, 'failed_samples':failed,
            'avg_channels':round(channels/complete,2) if complete else None,
            'avg_viewers':round(viewers/complete,2) if complete else None,
            'top5_share_pct':round(100*top5/viewers,2) if viewers else None,
            'paired_ray_minutes':round(covered/60,2) if covered else None,
            'overlap_minutes':round(overlap/60,2) if covered else None}


def _slot_totals(slot, rows, windows, runs, gaps, now):
    name, hour, days = slot
    observed = [r for r in rows if r['slot'] == name]
    mine = [r for r in observed if r['state'] == 'ended']
    eligible = [r for r in mine if r['chat']['eligible'] and r['chat']['observed_seconds'] >= DURATION * CHAT_RANK_COVERAGE]
    cohort = _cohort(eligible)
    outcomes = {'claimed':len(runs), 'blocked':sum(status == 'blocked' for _,status in runs),
                'failed':sum(status == 'failed' for _,status in runs), 'stopped_early':sum(status == 'stopped_early' for _,status in runs),
                'unresolved':sum(status in ('interrupted','unconfirmed') or (status == 'claimed' and now >= start+DURATION+120) for start,status in runs)}
    return {'id':name, 'activity':_activity(observed, gaps), 'runs':outcomes, 'language':name[:2], 'start':f'{hour:02d}:00', 'end':f'{hour+2:02d}:00',
            'days':'daily' if days == DAILY else 'tue-thu-sat-sun', 'broadcasts':len(mine), 'eligible':len(eligible),
            'eligible_minutes':round(sum(r['chat']['observed_seconds'] for r in eligible)/60, 2),
            'speakers':_sum(eligible,'speakers'), 'spoke_again':_sum(eligible,'spoke_again'), 'cohort':cohort,
            'unique_viewers':_sum(mine,'unique_viewers'), 'viewer_summaries':sum(r['unique_viewers'] is not None for r in mine),
            'market':_market(windows, now)}


def project(db, now):
    db.row_factory = sqlite3.Row
    version = db.execute('PRAGMA user_version').fetchone()[0]
    ready = version >= 20
    bindings, samples, stream_ids = {}, defaultdict(list), {}
    attempts = defaultdict(list)
    if version >= 22:
        for run in db.execute('SELECT scheduled_start,arm,session,result FROM twitch_schedule_runs'):
            start, arm, session, status = run
            slot = slot_at(start)
            if slot and slot.start == start and slot.arm == arm:
                attempts[arm].append((start,status))
                samples.setdefault(start, [])
                if session:
                    bindings[session] = slot
        for row in db.execute('SELECT * FROM twitch_schedule_market ORDER BY scheduled_start,tick'):
            samples[row['scheduled_start']].append(row)
    rows, membership, gaps = [], {}, defaultdict(list)
    registered = []
    if ready:
        for b in report(db, now=now, include_legacy=False, include_gap_samples=True)['broadcasts']:
            registered.extend(s['id'] for s in b['sessions'])
            if b['platform'] not in PUBLIC_PLATFORMS:
                continue
            row = _broadcast(b)
            if row is None:
                continue
            sessions = [s['id'] for s in b['sessions']]
            group = (len(rows), row['state'])
            confirmed = row['state']=='ended' or row['last_confirmed_at'] is not None
            if confirmed:
                membership.update((session, group) for session in sessions)
            bound = {bindings.get(session) for session in sessions}
            if b['platform'] == 'twitch' and len(bound) == 1 and None not in bound:
                slot = bound.pop()
                row['slot'] = slot.arm
                stream_ids[slot.start] = b['stream_id']
                row['market'] = _market([(slot.start, samples[slot.start], b['stream_id'])], now)
            if confirmed and row['gap'] is not None:
                gaps[row['slot']].extend(b['gap_samples_ms'])
            rows.append(row)
    done = [r for r in rows if r['state']=='ended']
    observed = [r for r in rows if r['state']=='ended' or r['last_confirmed_at'] is not None]
    by_group, repeat, ended_repeat = _people(db,membership) if membership else ({},None,None)
    for index, row in enumerate(rows):
        if row['comments'] is not None and (row['state']=='ended' or row['last_confirmed_at'] is not None):
            row['cohort'] = by_group.get((index,row['state']), {'first_seen':0,'replied':0,'spoke_again':0})
    legacy = {}
    if ready:
        for item in legacy_sessions(db,json.dumps(registered),include_usage=False):
            platform = item['platform']
            counts = legacy.setdefault(platform,dict(platform=platform,sessions=0,retained_comments=0,viewer_turns=0,played_flags=0))
            counts['sessions'] += 1
            counts['retained_comments'] += item['retained_comments']
            counts['viewer_turns'] += item['recorded_viewer_turns']
            counts['played_flags'] += item['retained_played_flags']
    windows = defaultdict(list)
    for start, ticks in samples.items():
        slot = slot_at(start)
        if slot and slot.start == start:
            windows[slot.arm].append((start, ticks, stream_ids.get(start)))
    rows.sort(key=lambda r:r['started_jst'], reverse=True)
    upcoming = next_slot(now)
    return {
        'schema':SCHEMA, 'generated_at':now, 'pipeline':{'observations_ready':ready},
        'experiment':{'weekly_broadcasts':WEEKLY_SHOWS, 'weekly_hours':WEEKLY_SHOWS*2,
                      'next_jst':upcoming.start_jst, 'next_slot':upcoming.arm, 'min_chat_fraction':CHAT_RANK_COVERAGE},
        'totals':_totals(done,ended_repeat), 'legacy':[legacy[k] for k in sorted(legacy)],
        'observed':{**_totals(observed, repeat), 'live':sum(r['state']=='live' for r in observed),
                    'last_seen':sum(r['state']=='unknown' for r in observed), 'unknown':len(rows)-len(observed),
                    'gap':_pooled_gap(observed, (v for values in gaps.values() for v in values))},
        'slots':[_slot_totals(slot, observed, windows[slot[0]], attempts[slot[0]], gaps[slot[0]], now) for slot in SLOTS],
        'broadcasts':rows[:MAX_BROADCASTS],
    }


def dumps(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
