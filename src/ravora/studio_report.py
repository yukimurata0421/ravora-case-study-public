"""Produce one aggregate per identified broadcast from a consistent SQLite snapshot."""
from bisect import bisect_right
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from itertools import chain
import json
import sqlite3
import time

from ravora.comments import VIEWER_CHAT_SOURCES
from ravora.voice_evaluation import percentile
from ravora.ray_public_contract import CHAT_RANK_COVERAGE, P95_MIN_SAMPLES


JST = timezone(timedelta(hours=9))
SESSION_SET = '(SELECT value FROM json_each(?))'
BUDGET_CODES = {'LOCAL_MONTHLY_AUX_LIMIT','LOCAL_MONTHLY_REPLY_LLM_LIMIT','LOCAL_MONTHLY_TTS_LIMIT','STT_SESSION_LIMIT','STT_MONTHLY_AUDIO_LIMIT'}
FINAL_UNPLAYED = {'cancelled','expired','removed','failed','held','dropped'}


def jst(value):
    return datetime.fromtimestamp(value, JST).isoformat(timespec='milliseconds') if value is not None else None


def elapsed(start, end):
    return (end-start)*1000 if start is not None and end is not None and end >= start else None


def timing(values):
    present = [n for n in values if n is not None and n >= 0]
    return {'n':len(present), 'missing':len(values)-len(present),
            'p50_ms':percentile(present,.5), 'p95_ms':percentile(present,.95) if len(present)>=P95_MIN_SAMPLES else None,
            'sample_p95_ms':percentile(present,.95),
            'max_ms':round(max(present),3) if present else None}


def timeline(db, sessions):
    return [dict(r) for r in db.execute(f"""
        SELECT i.*, run.sample_rate, t.created_at AS turn_started,
               (SELECT MIN(u.created_at) FROM utterances u WHERE u.turn_id=i.turn_id) AS wav_ready
        FROM studio_inputs i JOIN studio_sessions run ON run.session=i.session LEFT JOIN conversation_turns t ON t.turn_id=i.turn_id
        WHERE i.session IN {SESSION_SET} ORDER BY i.received,i.receipt""", (sessions,))]


def first_ready(row):
    return min((x for x in (row['ready_at'],row['wav_ready']) if x is not None), default=None)


