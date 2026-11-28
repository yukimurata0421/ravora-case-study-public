"""Versioned count-only contract shared with the Pi collector."""
LEGACY_SCHEMA = 'ravora.ray_public_observation.v4'
V5_SCHEMA = 'ravora.ray_public_observation.v5'
V6_SCHEMA = 'ravora.ray_public_observation.v6'
V7_SCHEMA = 'ravora.ray_public_observation.v7'
V8_SCHEMA = 'ravora.ray_public_observation.v8'
SCHEMA = 'ravora.ray_public_observation.v9'
MAX_BROADCASTS = 60
P95_MIN_SAMPLES = 20
CHAT_RANK_COVERAGE = .95
PROVIDERS = ('gemini', 'jev', 'tts', 'stt', 'other')
API_COUNTS = ('attempts', 'failed', 'retries', 'discarded_prefetch_attempts', 'outcome_unknown')
# These values are also consumed by the Dell scheduler; Python weekday 0 is Monday.
DAILY = tuple(range(7))
EXTRA_DAYS = (1, 3, 5, 6)
OLD_SLOTS = (
    ('en-03', 3, DAILY), ('en-05', 5, DAILY), ('en-07', 7, DAILY),
    ('en-11', 11, EXTRA_DAYS), ('ja-17', 17, DAILY), ('ja-19', 19, DAILY), ('ja-21', 21, DAILY),
)
SLOTS = tuple((f"{'en' if hour < 15 else 'ja'}-{hour:02d}", hour, DAILY) for hour in range(1, 24, 2))
OLD_IDS = [name for name, _, _ in OLD_SLOTS]
SLOT_IDS = [name for name, _, _ in SLOTS]
WEEKLY_SHOWS = sum(len(days) for _, _, days in SLOTS)


def obj(**fields):
    return {'type': 'object', 'properties': fields, 'required': list(fields), 'additionalProperties': False}


def nullable(item):
    return {'anyOf': [item, {'type': 'null'}]}


def array(item, high, low=0):
    return {'type': 'array', 'items': item, 'minItems': low, 'maxItems': high}


COUNT = {'type': 'integer', 'minimum': 0, 'maximum': 10**12}
NUMBER = {'type': 'number', 'minimum': 0, 'maximum': 10**12}
NCOUNT, NNUMBER = nullable(COUNT), nullable(NUMBER)
BOOL = {'type': 'boolean'}
STAMP = {'type':'string', 'pattern':r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}\+09:00$'}
API = obj(**dict.fromkeys(API_COUNTS, COUNT))
PROVIDER_USAGE = obj(**API['properties'], input_tokens=NCOUNT, output_tokens=NCOUNT, characters=NCOUNT,
                     tokens_reported_calls=COUNT, estimated_usd_known=NNUMBER, unpriced_calls=COUNT)
TIMING = obj(p50_ms=NNUMBER, p95_ms=NNUMBER, sample_p95_ms=NNUMBER, n=COUNT, missing=COUNT)
GAP = obj(**TIMING['properties'], idle_excluded=COUNT, first_in_session=COUNT,
          previous_end_unknown=COUNT, no_program_signal=COUNT)
CHAT = obj(status={'enum':['complete','partial','unobserved','unknown']}, observed_seconds=NNUMBER,
           window_seconds=NNUMBER, eligible=BOOL)
COHORT = obj(first_seen=COUNT, replied=COUNT, spoke_again=COUNT)
MARKET = obj(complete_samples=COUNT, partial_samples=COUNT, failed_samples=COUNT,
             expected_samples=COUNT, avg_channels=NNUMBER, avg_viewers=NNUMBER,
             top5_share_pct=nullable({'type':'number','minimum':0,'maximum':100}),
             paired_ray_minutes=NNUMBER, overlap_minutes=NNUMBER)
RUNS = obj(claimed=COUNT, blocked=COUNT, failed=COUNT, stopped_early=COUNT, unresolved=COUNT)
ACTIVITY = obj(broadcasts=COUNT, live=COUNT, last_seen=COUNT, chat_broadcasts=COUNT, speakers=NCOUNT,
               comments=NCOUNT, replied=NCOUNT, spoke_again=NCOUNT, cohort=nullable(COHORT), gap=nullable(GAP))
