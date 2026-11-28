"""Monthly reservations for scheduled reaction and memory requests, in USD microdollars."""
from datetime import datetime, timedelta, timezone

from ravora.providers import ProviderError

AUX_LIMIT = 2_000_000
MEMORY_MAX_INPUT_TOKENS = 1_048_576
JEV_RESERVATION = (65_536*42+999)//1000
# The memory extractor caps output at 1,600 tokens. A response with usage releases unused headroom.
MEMORY_RESERVATION = (3*MEMORY_MAX_INPUT_TOKENS+9)//10 + 4_000


def reserve_auxiliary(db, call_id, now):
    # The caller owns BEGIN IMMEDIATE and marks dispatch in this same transaction.
    month = datetime.fromtimestamp(now,timezone(timedelta(hours=9))).strftime('%Y-%m')
    row = db.execute("""SELECT p.provider,p.operation,a.model FROM provider_calls p JOIN api_usage a USING(id)
        WHERE p.id=? AND p.state='prepared' AND p.attempted=0 AND a.mode='cloud'""",(call_id,)).fetchone()
    if row and tuple(row)==('jev','reaction','jev-1.14.0'):
        reservation=JEV_RESERVATION
    elif row and tuple(row)==('gemini','extract_memory','gemini-3.5-flash-lite'):
        reservation=MEMORY_RESERVATION
    else:
        raise ProviderError('SCHEDULED_AUX_MODEL_UNPRICED')
    used=db.execute("""SELECT COALESCE(SUM(CASE
        WHEN p.provider='jev' AND a.model IN ('jev-1.14.0','jev-latest') AND a.input_tokens>=0 THEN (a.input_tokens*42+999)/1000
        WHEN p.provider='gemini' AND a.model='gemini-3.5-flash-lite' AND a.total_tokens>=a.input_tokens AND a.input_tokens>=0
          THEN (3*a.input_tokens+25*(a.total_tokens-a.input_tokens)+9)/10
        ELSE COALESCE(json_extract(p.summary,'$.aux_reserved_usd_micros'),
             CASE WHEN p.provider='jev' THEN ? ELSE ? END)
        END),0) FROM provider_calls p JOIN api_usage a USING(id)
        WHERE a.mode='cloud' AND p.attempted=1 AND (p.provider='jev' OR (p.provider='gemini' AND p.operation='extract_memory'))
        AND COALESCE(json_extract(p.summary,'$.aux_budget_month'),strftime('%Y-%m',a.created_at,'unixepoch','+9 hours'))=?""",
        (JEV_RESERVATION,MEMORY_RESERVATION,month)).fetchone()[0]
    if used+reservation>AUX_LIMIT:
        raise ProviderError('LOCAL_MONTHLY_AUX_LIMIT')
    db.execute("""UPDATE provider_calls SET summary=json_set(summary,'$.aux_budget_month',?,'$.aux_reserved_usd_micros',?)
               WHERE id=?""",(month,reservation,call_id))