def conversation(rows, tests, *, gap_samples=None):
    viewers = [r for r in rows if r['source'] in VIEWER_CHAT_SOURCES and bool(r['test']) == tests
               and r['intake_reason'] != 'startup_history']
    played = [r for r in viewers if r['played_at'] is not None]
    selected = [r for r in viewers if r['selected']]
    returning = set()
    first_reply = {}
    direct = [r for r in played if r.get('reply_kind') != 'group' and r['status']!='removed']
    eligible_comments = [r for r in viewers if r['intake_reason'] not in
                         ('future_timestamp','comment_too_old') and r['status']!='removed']
    eligible = {r['viewer'] for r in eligible_comments}
    starts = [r for r in eligible_comments if r['first_sample'] is not None and r['first_written'] is not None]
    delays = [elapsed(r['received'],r['first_written']) for r in starts]
    denominator = len(eligible_comments)
    counts = {'started':len(starts),
              'within_3s':sum(d is not None and d<=3000 for d in delays),
              'within_5s':sum(d is not None and d<=5000 for d in delays),
              'budget_blocked':sum(r['reason'] in BUDGET_CODES and r['first_sample'] is None for r in eligible_comments)}
    response_sli = {'eligible':denominator, 'unobserved_start':denominator-len(starts),
                    **counts, 'rates':{k:v/denominator if denominator else None for k,v in counts.items()}}
    reached = {r['viewer'] for r in direct} & eligible
    for r in direct:
        who = r['viewer']
        first_reply[who] = min(first_reply.get(who, r['played_at']), r['played_at'])
    for r in viewers:
        if r['received'] > first_reply.get(r['viewer'], float('inf')):
            returning.add(r['viewer'])

    gaps = []
    idle = first = unknown = 0
    by_turn = {r['turn_id']: r for r in rows if r['turn_id']}
    for r in viewers:
        if r['first_sample'] is None:
            continue
        prior = by_turn.get(r['previous_turn'])
        if r['previous_turn'] is None:
            first += 1
        elif (prior is None or prior['session'] != r['session']
              or prior['last_written'] is None or prior['end_sample'] is None):
            unknown += 1
        elif r['received'] > prior['last_written']:
            idle += 1
        else:
            gaps.append((r['first_sample']-prior['end_sample'])*1000/r['sample_rate']
                        if r['first_sample'] >= prior['end_sample'] else None)
    if gap_samples is not None:
        gap_samples.extend(g for g in gaps if g is not None)
    prefetched = [r for r in viewers if r['prefetched']]
    return {'received':len(viewers), 'speakers':len({r['viewer'] for r in viewers}), 'selected':len(selected),
            'response_sli':response_sli,
            'selection':{'eligible_speakers':len(eligible),'individually_replied_speakers':len(reached),
                         'individual_reply_rate':len(reached)/len(eligible) if eligible else None,
                         'group_replies':sum(r.get('reply_kind')=='group' for r in played),
                         'group_members_covered':sum(r['status']=='covered' for r in viewers),
                         'lanes':dict(Counter(r['selection_lane'] for r in selected if r.get('selection_lane'))),
                         'reasons':dict(Counter(r['reason'] for r in viewers if r['reason']))},
            'played':len(played), 'not_played':len(viewers)-len(played), 'returning_after_reply':len(returning),
            'budget_blocked':sum(r['reason'] in BUDGET_CODES for r in viewers),
            'outcomes':dict(Counter(r['status'] for r in viewers)),
            'prefetched':len(prefetched), 'prefetched_audio_ready':sum(first_ready(r) is not None for r in prefetched),
            'prefetched_discarded_after_ready':sum(r['status'] in FINAL_UNPLAYED and r['played_at'] is None
                                                  and first_ready(r) is not None for r in prefetched), 'prefetched_discarded':sum(
                r['status'] in FINAL_UNPLAYED and r['played_at'] is None for r in prefetched),
            'received_to_program':timing([elapsed(r['received'],r['first_written']) for r in viewers]),
            'received_to_audio_ready':timing([elapsed(r['received'],first_ready(r)) for r in selected]),
            'event_to_player':timing([elapsed(r['published'],r['player_at']) for r in viewers]),
            'ray_gap_waiting_comment':{**timing(gaps), 'within_500ms':sum(g is not None and 0<=g<=500 for g in gaps),
                                      'idle_excluded':idle, 'first_in_session':first,
                                      'previous_end_unknown':unknown,
                                      'no_program_signal':sum(r['first_sample'] is None for r in viewers)}}


def estimate(row):
    if row['cost_estimate'] is not None:
        return row['cost_estimate']
    model = row['model'] or ''
    # Standard text/voice list prices, before free tiers, 2026-11-24. Rates and units in the runbook.
    if row['provider']=='tts' and row['characters'] is not None:
        if '-Chirp3-HD-' in model:
            return row['characters'] * 30 / 1e6
        if '-Wavenet-' in model:
            return row['characters'] * 4 / 1e6
    if row['provider']=='jev' and model=='jev-1.14.0' and row['input_tokens'] is not None:
        return row['input_tokens']*.042/1e6
    if row['provider']=='gemini' and model=='gemini-3.5-flash-lite':
        inputs, cached, total = row['input_tokens'],row['cached_input_tokens'],row['total_tokens']
        # Memory extraction reports no cached count; price it as uncached input, an upper bound.
        if cached is None and inputs is not None and total is not None:
            cached = 0
        if None not in (inputs,cached,total) and 0 <= cached <= inputs <= total:
            return ((inputs-cached)*.30 + cached*.03 + (total-inputs)*2.50) / 1e6
    return None


def stt_estimate(row):
    if row['cost_estimate'] is not None:
        return row['cost_estimate']
    if row['billed'] is not None:
        return row['billed'] * .016 / 60
    return None