SLOT = obj(activity=ACTIVITY, runs=RUNS, id={'enum':OLD_IDS}, language={'enum':['en','ja']}, days={'enum':['daily','tue-thu-sat-sun']},
           start={'enum':[f'{hour:02d}:00' for _,hour,_ in OLD_SLOTS]}, end={'enum':[f'{hour+2:02d}:00' for _,hour,_ in OLD_SLOTS]},
           broadcasts=COUNT, eligible=COUNT, eligible_minutes=NUMBER, speakers=NCOUNT, spoke_again=NCOUNT,
           unique_viewers=NCOUNT, viewer_summaries=COUNT, cohort=nullable(COHORT), market=MARKET)
BROADCAST = obj(platform={'enum':['twitch','youtube']}, slot={'enum':[*OLD_IDS,'off-plan']}, started_jst=STAMP,
                state={'enum':['live','ended','unknown']}, state_source={'enum':['summary','helix','youtube','studio_stop','unconfirmed']},
                live_until=NNUMBER, last_confirmed_at=NNUMBER, last_audience_at=NNUMBER, latest_viewers=NCOUNT,
                minutes=NNUMBER, minutes_source={'enum':['summary','program','last_live',None]}, unique_viewers=NCOUNT, peak_viewers=NCOUNT, average_viewers=NNUMBER,
                average_source={'enum':['summary','samples',None]},
                peak_source={'enum':['summary','samples',None]}, viewer_samples=COUNT, chat=CHAT,
                speakers=NCOUNT, comments=NCOUNT, selected=NCOUNT, replied=NCOUNT, unplayed=NCOUNT, prefetched=NCOUNT,
                prefetched_ready=NCOUNT, prefetched_discarded=NCOUNT, audio_ready=nullable(TIMING), budget_stopped=NCOUNT, spoke_again=NCOUNT,
                prefetch={'enum':['serial','prefetch','mixed','unknown']}, gap=nullable(GAP), reply=nullable(TIMING),
                player=nullable(TIMING), api=nullable(obj(**API['properties'], by_provider=obj(**dict.fromkeys(PROVIDERS, PROVIDER_USAGE)))),
                cost_usd=NNUMBER, cost_usd_known=NNUMBER, unpriced_calls=NCOUNT, cost_partial=BOOL, cohort=nullable(COHORT), market=nullable(MARKET))
TOTALS = obj(broadcasts=COUNT, minutes=NNUMBER, viewer_summaries=COUNT, unique_viewers=NCOUNT,
             speakers=NCOUNT, comments=NCOUNT, replied=NCOUNT, budget_stopped=NCOUNT, spoke_again=NCOUNT,
             chat_broadcasts=COUNT, api_broadcasts=COUNT, cost_usd=NNUMBER, cost_usd_known=NNUMBER, unpriced_calls=NCOUNT, api=nullable(API),
             cohort=nullable(COHORT),
             repeat_viewers=nullable(obj(speakers=COUNT, on_two_or_more_days=COUNT)))
LEGACY = obj(platform={'enum':['twitch','youtube']}, sessions=COUNT, retained_comments=COUNT, viewer_turns=COUNT, played_flags=COUNT)
OBSERVED = obj(**TOTALS['properties'], live=COUNT, last_seen=COUNT, unknown=COUNT, gap=nullable(GAP))
LEGACY_SOURCE_SCHEMA = obj(schema={'const':LEGACY_SCHEMA}, generated_at=NUMBER, pipeline=obj(observations_ready=BOOL),
                    experiment=obj(weekly_broadcasts={'const':46}, weekly_hours={'const':92},
                                   next_jst=STAMP, next_slot={'enum':OLD_IDS}, min_chat_fraction={'const':CHAT_RANK_COVERAGE}),
                    totals=TOTALS, observed=OBSERVED, legacy=array(LEGACY,2), slots=array(SLOT,len(OLD_SLOTS),len(OLD_SLOTS)), broadcasts=array(BROADCAST,MAX_BROADCASTS))


