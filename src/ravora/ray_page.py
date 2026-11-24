"""Render the validated public snapshot, including the content of the initial HTTP response."""
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path
from string import Template

from ravora.ray_public_contract import P95_MIN_SAMPLES

JST = timezone(timedelta(hours=9))
TEMPLATE = Path(__file__).with_name('web') / 'observation' / 'index.html'
DASH = '<span class="na">—</span>'
DAYS = {'daily': 'Every day', 'tue-thu-sat-sun': 'Tue · Thu · Sat · Sun'}
# Local start times for English slots. November offsets from JST: London −9 h, New York −14 h.
REACH = (('London', -9), ('New York', -14))


def number(value, digits=0, missing='Unobserved'):
    return missing if value is None else f'{value:,.{digits}f}'


def num(value, digits=0):
    """HTML number cell: a dash when unobserved."""
    return DASH if value is None else escape(f'{value:,.{digits}f}')


def clock(stamp):
    return datetime.fromtimestamp(stamp, JST).strftime('%m/%d %H:%M:%S JST')


def start_time(iso):
    return datetime.fromisoformat(iso).strftime('%m/%d %a %H:%M')


def slot_name(name):
    if name == 'off-plan':
        return 'Unscheduled'
    lang, hour = name.split('-')
    return f'{lang.upper()} {hour}–{int(hour)+2:02d}'


def percentile_text(t, unit='intervals'):
    if t is None:
        return 'No chat observation'
    n = t['n']
    if n == 0:
        return f'Awaiting {unit} · 0/{P95_MIN_SAMPLES}'
    if t['p95_ms'] is not None:
        return f'{number(t["p95_ms"],1)} ms · n={n}'
    return f'{number(t["sample_p95_ms"],1)} ms · provisional ({n}/{P95_MIN_SAMPLES})'


def duration(ms_value):
    if ms_value is None:
        return DASH
    return f'{ms_value:,.0f} ms' if ms_value < 1000 else f'{ms_value/1000:,.1f} s'


def p95_cell(t):
    """p95 once it is comparable; before that the sample value with its count, e.g. 207 ms 1/20."""
    if t is None or t['n'] == 0:
        return DASH
    if t['p95_ms'] is not None:
        return duration(t['p95_ms'])
    return (f'{duration(t["sample_p95_ms"])} <span class="flag" title="provisional: {t["n"]} of '
            f'{P95_MIN_SAMPLES} intervals">{t["n"]}/{P95_MIN_SAMPLES}</span>')


def cost_parts(row):
    """(headline value, note) for a row with cost_usd / cost_usd_known / unpriced_calls."""
    count = row['unpriced_calls'] or 0
    if row['cost_usd'] is not None:
        return f'${row["cost_usd"]:,.2f}', 'estimated, list prices'
    if row['cost_usd_known'] is not None:
        return f'${row["cost_usd_known"]:,.2f}', f'+{count} calls not priced yet'
    return DASH, f'{count} calls not priced yet' if count else 'no cost evidence'


def coverage_pct(chat):
    seconds, window = chat['observed_seconds'], chat['window_seconds']
    if seconds is None or not window:
        return escape(chat['status'])
    return f'{100*seconds/window:.1f}% <span class="sub-inline">{seconds/60:,.1f} min</span>'


def kpi(label, value, note, of=None):
    frac = ''
    suffix = ''
    if of:
        suffix = f'<small>/ {of:,}</small>'
        try:
            share = max(0.0, min(1.0, float(value.replace(',', '')) / of))
            frac = f'<div class="meter"><i style="width:{share*100:.1f}%"></i></div>'
        except ValueError:
            pass
    return (f'<div class="kpi"><span class="label">{escape(label)}</span><span class="value">{value}{suffix}</span>'
            f'{frac}<span class="note">{note}</span></div>')