def usage(db, sessions, *, instrumented=True, windows=None):
    prep_fields = 'i.prefetched, i.played_at, i.status AS outcome' if instrumented else 'NULL AS prefetched, NULL AS played_at, NULL AS outcome'
    observation_join = 'LEFT JOIN studio_inputs i ON i.turn_id=t.turn_id' if instrumented else ''
    rows = db.execute(f"""SELECT a.*,t.usage_session AS studio_session,p.operation,p.code,
            json_extract(p.summary,'$.chunk') AS chunk,
            {prep_fields}
        FROM conversation_turns t JOIN api_usage a ON a.turn_id=t.turn_id
        JOIN provider_calls p ON p.id=a.id {observation_join}
        WHERE t.usage_session IN {SESSION_SET} AND a.mode='cloud'""", (sessions,)).fetchall()
    grouped = defaultdict(list)
    for row in rows:
        if windows and not in_window(windows,row['studio_session'],row['created_at']):
            continue
        grouped[(row['provider'],row['operation'])].append(row)
    result = []
    discarded_costs = []
    for (provider,operation), items in sorted(grouped.items()):
        sent = [r for r in items if r['attempted']]
        costs = [estimate(r) for r in sent]
        discarded = [r for r in sent if r['prefetched'] and r['played_at'] is None and r['outcome'] in FINAL_UNPLAYED]
        discarded_costs.extend(estimate(r) for r in discarded)
        logical = {(r['turn_id'],r['chunk']) for r in sent}
        result.append({'provider':provider, 'operation':operation, 'attempts':len(sent),
                       'retries':len(sent)-len(logical), 'failed':sum(r['status']=='failed' for r in sent),
                       'outcome_unknown':sum(r['status'] in ('outcome_unknown','dispatched','response_received') for r in sent),
                       'blocked_before_send':sum(not r['attempted'] for r in items),
                       'budget_blocked':sum(r['code'] in BUDGET_CODES for r in items),
                       'discarded_prefetch_attempts':len(discarded),
                       **{k:sum(r[k] for r in sent if r[k] is not None) if any(r[k] is not None for r in sent)
                          else None for k in ('input_tokens','output_tokens','total_tokens','characters')},
                       'tokens_reported_calls':sum(r['total_tokens'] is not None for r in sent),
                       'latency':timing([r['latency_ms'] for r in sent]),
                       'estimated_usd_known':round(sum(c for c in costs if c is not None),9) if any(
                           c is not None for c in costs) else None, 'unpriced_calls':costs.count(None)})
    stt = db.execute(f"""SELECT session,created_at,request_attempted,input_state,code,duration_seconds,latency_ms,cost_estimate,
            json_extract(stt_diagnostics,'$.provider_billed_seconds') AS billed
        FROM developer_inputs WHERE session IN {SESSION_SET} AND kind='audio' AND mode='cloud'""",(sessions,)).fetchall()
    if windows:
        stt = [r for r in stt if in_window(windows,r['session'],r['created_at'])]
    if stt:
        sent = [r for r in stt if r['request_attempted']]
        priced = [cost for r in sent if (cost := stt_estimate(r)) is not None]
        result.append({'provider':'stt','operation':'recognize','attempts':len(sent),'retries':0,
                       'failed':sum(r['input_state']=='failed' for r in sent),
                       'outcome_unknown':sum(r['input_state'] in ('outcome_unknown','dispatched') for r in sent),
                       'blocked_before_send':len(stt)-len(sent), 'budget_blocked':sum(r['code'] in BUDGET_CODES for r in stt),
                       'discarded_prefetch_attempts':0, 'submitted_seconds':sum(r['duration_seconds'] or 0 for r in sent),
                       'latency':timing([r['latency_ms'] for r in sent]),
                       'estimated_usd_known':round(sum(priced),9) if priced else None,'unpriced_calls':len(sent)-len(priced)})
    attempts = sum(r['attempts'] for r in result)
    unpriced = sum(r['unpriced_calls'] for r in result)
    known = round(sum(r['estimated_usd_known'] or 0 for r in result),9)
    return {'attempts':attempts, 'discarded_prefetch_attempts':sum(r['discarded_prefetch_attempts'] for r in result),
            'discarded_prefetch_usd_known':round(sum(c for c in discarded_costs if c is not None),9)
                if not discarded_costs or any(c is not None for c in discarded_costs) else None,
            'discarded_prefetch_unpriced_calls':discarded_costs.count(None),
            'estimated_usd':known if not unpriced else None,
            'estimated_usd_known':known if not attempts or unpriced < attempts else None,
            'unpriced_calls':unpriced, 'by_operation':result}


