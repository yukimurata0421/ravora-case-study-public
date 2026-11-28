"""Read-only monthly budget use, with the same counting rules the dispatch checks apply."""
from datetime import datetime, timedelta, timezone

from ravora.scheduled_budget import AUX_LIMIT, JEV_RESERVATION, MEMORY_RESERVATION

JST = timezone(timedelta(hours=9))
# Ceilings scheduled broadcasts apply on top of the configured limits.
SCHEDULED_REPLY_USD, SCHEDULED_TTS_CHARS, SCHEDULED_STT_SECONDS = 20, 900_000, 21_600

REPLY_COST = """COALESCE(json_extract(p.summary,'$.reply_reserved_microusd'),
    MAX(20000,(CASE p.operation WHEN 'stream_generate' THEN 3 ELSE 1 END)*
        ((3*COALESCE(a.input_tokens,0)+9)/10+(5*MAX(400,COALESCE(a.output_tokens,0)+COALESCE(a.thought_tokens,0))+1)/2)))"""
REPLY_WHERE = "p.provider='gemini' AND p.operation IN ('generate','stream_generate') AND p.attempted=1"
AUX_COST = f"""CASE
    WHEN p.provider='jev' AND a.model IN ('jev-1.14.0','jev-latest') AND a.input_tokens>=0 THEN (a.input_tokens*42+999)/1000
    WHEN p.provider='gemini' AND a.model='gemini-3.5-flash-lite' AND a.total_tokens>=a.input_tokens AND a.input_tokens>=0
      THEN (3*a.input_tokens+25*(a.total_tokens-a.input_tokens)+9)/10
    ELSE COALESCE(json_extract(p.summary,'$.aux_reserved_usd_micros'),
         CASE WHEN p.provider='jev' THEN {JEV_RESERVATION} ELSE {MEMORY_RESERVATION} END) END"""
AUX_WHERE = """a.mode='cloud' AND p.attempted=1
    AND (p.provider='jev' OR (p.provider='gemini' AND p.operation='extract_memory'))"""


def scheduled_limits(settings):
    return dict(reply_llm_usd=min(settings.monthly_reply_llm_usd or SCHEDULED_REPLY_USD, SCHEDULED_REPLY_USD),
                tts_characters=min(settings.monthly_tts_chars, SCHEDULED_TTS_CHARS),
                auxiliary_usd=AUX_LIMIT/1_000_000,
                cloud_stt_seconds=min(settings.stt_monthly_audio_seconds, SCHEDULED_STT_SECONDS))


CALL_JOIN = """FROM provider_calls p JOIN api_usage a ON a.id=p.id
    LEFT JOIN conversation_turns t ON t.turn_id=a.turn_id LEFT JOIN source_events e ON e.id=t.source_event_id
    LEFT JOIN studio_sessions s ON s.session=t.usage_session AND s.platform IN ('twitch','youtube')"""
PURPOSE = """CASE WHEN s.session IS NULL THEN 'outside' WHEN e.platform IN ('twitch','youtube') THEN 'viewer'
    WHEN e.platform='ray-initiative' THEN 'unprompted' ELSE 'broadcast_other' END"""
LANGUAGE = "CASE WHEN a.model LIKE 'en-%' THEN 'en' WHEN a.model LIKE 'ja-%' THEN 'ja' ELSE 'other' END"
ENGINE = "CASE WHEN d.provider LIKE 'cloud_speech%' THEN 'cloud_speech' WHEN d.provider='gemini' THEN 'gemini' ELSE 'other' END"


def _split(rows, scale):
    """rows: (purpose, service, used, calls, recent). Sums of each split equal the sum of the rows."""
    def part(index):
        groups = {}
        for row in rows:
            g = groups.setdefault(row[index], [0, 0, 0])
            for n in range(3):
                g[n] += row[n+2] or 0
        return [{'id': name, 'used': round(used/scale, 6), 'calls': calls, 'last_24h': round(recent/scale, 6)}
                for name, (used, calls, recent) in sorted(groups.items(), key=lambda item: -item[1][0])]
    return {'purposes': part(0), 'services': part(1)}


