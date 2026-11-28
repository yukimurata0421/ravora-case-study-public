"""Bind the on-air developer identity and public recall to an authenticated studio turn."""
from functools import wraps
from pathlib import Path
import re
import time

from ravora.context_prompt import context_text
from ravora.developer_identity import viewer_key
from ravora.memory_extraction import historical_reference, session_recap
from ravora.persona import DEVELOPER_SPEAKER
from ravora.ray_memory import character_source_allowed

HOST = viewer_key('live-host', 'host')
RECALL = re.compile(r'覚えて|思い出|前回|以前|前の(?:枠|配信)|この前|さっき|先ほど|これまで|何.{0,6}話した|どんな話')
OLDER_SLOT = re.compile(r'前(?:回|の)(?:の)?(?:枠|配信)|前の放送|(?:last|previous) (?:stream|broadcast|session)', re.I)


def public_turn(inbox, receipt):
    with inbox.store.connect() as db:
        return db.execute("""SELECT t.turn_id, s.received_at, s.rowid
            FROM source_events s JOIN comment_inbox i ON i.receipt=s.id
            JOIN conversation_turns t ON t.source_event_id=s.id AND t.turn_id=i.turn_id
            WHERE i.receipt=? AND i.session=? AND s.stream_id=i.session AND i.source=s.platform
              AND s.platform='live-host' AND s.viewer_id='host' AND s.viewer_key=? AND i.viewer=s.viewer_key
              AND t.viewer=? AND i.status='processing' AND t.status='running'
              AND s.is_deleted=0 AND s.raw_purged_at IS NULL AND s.expires_at>?""",
            (receipt, inbox.session, HOST, inbox.scope + ':' + HOST, time.time())).fetchone()


def past_exchanges(inbox, cutoff, query, history, settings):
    previous_slot = bool(OLDER_SLOT.search(query))
    with inbox.store.connect() as db:
        rows = db.execute("""SELECT t.turn_id,s.id,t.comment,t.reply FROM source_events s
            JOIN conversation_turns t ON t.source_event_id=s.id
            WHERE s.viewer_key=? AND s.platform='live-host' AND s.viewer_id='host' AND t.viewer=?
              AND (s.received_at,s.rowid)<(?,?) AND t.played=1 AND t.status='completed'
              AND t.closed_at<=? AND s.is_deleted=0 AND s.raw_purged_at IS NULL AND s.expires_at>?
              AND (?=0 OR s.stream_id<>?)
            ORDER BY s.received_at DESC,s.rowid DESC LIMIT 32""",
            (HOST, inbox.scope + ':' + HOST, *cutoff, cutoff[0], time.time(), previous_slot, inbox.session)).fetchall()
    terms = set(re.findall(r'[一-龯々]{2,}|[ァ-ヺー]{2,}|[a-z]{3,}', query.lower()))
    ranked = sorted(rows, key=lambda row: sum(term in row[2].lower() for term in terms), reverse=True)
    entries = []
    for turn, event, question, answer in ranked:
        if (('user', question) in history or not all(
                s and len(s) <= 1000 and character_source_allowed(s) for s in (question, answer))):
            continue
        entry = dict(context_id='turn:2:' + turn, tier=2, kind='comment_exchange',
                     source_turn_id=turn, source_event_id=event,
                     text='Host: ' + question[:300] + '\nRay: ' + answer[:400])
        if len(context_text(entries + [entry])) <= settings.context_max_chars:
            entries.append(entry)
        if len(entries) >= min(3, settings.context_max_items):
            break
    return entries


def bind_turn(text, inbox, receipt, name):
    upstream = text.stream

    @wraps(upstream)
    def stream(query, history, **kwargs):
        anchor = public_turn(inbox, receipt) if kwargs.get('live_host') else None
        if not anchor:
            yield from upstream(query, history, **kwargs)
            return
        turn, *cutoff = anchor
        prompt, context, memories = text.prompt, text.context, text.memories
        try:
            text.prompt = (prompt + DEVELOPER_SPEAKER + '\nYour developer and on-air co-host uses the name '
                           + name + '. This is their confirmed name even if an earlier answer was mistaken. '
                           'Use it naturally when asked. Recall only supported on-air facts and exchanges.')
            recall = RECALL.search(query) or historical_reference(query) or session_recap(query)
            if recall and text.s.memory_enabled:
                extra = past_exchanges(inbox, cutoff, query, history, text.s) if text.s.context_enabled else []
                if extra:
                    selected = list(extra)
                    seen = {e['context_id'] for e in extra}
                    for entry in context:
                        if (entry['context_id'] not in seen and len(selected) < text.s.context_max_items
                                and len(context_text(selected + [entry])) <= text.s.context_max_chars):
                            selected.append(entry)
                    text.set_context(selected)
                    inbox.store.record_context_retrievals(turn, extra, call_id=None, prepared=True)
                if not memories:
                    notes = inbox.store.search_memory(HOST, query, scope=inbox.scope, before_turn=turn, limit=3)
                    if not notes and re.search(r'どんな話|何.{0,6}話した|これまで|what do you (?:recall|remember)', query, re.I):
                        notes = inbox.store.search_memory(HOST, scope=inbox.scope, before_turn=turn, limit=3)
                    text.set_memories(notes)
                    inbox.store.record_retrievals(turn, notes, context_prepared=True, strategy='studio_host')
            yield from upstream(query, history, **kwargs)
        finally:
            text.prompt, text.context, text.memories = prompt, context, memories

    text.stream = stream


def configure(program, voice):
    name = voice.get('operator_name', '')
    if name and (not isinstance(name, str) or not re.fullmatch(r'[\w-]{1,32}', name)):
        raise ValueError('INVALID_STUDIO_OPERATOR_NAME')
    if not program.reply_profiles:
        return
    original = getattr(program._profile, 'studio_original_profile', program._profile)
    if not name:
        if hasattr(program._profile, 'studio_original_profile'):
            program._profile = original
        return

    @wraps(original)
    def profile(bridge, comment, language):
        original(bridge, comment, language)
        if comment.get('source') == 'live-host' and comment.get('viewer') == HOST:
            text = bridge.controller.text
            if hasattr(text, 'prompt') and all(text is not p[1] for p in program.reply_profiles.values()):
                bind_turn(text, program.pipeline.inbox, comment['receipt'], name)

    profile.studio_original_profile = original
    program._profile = profile