SLI = obj(eligible=COUNT, started=COUNT, within_3s=COUNT, within_5s=COUNT, budget_blocked=COUNT)
HEALTH = obj(last_attempt=NNUMBER, last_success=NNUMBER,
             status={'enum':['live','offline','http_401','unavailable','no_samples']},
             consecutive_401=COUNT, total_401=COUNT)
BROADCAST_V5 = obj(**BROADCAST['properties'], response_sli=nullable(SLI), audience_health=nullable(HEALTH),
                   speech_policy={'enum':['short-turn-v1','mixed','unrecorded']})
V5_SOURCE_SCHEMA = obj(**{**LEGACY_SOURCE_SCHEMA['properties'], 'schema':{'const':V5_SCHEMA},
                       'broadcasts':array(BROADCAST_V5,MAX_BROADCASTS)})


PROMOTION = {'enum':['not_scheduled','planned','posted','skipped','unrecorded']}
BROADCAST_V6 = obj(**{**BROADCAST_V5['properties'], 'slot':{'enum':[*SLOT_IDS,'off-plan']}, 'x_promotion':PROMOTION})
SLOT_V6 = obj(**{**SLOT['properties'], 'id':{'enum':SLOT_IDS}, 'days':{'const':'daily'},
                 'start':{'enum':[f'{h:02d}:00' for _,h,_ in SLOTS]},
                 'end':{'enum':[f'{(h+2)%24:02d}:00' for _,h,_ in SLOTS]}})
PERFORMANCE = obj(broadcasts=COUNT, minutes=NUMBER, first_seen=COUNT, cost_usd_known=NUMBER,
                  unpriced_calls=COUNT, cost_missing=COUNT, first_per_hour=NNUMBER, first_per_usd=NNUMBER,
                  returned=COUNT, return_rate=NNUMBER, paired_ray_minutes=NUMBER, overlap_minutes=NNUMBER)
EXPLORATION = obj(start_jst=STAMP, end_jst=STAMP, planned={'const':168}, due=COUNT, claimed=COUNT, finished=COUNT,
                  slots=obj(**dict.fromkeys(SLOT_IDS, PERFORMANCE)),
                  by_x=obj(**dict.fromkeys(('not_scheduled','planned','posted','skipped','unrecorded'), PERFORMANCE)))
EXPERIMENT = obj(weekly_broadcasts={'const':84}, weekly_hours={'const':168}, next_jst=STAMP,
                 next_slot={'enum':SLOT_IDS}, min_chat_fraction={'const':CHAT_RANK_COVERAGE}, exploration=EXPLORATION)
V6_SOURCE_SCHEMA = obj(**{**V5_SOURCE_SCHEMA['properties'], 'schema':{'const':V6_SCHEMA}, 'experiment':EXPERIMENT,
                       'slots':array(SLOT_V6,12,12), 'broadcasts':array(BROADCAST_V6,MAX_BROADCASTS)})
# Monthly local limits (JST). Amounts only: no account, project or provider balance.
BUDGET = obj(limit=NUMBER, used=NUMBER, last_24h=NNUMBER)
BUDGETS = obj(month={'type':'string','pattern':r'^\d{4}-\d{2}$'}, resets_jst=STAMP, reply_llm_usd=BUDGET, tts_characters=BUDGET,
              auxiliary_usd=BUDGET, cloud_stt_seconds=BUDGET)
V7_SOURCE_SCHEMA = obj(**V6_SOURCE_SCHEMA['properties'] | {'schema':{'const':V7_SCHEMA}, 'budgets':nullable(BUDGETS)})
# Where each limit goes. Purposes follow the broadcast and comment source; services follow the operation or voice language.
BUDGET_PURPOSES = ('viewer', 'unprompted', 'broadcast_other', 'outside', 'not_itemized')
BUDGET_SERVICES = {'reply_llm_usd':('generate','stream_generate'), 'tts_characters':('en','ja','other','not_itemized'),
                   'auxiliary_usd':('reaction','memory'), 'cloud_stt_seconds':('cloud_speech','gemini','other')}