def headline(d):
    t = d['observed']
    c = t['cohort'] or {}
    first, replied = c.get('first_seen'), c.get('replied')
    cost, cost_note = cost_parts(t)
    tiles = [kpi('First commenters', num(first), 'distinct accounts'),
             kpi('Got a reply', num(replied), 'Ray answered them on air', first if first else None),
             kpi('Wrote again', num(c.get('spoke_again')), 'after hearing the reply', replied if replied else None),
             kpi('API cost', cost, escape(cost_note))]
    finished = d['totals']['broadcasts']
    state = []
    if t['live']:
        state.append(f'{t["live"]} live')
    if t['last_seen']:
        state.append(f'{t["last_seen"]} awaiting confirmation')
    repeat = t['repeat_viewers'] or {}
    viewers = (f'<b>{num(t["unique_viewers"])}</b>' if t['viewer_summaries']
               else '<b class="na">after summary import</b>')
    minor = [f'Broadcasts <b>{num(t["broadcasts"])}</b>' + (f' ({", ".join(state)})' if state else '')
             + f' · {finished} finished',
             f'Minutes on air <b>{num(t["minutes"])}</b>',
             f'Comments <b>{num(t["comments"])}</b>, <b>{num(t["replied"])}</b> answered',
             f'Stopped by budget <b>{num(t["budget_stopped"])}</b>',
             f'Unique viewers {viewers}',
             f'Came back another day <b>{num(repeat.get("on_two_or_more_days"))}</b>']
    if t['unknown']:
        minor.append(f'Unconfirmed records excluded <b>{t["unknown"]}</b>')
    return ''.join(tiles), ''.join(f'<span>{m}</span>' for m in minor)


def reaches(s):
    if s['language'] == 'ja':
        return 'Japan evening'
    hour = int(s['start'][:2])
    return ' · '.join(f'{city} {(hour+offset) % 24:02d}:00' for city, offset in REACH)


def band(d):
    nxt = d['experiment']['next_slot']
    parts = ['<div class="axis"></div>']
    for hour in range(0, 25, 3):
        parts.append(f'<div class="tick" style="left:{hour/24*100:.3f}%"><span>{hour:02d}</span></div>')
    for s in d['slots']:
        start, end = int(s['start'][:2]), int(s['end'][:2])
        a = s['activity']
        has = a['broadcasts'] > 0
        first = (a['cohort'] or {}).get('first_seen')
        body = (f'{first or 0} new' if first is not None else 'chat pending') if has else DASH
        classes = f'block {s["language"]}' + (' has' if has else '') + (' next' if s['id'] == nxt else '')
        live = '<span class="dot" title="live"></span>' if a['live'] else ''
        parts.append(f'<div class="{classes}" style="left:calc({start/24*100:.3f}% + 2px);width:calc({(end-start)/24*100:.3f}% - 4px)" '
                     f'title="{escape(slot_name(s["id"]))}"><span class="lang">{s["language"].upper()}{live}</span>'
                     f'<b>{s["start"]}</b>{body}</div>')
    return ''.join(parts)


def slot_row(s):
    a, m = s['activity'], s['market']
    has = a['broadcasts'] > 0
    c = a['cohort'] or {}
    done = f'{s["broadcasts"]} done'
    extra = []
    if a['live']:
        extra.append(f'{a["live"]} live')
    if a['last_seen']:
        extra.append(f'{a["last_seen"]} unconfirmed')
    broadcasts = (done + (' · ' + ' · '.join(extra) if extra else '')) if has else (
        'launch recorded' if s['runs']['claimed'] else '0')
    market = DASH if m['avg_channels'] is None else (
        f'{m["avg_channels"]:,.0f} channels · {m["avg_viewers"]:,.0f} avg viewers'
        + (f'<span class="sub">top five hold {m["top5_share_pct"]:.0f}%</span>' if m['top5_share_pct'] is not None else ''))
    overlap = DASH if m['paired_ray_minutes'] is None else f'{m["overlap_minutes"]:,.0f} / {m["paired_ray_minutes"]:,.0f} min'
    rate = ''
    if s['eligible']:
        rate = f'<span class="sub">{s["cohort"]["first_seen"]/s["eligible"]:.1f} per comparable broadcast</span>'
    return (f'<tr class="{"" if has else "empty"}" id="slot-{s["id"]}"><td class="slotname"><span class="lang {s["language"]}">'
            f'{s["language"].upper()}</span><b>{s["start"]}–{s["end"]}</b><span class="sub">{DAYS[s["days"]]} · {reaches(s)}</span></td>'
            f'<td class="r">{broadcasts}</td><td class="r">{num(c.get("first_seen"))}{rate}</td>'
            f'<td class="r">{num(c.get("spoke_again"))}</td><td class="r">{p95_cell(a["gap"])}</td><td class="market">{market}</td>'
            f'<td class="r">{overlap}</td></tr>')