def legacy_sessions(db, registered, *, include_usage=True):
    # Earlier sessions identify a chat/channel and may have already purged their inbox.
    # A session ID is not a provider broadcast ID. Retain this distinction in exports.
    rows = db.execute(f"""WITH legacy AS (
          SELECT t.usage_session AS session,s.platform,t.turn_id,t.played,
                 json_extract(t.metrics,'$.live_program.turn_to_program_signal_ms') AS signal
          FROM conversation_turns t JOIN source_events s ON s.id=t.source_event_id
          WHERE t.usage_session IS NOT NULL AND s.platform IN ('twitch','youtube')
        ), ids AS (
          SELECT session,platform FROM legacy UNION SELECT session,source FROM comment_inbox
          WHERE source IN ('twitch','youtube'))
        SELECT ids.session,ids.platform,COUNT(l.turn_id) AS recorded_viewer_turns,
               COALESCE(SUM(l.played),0) AS retained_played_flags, COUNT(l.signal) AS recorded_program_starts,
               (SELECT COUNT(*) FROM comment_inbox i WHERE i.session=ids.session AND i.source=ids.platform)
                   AS retained_comments
        FROM ids LEFT JOIN legacy l USING(session,platform)
        WHERE ids.session NOT IN {SESSION_SET} GROUP BY ids.session, ids.platform ORDER BY ids.session""",(registered,))
    if include_usage:
        return [{**dict(row),'api':usage(db,json.dumps([row['session']]),instrumented=False)} for row in rows]
    return [dict(row) for row in rows]


# Helix samples once a minute. A fresh document cannot renew a provider observation.
LIVE_EVIDENCE_SECONDS = 150


def stream_rollovers(db, version):
    history = defaultdict(list)
    if version >= 31:
        for session, previous, stream, boundary in db.execute(
                'SELECT session,previous_id,stream_id,stream_started FROM twitch_stream_rollovers ORDER BY stream_started'):
            history[session].append((previous,stream,boundary))
    return history


def in_window(windows, session, stamp):
    lower, upper = windows.get(session,(None,None))
    return (lower is None or lower <= stamp) and (upper is None or stamp < upper)


def broadcast_runs(session, transitions):
    if not transitions or session['identity_conflict']:
        yield dict(session)
        return
    lower = None
    for previous, stream, boundary in transitions:
        yield dict(session, stream_id=previous, started=lower or session['started'], stopped=boundary,
                   frames=None, window=(lower,boundary))
        lower = boundary
    yield dict(session, started=lower, frames=None, window=(lower,None))


