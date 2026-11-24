"""Produce one aggregate per identified broadcast from a consistent SQLite snapshot."""
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import json
import sqlite3
import time

from ravora.comments import VIEWER_CHAT_SOURCES
from ravora.voice_evaluation import percentile
from ravora.ray_public_contract import CHAT_RANK_COVERAGE, P95_MIN_SAMPLES


JST = timezone(timedelta(hours=9))
SESSION_SET = '(SELECT value FROM json_each(?))'
BUDGET_CODES = {'LOCAL_MONTHLY_REPLY_LLM_LIMIT','LOCAL_MONTHLY_TTS_LIMIT','STT_SESSION_LIMIT','STT_MONTHLY_AUDIO_LIMIT'}
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
    for r in played:
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
            'ray_gap_waiting_comment':{**timing(gaps), 'idle_excluded':idle, 'first_in_session':first,
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
    if row['provider']=='gemini' and model=='gemini-3.5-flash-lite':
        inputs, cached, total = row['input_tokens'],row['cached_input_tokens'],row['total_tokens']
        if None not in (inputs,cached,total) and 0 <= cached <= inputs <= total:
            return ((inputs-cached)*.30 + cached*.03 + (total-inputs)*2.50) / 1e6
    return None


def usage(db, sessions, *, instrumented=True):
    prep_fields = 'i.prefetched, i.played_at, i.status AS outcome' if instrumented else 'NULL AS prefetched, NULL AS played_at, NULL AS outcome'
    observation_join = 'LEFT JOIN studio_inputs i ON i.turn_id=t.turn_id' if instrumented else ''
    rows = db.execute(f"""SELECT a.*,p.operation,p.code,
            json_extract(p.summary,'$.chunk') AS chunk,
            {prep_fields}
        FROM conversation_turns t JOIN api_usage a ON a.turn_id=t.turn_id
        JOIN provider_calls p ON p.id=a.id {observation_join}
        WHERE t.usage_session IN {SESSION_SET} AND a.mode='cloud'""", (sessions,)).fetchall()
    grouped = defaultdict(list)
    for row in rows:
        grouped[(row['provider'],row['operation'])].append(row)
    result = []
    for (provider,operation), items in sorted(grouped.items()):
        sent = [r for r in items if r['attempted']]
        costs = [estimate(r) for r in sent]
        discarded = [r for r in sent if r['prefetched'] and r['played_at'] is None and r['outcome'] in FINAL_UNPLAYED]
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
    stt = db.execute(f"""SELECT request_attempted,input_state,code,duration_seconds,latency_ms,cost_estimate,
            json_extract(stt_diagnostics,'$.provider_billed_seconds') AS billed
        FROM developer_inputs WHERE session IN {SESSION_SET} AND kind='audio' AND mode='cloud'""",(sessions,)).fetchall()
    if stt:
        sent = [r for r in stt if r['request_attempted']]
        priced = [r['cost_estimate'] if r['cost_estimate'] is not None else r['billed']*.016/60
                  for r in sent if r['cost_estimate'] is not None or r['billed'] is not None]
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
    for s in sessions:
        key = ('session',s['session']) if not s['stream_id'] or s['identity_conflict'] else ('stream',s['stream_id'])
        groups[(s['platform'], *key)].append(s)
    summaries = {(s['platform'],s['stream_id']):s for s in db.execute('SELECT * FROM studio_stream_summaries')} if version>=20 else {}
    for platform, external in summaries:
        groups.setdefault((platform,'stream',external), [])
    broadcasts = []
    for (platform, binding, identity), runs in groups.items():
        ids = json.dumps([r['session'] for r in runs])
        inputs = timeline(db,ids)
        audience = list(db.execute(f'SELECT * FROM studio_audience WHERE session IN {SESSION_SET} ORDER BY observed', (ids,)))
        samples = [r for r in audience if r['status']=='live' and binding=='stream' and r['stream_id']==identity]
        counts = [r['viewers'] for r in samples if r['viewers'] is not None]
        summary = summaries.get((platform,identity)) if binding=='stream' else None
        provider_start = summary['started'] if summary else next(
            (r['stream_started'] for r in audience if binding == 'stream' and r['stream_id'] == identity
             and r['stream_started'] is not None), None)
        external_id = identity if binding == 'stream' else None
        if version >= 21:
            intervals = db.execute(f'SELECT started,ended FROM studio_chat_intervals WHERE session IN {SESSION_SET} ORDER BY started', (ids,))
            events = list(db.execute(f'SELECT observed,status FROM studio_provider_events WHERE session IN {SESSION_SET} ORDER BY observed', (ids,)))
        else:
            intervals, events = (), ()
        tracked = version >= 21 and any(r['chat_tracked'] for r in runs)
        state, evidence, live_until, ended = broadcast_state(runs, external_id, summary, audience, events, now)
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
            'state': state, 'state_source': evidence, 'live_until': live_until, 'chat_coverage': coverage,
            'platform':platform, 'stream_id':identity if binding=='stream' else None,
            'sessions':[{'id':r['session'],'mode':r['mode'],'prefetch':bool(r['prefetch']),
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
            'api':usage(db,ids) if runs else None,
        })
    return {'broadcasts':broadcasts,'legacy_sessions':legacy_sessions(db,json.dumps([s['session'] for s in sessions])) if include_legacy else []}