def slot_coverage(slots):
    rows = [s for s in slots if s['market']['expected_samples'] or s['runs']['claimed']]
    if not rows:
        return ''
    body = ''.join(
        f'<tr><td>{escape(slot_name(s["id"]))}</td><td class="r">{s["runs"]["claimed"]}</td><td class="r">{s["runs"]["blocked"]}</td>'
        f'<td class="r">{s["runs"]["failed"]}</td><td class="r">{s["runs"]["stopped_early"]}</td><td class="r">{s["runs"]["unresolved"]}</td>'
        f'<td class="r">{s["market"]["complete_samples"]} / {s["market"]["expected_samples"]}</td>'
        f'<td class="r">{s["market"]["partial_samples"]} · {s["market"]["failed_samples"]}</td>'
        f'<td class="r">{s["eligible"]} · {s["eligible_minutes"]:,.0f} min</td></tr>' for s in rows)
    return ('<details class="more boxed" data-key="slot-coverage"><summary>Launches and market sampling by slot</summary>'
            '<div class="tw"><table><thead><tr><th>Slot</th><th class="r">Launches</th><th class="r">Blocked</th><th class="r">Failed</th>'
            '<th class="r">Stopped early</th><th class="r">Unresolved</th><th class="r">Market scans</th><th class="r">Partial · failed</th>'
            f'<th class="r">Comparable broadcasts</th></tr></thead><tbody>{body}</tbody></table></div></details>')


def timing_row(label, t):
    if t is None:
        return f'<tr><td>{label}</td><td class="r">{DASH}</td><td class="r">{DASH}</td><td class="r na">no chat</td></tr>'
    missing = f' <span class="na">+{t["missing"]} missing</span>' if t['missing'] else ''
    p50 = duration(t['p50_ms']) if t['n'] else DASH
    return f'<tr><td>{label}</td><td class="r">{p50}</td><td class="r">{p95_cell(t)}</td><td class="r">{t["n"]}{missing}</td></tr>'


def usage(p):
    if p['characters'] is not None:
        return f'{p["characters"]:,} chars'
    if p['input_tokens'] is not None:
        return f'{p["input_tokens"] + (p["output_tokens"] or 0):,} tokens'
    return DASH