def scheduled_audience_costs(db, since, until):
    """Group attempted cloud calls by the last audience evidence at local request creation."""
    if since >= until:
        raise ValueError('INVALID_SLOT_START_RANGE')
    db.row_factory = sqlite3.Row
    arms = {}
    version = db.execute('PRAGMA user_version').fetchone()[0]
    history = stream_rollovers(db,version)
    sessions = 'SELECT scheduled_start,session,1 AS original FROM twitch_schedule_runs'
    if version >= 27:
        sessions += ' UNION ALL SELECT scheduled_start,session,0 FROM twitch_slot_restarts WHERE session IS NOT NULL'
    if version >= 29:
        sessions += ' UNION ALL SELECT scheduled_start,session,0 FROM twitch_slot_fresh_runs WHERE session IS NOT NULL'
    runs = db.execute(f"""WITH sessions AS ({sessions})
        SELECT r.arm,b.session,s.stream_id,s.identity_conflict,b.original
        FROM twitch_schedule_runs r JOIN sessions b USING(scheduled_start)
        LEFT JOIN studio_sessions s ON s.session=b.session
        WHERE r.scheduled_start>=? AND r.scheduled_start<? ORDER BY r.scheduled_start""", (since,until))
    for arm, session, stream, conflict, original in runs:
        if arm not in arms:
            arms[arm] = {'claimed':0, 'with_session':0, 'audience':{
                state:{'samples':0, 'attempts':0, 'initiative_attempts':0, 'unpriced_calls':0, 'cost_usd_known':0.0}
                for state in ('zero','positive','unknown')}}
        totals = arms[arm]
        totals['claimed'] += original
        if session is None:
            continue
        totals['with_session'] += original
        times, states = [], []
        samples = db.execute('SELECT observed,status,stream_id,viewers FROM studio_audience WHERE session=? ORDER BY observed', (session,))
        for stamp, status, observed_stream, viewers in samples:
            state = 'unknown'
            at_sample = next((old for old,_,boundary in history.get(session,()) if stamp < boundary),stream)
            if not conflict and at_sample and observed_stream == at_sample and status == 'live' and viewers is not None:
                state = 'zero' if viewers == 0 else 'positive'
            times.append(stamp)
            states.append(state)
            totals['audience'][state]['samples'] += 1

        calls = db.execute("""SELECT a.*,e.platform AS source FROM conversation_turns t
            JOIN api_usage a ON a.turn_id=t.turn_id LEFT JOIN source_events e ON e.id=t.source_event_id
            WHERE t.usage_session=? AND a.mode='cloud' AND a.attempted=1""", (session,))
        mic = db.execute("""SELECT created_at,cost_estimate,
            json_extract(stt_diagnostics,'$.provider_billed_seconds') AS billed FROM developer_inputs
            WHERE session=? AND kind='audio' AND mode='cloud' AND request_attempted=1""", (session,))
        operations = ((r['created_at'],estimate(r),r['source']=='ray-initiative') for r in calls)
        transcriptions = ((r['created_at'],stt_estimate(r),False) for r in mic)
        for stamp, cost, initiative in chain(operations, transcriptions):
            idx = bisect_right(times,stamp)-1
            state = states[idx] if idx>=0 and stamp-times[idx]<=LIVE_EVIDENCE_SECONDS else 'unknown'
            bucket = totals['audience'][state]
            bucket['attempts'] += 1
            bucket['initiative_attempts'] += initiative
            if cost is None:
                bucket['unpriced_calls'] += 1
            else:
                bucket['cost_usd_known'] += cost
    for totals in arms.values():
        for bucket in totals['audience'].values():
            if bucket['attempts'] and bucket['unpriced_calls'] == bucket['attempts']:
                bucket['cost_usd_known'] = None
            else:
                bucket['cost_usd_known'] = round(bucket['cost_usd_known'],9)
    return {'slot_start_from_jst':jst(since), 'slot_start_until_jst':jst(until),
            'max_sample_age_seconds':LIVE_EVIDENCE_SECONDS, 'slots':dict(sorted(arms.items()))}


def audience_health(rows):
    last_success = None
    consecutive = failures = 0
    for row in rows:
        status = row['status']
        if status in ('live','offline'):
            last_success = row['observed']
        if status == 'http_401':
            consecutive += 1
            failures += 1
        else:
            consecutive = 0
    status = rows[-1]['status'] if rows else 'no_samples'
    return {'last_attempt':rows[-1]['observed'] if rows else None, 'last_success':last_success,
            'status':status if status in ('live','offline','http_401','no_samples') else 'unavailable',
            'consecutive_401':consecutive, 'total_401':failures}