def parts(names):
    return array(obj(id={'enum':list(names)}, used=NUMBER, calls=NCOUNT, last_24h=NNUMBER), len(names))


V8_SOURCE_SCHEMA = obj(**V6_SOURCE_SCHEMA['properties'] | {'schema':{'const':V8_SCHEMA}, 'budgets':nullable(obj(**BUDGETS['properties'] | {
    name:obj(**BUDGET['properties'], purposes=parts(BUDGET_PURPOSES), services=parts(services))
    for name, services in BUDGET_SERVICES.items()}))})
# Speech synthesis per broadcast by where it ran: on Dell (no API budget) or a cloud voice. Calls include failures.
VOICE = obj(local_calls=COUNT, local_failed=COUNT, local_characters=COUNT,
            cloud_calls=COUNT, cloud_failed=COUNT, cloud_characters=COUNT)
BROADCAST_V9 = obj(**BROADCAST_V6['properties'], voice=nullable(VOICE))
SOURCE_SCHEMA = obj(**V8_SOURCE_SCHEMA['properties'] | {'schema':{'const':SCHEMA}, 'broadcasts':array(BROADCAST_V9,MAX_BROADCASTS)})


def validate(value, now):
    # Validation runs at the publication boundary. The Dell exporter needs only stdlib.
    from jsonschema import Draft7Validator
    version = value.get('schema') if isinstance(value,dict) else None
    schema = {LEGACY_SCHEMA:LEGACY_SOURCE_SCHEMA, V5_SCHEMA:V5_SOURCE_SCHEMA, V6_SCHEMA:V6_SOURCE_SCHEMA, V7_SCHEMA:V7_SOURCE_SCHEMA,
              V8_SCHEMA:V8_SOURCE_SCHEMA}.get(version,SOURCE_SCHEMA) if isinstance(version,str) else SOURCE_SCHEMA
    if not Draft7Validator(schema).is_valid(value):
        raise ValueError('SCHEMA')
    if not now - 120 <= value['generated_at'] <= now + 10:
        raise ValueError('GENERATED_AT')
    actual = [(s['id'], s['language'], s['start'], s['end'], s['days']) for s in value['slots']]
    expected = [(name, name[:2], f'{hour:02d}:00', f'{(hour+2)%24:02d}:00',
                 'daily' if days == DAILY else 'tue-thu-sat-sun') for name, hour, days in (SLOTS if version in (V6_SCHEMA,V7_SCHEMA,V8_SCHEMA,SCHEMA) else OLD_SLOTS)]
    if actual != expected:
        raise ValueError('SLOTS')
    timings = [value['observed']['gap'], *(slot['activity']['gap'] for slot in value['slots'])]
    for row in value['broadcasts']:
        sli = row.get('response_sli')
        if sli and not (sli['within_3s'] <= sli['within_5s'] <= sli['started'] <= sli['eligible']
                        and sli['budget_blocked'] <= sli['eligible']-sli['started']):
            raise ValueError('SLI')
        health = row.get('audience_health')
        if health and (health['consecutive_401'] > health['total_401'] or
                       (health['last_success'] is not None and
                        (health['last_attempt'] is None or health['last_success'] > health['last_attempt']))):
            raise ValueError('AUDIENCE_HEALTH')
        voice = row.get('voice')
        if voice and (voice['local_failed'] > voice['local_calls'] or voice['cloud_failed'] > voice['cloud_calls']):
            raise ValueError('VOICE')
        timings.extend(row[k] for k in ('gap','reply','audio_ready','player'))
    for entry in timings:
        if entry is None:
            continue
        n = entry['n']
        median, sample, p95 = entry['p50_ms'], entry['sample_p95_ms'], entry['p95_ms']
        if ((median is None) != (n == 0) or (sample is None) != (n == 0)
                or (sample is not None and median > sample)
                or p95 != (sample if n >= P95_MIN_SAMPLES else None)):
            raise ValueError('TIMING')
    # jsonschema allows Python's non-JSON NaN as a number.
    import json
    try:
        json.dumps(value, allow_nan=False)
    except ValueError:
        raise ValueError('NONFINITE') from None
    return value