def card(b, ordinal, open_first):
    state = b['state']
    if state == 'live':
        pill, when = 'live', 'Live'
        status = f'confirmed {clock(b["last_confirmed_at"])}'
    elif state == 'finished':
        pill, when, status = 'ok', 'Finished', b['state_source']
    else:
        pill, when = 'wait', 'Unconfirmed'
        status = f'last seen {clock(b["last_confirmed_at"])}' if b['last_confirmed_at'] else 'no provider confirmation'
    started = datetime.fromisoformat(b['started_jst']).strftime('%a %d %b, %H:%M')
    cost, cost_note = cost_parts(b)
    if b['api'] and cost_note.startswith('estimated'):
        cost_note = f'{b["api"]["attempts"]:,} calls'
    co = b['cohort'] or {}

    def stat(label, value, note):
        return f'<div class="stat"><span>{label}</span><b>{value}</b><small>{note}</small></div>'

    stats = [stat('Peak viewers', num(b['peak_viewers']),
                  'unique after summary' if b['unique_viewers'] is None else f'{b["unique_viewers"]:,} unique'),
             stat('Speakers', num(b['speakers']), f'{num(co.get("first_seen"))} first-time'),
             stat('Comments', num(b['comments']),
                  f'{b["budget_stopped"]} budget stops' if b['budget_stopped'] else 'no budget stops'),
             stat('Answered', num(b['replied']),
                  f'{round(100*b["replied"]/b["comments"])}% of comments' if b['comments'] and b['replied'] is not None else '&nbsp;'),
             stat('Wrote again', num(b['spoke_again']), 'after a reply'),
             stat('API cost', cost, escape(cost_note))]
    m = b['market'] or {}
    chat_rows = [('Chat coverage', coverage_pct(b['chat'])),
                 ('Average viewers', f'{num(b["average_viewers"], 1)} <span class="sub-inline">{escape(b["average_source"] or "no samples")}</span>'),
                 ('Peak source', f'{escape(b["peak_source"] or "no samples")} · {b["viewer_samples"]} samples'),
                 ('Prepared replies', f'{escape(b["prefetch"])} · {num(b["selected"])} selected · {num(b["prefetched"])} prefetched · '
                                      f'{num(b["prefetched_discarded"])} discarded'),
                 ('Unplayed', num(b['unplayed'])),
                 ('VTuber market', DASH if m.get('avg_channels') is None else f'{m["avg_channels"]:,.0f} ch · {m["avg_viewers"]:,.0f} avg'),
                 ('vedal987 overlap', DASH if m.get('paired_ray_minutes') is None else f'{m["overlap_minutes"]:,.0f} / {m["paired_ray_minutes"]:,.0f} min')]
    if b['latest_viewers'] is not None and b['last_audience_at'] is not None:
        chat_rows.insert(1, ('Watching at last sample', f'{b["latest_viewers"]:,} <span class="sub-inline">{clock(b["last_audience_at"])}</span>'))
    g = b['gap']
    gap_note = ''
    if g:
        gap_note = (f'<p class="fine">Ray gap excludes {g["idle_excluded"]} idle · {g["first_in_session"]} first utterances · '
                    f'{g["previous_end_unknown"]} unknown ends · {g["no_program_signal"]} missing signals.</p>')
    api_rows = ''
    if b['api']:
        for name, p in b['api']['by_provider'].items():
            if not p['attempts']:
                continue
            flags = ''.join(f' <span class="flag">{p[k]} {label}</span>' for k, label in
                            (('failed', 'failed'), ('retries', 'retries'), ('outcome_unknown', 'unknown'),
                             ('discarded_prefetch_attempts', 'discarded')) if p[k])
            price = DASH if p['estimated_usd_known'] is None else f'${p["estimated_usd_known"]:,.3f}'
            if p['unpriced_calls'] and p['estimated_usd_known'] is not None:
                price += f' <span class="na">+{p["unpriced_calls"]}</span>'
            elif p['unpriced_calls']:
                price = '<span class="na">unpriced</span>'
            api_rows += (f'<tr><td>{escape(name)}</td><td class="r">{p["attempts"]:,}{flags}</td>'
                         f'<td class="r">{usage(p)}</td><td class="r">{price}</td></tr>')
    api_rows = api_rows or f'<tr><td colspan="4" class="na">No API calls recorded</td></tr>'
    key = f'{b["platform"]}-{b["started_jst"]}-{ordinal}'
    return (f'<article class="card"><div class="card-head"><span class="pill {pill}">{when}</span>'
            f'<h3>{escape(started)} JST</h3><span class="meta">{escape(slot_name(b["slot"]))} · {escape(b["platform"])} · '
            f'{num(b["minutes"])} min · {escape(status)}</span></div>'
            f'<div class="stats">{"".join(stats)}</div>'
            f'<details class="more" data-key="{escape(key)}"{" open" if open_first else ""}><summary>Response times, chat and API</summary>'
            '<div class="panels">'
            '<div class="panel"><h4>Response times</h4><div class="tw"><table><thead><tr><th></th><th class="r">p50</th>'
            '<th class="r">p95</th><th class="r">n</th></tr></thead><tbody>'
            + timing_row('Ray gap', b['gap']) + timing_row('Comment → audio ready', b['audio_ready'])
            + timing_row('Comment → on air', b['reply']) + timing_row('Comment → viewer hears', b['player'])
            + f'</tbody></table></div>{gap_note}</div>'
            '<div class="panel"><h4>Chat and audience</h4><div class="tw"><table class="wrapv"><tbody>'
            + ''.join(f'<tr><td>{label}</td><td class="r">{value}</td></tr>' for label, value in chat_rows)
            + '</tbody></table></div></div>'
            '<div class="panel"><h4>API calls</h4><div class="tw"><table><thead><tr><th></th><th class="r">Calls</th>'
            f'<th class="r">Usage</th><th class="r">Cost</th></tr></thead><tbody>{api_rows}</tbody></table></div></div>'
            '</div></details></article>')