def broadcast_state(runs, identity, summary, audience, events, now):
    if summary:
        return 'ended', 'summary', None, summary['ended']
    if not identity or not runs:
        return 'unknown', 'unconfirmed', None, None
    signals = [(e['observed'], e['status'], 'youtube') for e in events]
    seen_live = False
    for sample in audience:
        if sample['status'] == 'live' and sample['stream_id'] == identity:
            seen_live = True
            signals.append((sample['observed'], 'live', 'helix'))
        elif sample['status'] == 'offline' and seen_live:
            signals.append((sample['observed'], 'ended', 'helix'))
        else:
            signals.append((sample['observed'], 'unknown', 'helix'))
    signals.sort()
    latest_run = runs[-1]
    # A fresh attempt to resume may follow an earlier unclean exit.
    latest = signals[-1] if signals else None
    definite = next((s for s in reversed(signals) if s[1] != 'unknown'), None)
    if definite and definite[1] == 'ended' and definite[0] >= latest_run['started']:
        return 'ended', definite[2], None, definite[0]
    if latest_run['stopped'] is not None and any(sig[1] == 'live' for sig in signals):
        return 'ended', 'studio_stop', None, latest_run['stopped']
    if latest and latest[1] == 'live' and latest[0] >= latest_run['started'] and 0 <= now-latest[0] <= LIVE_EVIDENCE_SECONDS:
        return 'live', latest[2], latest[0] + LIVE_EVIDENCE_SECONDS, None
    return 'unknown', 'unconfirmed', None, None


def chat_coverage(intervals, tracked, start, end, received):
    duration = max(0, end - start) if start is not None and end is not None else None
    seconds = 0.0
    if duration:
        cursor = start
        for row in intervals:
            left, right = max(cursor, row['started']), min(end, row['ended'])
            if right > left:
                seconds += right - left
                cursor = right
    if seconds:
        status = 'complete' if duration - seconds < 0.001 else 'partial'
    elif received:
        status = 'partial'
    else:
        status = 'unobserved' if tracked else 'unknown'
    return {'status': status, 'observed_seconds': round(seconds, 3) if tracked or seconds else None,
            'window_seconds': round(duration, 3) if duration is not None else None,
            'eligible': bool(duration and seconds / duration >= CHAT_RANK_COVERAGE)}