def _calls(db, cost, where, month_key, service, month, since, now):
    in_month = "COALESCE(json_extract(p.summary,?),strftime('%Y-%m',a.created_at,'unixepoch','+9 hours'))=?"
    rows = db.execute(f"""SELECT {PURPOSE},{service},SUM(CASE WHEN {in_month} THEN {cost} ELSE 0 END),
        SUM({in_month}),SUM(CASE WHEN a.created_at BETWEEN ? AND ? THEN {cost} ELSE 0 END) {CALL_JOIN}
        WHERE {where} AND ({in_month} OR a.created_at BETWEEN ? AND ?) GROUP BY 1,2""",
        (*(f'$.{month_key}', month)*2, since, now, f'$.{month_key}', month, since, now)).fetchall()
    return {'used': sum(r[2] for r in rows)/1e6, 'last_24h': sum(r[4] for r in rows)/1e6, **_split(rows, 1e6)}


def _speech(db, month, since, now):
    used = db.execute('SELECT COALESCE(SUM(characters),0) FROM speech_usage WHERE month=?', (month,)).fetchone()[0]
    rows = db.execute(f"""SELECT {PURPOSE},{LANGUAGE},
        SUM(CASE WHEN {{m}} THEN a.characters ELSE 0 END),SUM({{m}} AND a.characters IS NOT NULL),
        SUM(CASE WHEN a.created_at BETWEEN ? AND ? THEN a.characters ELSE 0 END),
        SUM(a.created_at BETWEEN ? AND ? AND a.characters IS NULL) {CALL_JOIN}
        WHERE p.provider='tts' AND a.mode='cloud' AND a.attempted=1 AND ({{m}} OR a.created_at BETWEEN ? AND ?)
        GROUP BY 1,2""".replace('{m}', "strftime('%Y-%m',a.created_at,'unixepoch','+9 hours')=?"),
        (month, month, since, now, since, now, month, since, now)).fetchall()
    result = _split([r for r in rows if r[3] or r[4]], 1)
    # speech_usage has no link to calls or timestamps. Streaming reservations have no per-call count, so they stay together.
    rest = used-sum(r[2] or 0 for r in rows)
    if rest > 0:
        for split in result.values():
            split.append({'id': 'not_itemized', 'used': rest, 'calls': None, 'last_24h': None})
    return {'used': used, 'last_24h': None if any(r[5] for r in rows) else sum(r[4] or 0 for r in rows), **result}


def _transcription(db, month, since, now):
    rows = db.execute(f"""SELECT CASE WHEN s.session IS NULL THEN 'outside' ELSE 'broadcast_other' END,{ENGINE},
        SUM(CASE WHEN d.month=? THEN COALESCE(d.duration_seconds,0) ELSE 0 END),SUM(d.month=?),
        SUM(CASE WHEN d.created_at BETWEEN ? AND ? THEN COALESCE(d.duration_seconds,0) ELSE 0 END)
        FROM developer_inputs d LEFT JOIN studio_sessions s ON s.session=d.session AND s.platform IN ('twitch','youtube')
        WHERE d.request_attempted=1 AND (d.month=? OR d.created_at BETWEEN ? AND ?) GROUP BY 1,2""",
        (month, month, since, now, month, since, now)).fetchall()
    return {'used': sum(r[2] for r in rows), 'last_24h': sum(r[4] for r in rows), **_split(rows, 1)}


def budget_status(db, limits, now):
    moment = datetime.fromtimestamp(now, JST)
    month, since = moment.strftime('%Y-%m'), now-86400
    resets = (moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)+timedelta(days=32)).replace(day=1)
    window = month, since, now
    use = {'reply_llm_usd': _calls(db, REPLY_COST, REPLY_WHERE, 'reply_budget_month', 'p.operation', *window),
           'auxiliary_usd': _calls(db, AUX_COST, AUX_WHERE, 'aux_budget_month', "CASE p.provider WHEN 'jev' THEN 'reaction' ELSE 'memory' END", *window),
           'tts_characters': _speech(db, *window), 'cloud_stt_seconds': _transcription(db, *window)}
    for item in use.values():
        for key in ('used', 'last_24h'):
            item[key] = None if item[key] is None else round(item[key], 6)
    return {'month': month, 'resets_jst': resets.isoformat(timespec='minutes'),
            **{name: {'limit': limits[name], **item} for name, item in use.items()}}