def render(d):
    collected = d['pipeline']['collected_at']
    rows, e = d['broadcasts'], d['experiment']
    live = next((b for b in rows if b['state'] == 'live'), None)
    if live:
        state = 'live'
        title = f'{slot_name(live["slot"])} slot is live'
        detail = (f'Started {start_time(live["started_jst"])} JST · {number(live["minutes"])} min so far · '
                  f'confirmed by {live["state_source"]} at {clock(live["last_confirmed_at"])}.')
    elif rows and rows[0]['state'] == 'unknown':
        state, title = 'unknown', 'Awaiting fresh provider confirmation'
        last = rows[0]['last_confirmed_at']
        detail = 'Last seen live ' + clock(last) + '.' if last is not None else 'The studio record has no confirmed provider start.'
    elif rows:
        state, title = 'ok', 'Latest broadcast finished'
        detail = f'{d["totals"]["broadcasts"]} finished broadcasts recorded.'
    else:
        state, title, detail = 'collecting', 'Awaiting broadcast observations', 'No instrumented broadcast yet.'
    detail += f' Next: {slot_name(e["next_slot"])} at {start_time(e["next_jst"])} JST.'
    tiles, minor = headline(d)
    cards, identities = [], {}
    for i, row in enumerate(rows):
        key = row['platform'], row['started_jst']
        ordinal = identities.get(key, 0)
        cards.append(card(row, ordinal, i == 0))
        identities[key] = ordinal + 1
    legacy = ''
    if d['legacy']:
        legacy = ('<details class="more boxed" data-key="legacy"><summary>Studio records before per-broadcast tracking</summary>'
                  '<p class="fine">Earlier inbox records can include tests and startup history and were not tied to a broadcast, '
                  'so they stay out of every comparison above.</p><div class="tw"><table><thead><tr><th>Platform</th>'
                  '<th class="r">Studio runs</th><th class="r">Inbox records</th><th class="r">Viewer turns</th>'
                  '<th class="r">Playback confirmed</th></tr></thead><tbody>'
                  + ''.join(f'<tr><td>{escape(r["platform"].title())}</td><td class="r">{r["sessions"]}</td>'
                            f'<td class="r">{r["retained_comments"]}</td><td class="r">{r["viewer_turns"]}</td>'
                            f'<td class="r">{r["played_flags"]}</td></tr>' for r in d['legacy'])
                  + '</tbody></table></div></details>')
    schedule = (f'{e["weekly_broadcasts"]} broadcasts and {e["weekly_hours"]} hours a week across seven two-hour windows. '
                'Times in JST; English slots show the local start in London and New York.')
    return Template(TEMPLATE.read_text()).substitute(
        generated=d['generated_at'], collected=collected, status_state=state,
        status_title=escape(title), status_detail=escape(detail),
        live_until=live['live_until'] if live else '', collected_label='Updated ' + clock(collected),
        headline=tiles, minor=minor, band=band(d), slots=''.join(slot_row(s) for s in d['slots']),
        coverage=slot_coverage(d['slots']), schedule=escape(schedule), legacy=legacy,
        broadcasts=''.join(cards) or '<p class="lede">No instrumented broadcasts yet.</p>',
        footer=f'Studio export {clock(d["generated_at"])} · Site collection {clock(collected)}')