def report(db, *, now=None, include_legacy=True, include_gap_samples=False):
    now = time.time() if now is None else now
    db.row_factory = sqlite3.Row
    version = db.execute('PRAGMA user_version').fetchone()[0]
    if version < 17:
        raise ValueError('STUDIO_USAGE_SESSION_SCHEMA_REQUIRED')
    sessions = list(db.execute('SELECT * FROM studio_sessions ORDER BY started')) if version >= 20 else []
    groups = defaultdict(list)
    history = stream_rollovers(db,version)
    for s in chain.from_iterable(broadcast_runs(row,history.get(row['session'])) for row in sessions):
        key = ('session',s['session']) if not s['stream_id'] or s['identity_conflict'] else ('stream',s['stream_id'])
        groups[(s['platform'], *key)].append(s)
    summaries = {(s['platform'],s['stream_id']):s for s in db.execute('SELECT * FROM studio_stream_summaries')} if version>=20 else {}
    for platform, external in summaries:
        groups.setdefault((platform,'stream',external), [])
    broadcasts = []
    for (platform, binding, identity), runs in groups.items():
        ids = json.dumps([r['session'] for r in runs])
        windows = {r['session']:r['window'] for r in runs if 'window' in r}
        inputs = timeline(db,ids)
        if windows:
            inputs = [r for r in inputs if in_window(windows,r['session'],r['received'])]
        audience = list(db.execute(f'SELECT * FROM studio_audience WHERE session IN {SESSION_SET} ORDER BY observed', (ids,)))
        if windows:
            audience = [r for r in audience if in_window(windows,r['session'],r['observed'])]
        samples = [r for r in audience if r['status']=='live' and binding=='stream' and r['stream_id']==identity]
        policy_events = ([dict(r) for r in db.execute(f"""SELECT session,first_frame,activated,policy
                         FROM studio_speech_policies WHERE session IN {SESSION_SET} ORDER BY activated""",(ids,))]
                         if version>=25 else [])
        if windows:
            inherited = {}
            for event in policy_events:
                lower, _ = windows.get(event['session'],(None,None))
                if lower is not None and event['activated'] < lower:
                    inherited[event['session']] = dict(event,inherited=True)
            policy_events = [*inherited.values(), *(e for e in policy_events
                             if in_window(windows,e['session'],e['activated']))]
        counts = [r['viewers'] for r in samples if r['viewers'] is not None]
        summary = summaries.get((platform,identity)) if binding=='stream' else None
        provider_start = summary['started'] if summary else next(
            (r['stream_started'] for r in audience if binding == 'stream' and r['stream_id'] == identity
             and r['stream_started'] is not None), None)
        external_id = identity if binding == 'stream' else None
        if version >= 21:
            intervals = db.execute(f'SELECT session,started,ended FROM studio_chat_intervals WHERE session IN {SESSION_SET} ORDER BY started', (ids,))
            events = list(db.execute(f'SELECT observed,status FROM studio_provider_events WHERE session IN {SESSION_SET} ORDER BY observed', (ids,)))
        else:
            intervals, events = (), ()
        if windows:
            clipped = []
            for row in intervals:
                lower, upper = windows.get(row['session'],(None,None))
                left = max(row['started'],lower) if lower is not None else row['started']
                right = min(row['ended'],upper) if upper is not None else row['ended']
                if left < right:
                    clipped.append(dict(started=left,ended=right))
            intervals = sorted(clipped,key=lambda r:r['started'])
        tracked = version >= 21 and any(r['chat_tracked'] for r in runs)
        state, evidence, live_until, ended = broadcast_state(runs, external_id, summary, audience, events, now)
        if not summary and runs and runs[-1].get('window',(None,None))[1] is not None:
            state, evidence, live_until, ended = 'ended','helix',None,runs[-1]['window'][1]
        observed_start = provider_start if provider_start is not None else (runs[0]['started'] if runs else None)
        window_end = ended
        if window_end is None and runs:
            window_end = runs[-1]['stopped'] or now
        gap_samples = [] if include_gap_samples else None
        comments = conversation(inputs, False, gap_samples=gap_samples) if runs else None
        confirmations = [r['observed'] for r in samples] + [r['observed'] for r in events if r['status']=='live']
        coverage = chat_coverage(intervals, tracked, observed_start, window_end, comments and comments['received'])
        broadcasts.append({
            **({'gap_samples_ms':gap_samples} if include_gap_samples else {}),
            'last_confirmed_at':max(confirmations, default=None),
            'speech_policy_events':policy_events,
            'audience_health':audience_health(audience) if platform=='twitch' and runs else None,
            'state': state, 'state_source': evidence, 'live_until': live_until, 'chat_coverage': coverage,
            'platform':platform, 'stream_id':identity if binding=='stream' else None,
            'sessions':[{'id':r['session'],**({'window':r['window']} if 'window' in r else {}),'mode':r['mode'],'prefetch':bool(r['prefetch']),
                         'selection_policy':r['selection_policy'] if version>=24 else None,
                         'started_jst':jst(r['started']), 'stopped_jst':jst(r['stopped']),
                         'binding_source':r['binding_source'], 'chat_source':r['chat_source'], 'chat_status_at_stop':r['chat_status_at_stop'],
                         'timing_write_losses':r['timing_write_losses'], 'identity_conflict':bool(r['identity_conflict'])} for r in runs],
            'program_seconds':round(sum(r['frames']/r['fps'] for r in runs),3) if runs and all(
                r['frames'] is not None for r in runs) else None,
            'broadcast_start_jst':jst(provider_start),
            'broadcast_end_jst':jst(summary['ended']) if summary else None,
            'broadcast_seconds':round(summary['ended']-summary['started'],3) if summary else None,
            'audience':{'latest_viewers':samples[-1]['viewers'] if samples else None,
                        'last_audience_at':samples[-1]['observed'] if samples else None, 'unique_viewers':summary['unique_viewers'] if summary else None,
                        'reported_average':summary['average_viewers'] if summary else None,
                        'reported_peak':summary['peak_viewers'] if summary else None,
                        'sample_average':round(sum(counts)/len(counts),3) if counts else None,
                        'sample_peak':max(counts, default=None), 'sample_count':len(counts),
                        'polls':dict(Counter(r['status'] for r in audience))},
            'comments':comments,
            'test_comments':conversation(inputs,True) if runs else None,
            'api':usage(db,ids,windows=windows) if runs else None,
        })
    return {'broadcasts':broadcasts,'legacy_sessions':legacy_sessions(db,json.dumps([s['session'] for s in sessions])) if include_legacy else []}
