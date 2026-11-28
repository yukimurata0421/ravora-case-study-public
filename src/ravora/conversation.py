"""Sentence-at-a-time speech controller, bounded queues, and local SQLite memory."""
from copy import copy
from functools import partial
from ravora import ray_self_disclosure, session_window, viewer_dialogue
import datetime as dt
import json
import logging
from pathlib import Path
import queue
import random
import re
import threading
import time
import uuid
import wave
import sqlite3

from ravora.speech import utterance_word_starts
from ravora.twitch_language_hint import reply_language as clear_reply_language
from .spoken_language import (explicit_unsupported_request, fixed_reply, japanese_reply_parts,
                              normalize_voice_name, readable, RecordedReply)
from ravora.providers import ProviderError, JevReaction, adapters
from .providers import GeminiText
from ravora.database import Connection as closing_connection
from ravora.developer_identity import DEVELOPER_PLATFORMS, viewer_key
from ravora.memory import MemoryRepository
from ravora.memory_worker import MemoryWorker
from ravora.conversation_context import ConversationContext, sensitive
from ravora.memory_extraction import acknowledgment, banter_preference
from ravora.relationships import viewer_details_supplied
from ravora.persona import JAPANESE_QUESTION_WINDOW, asked_listener_in_japanese, japanese_question_direction
from ravora.relationships import Relationships,relationship_text,VIEWER_PLATFORMS
from ravora.viewer_recognition import name_command
from ravora.semantic_memory import SemanticMemory, speech_background
from ravora import twitch_recall, viewer_recall as viewer_recall_search
from ravora.ray_character import CharacterOpening, CharacterRuntime
from .ray_character import CharacterError, PublicDecoder, PublicOutput
from ravora.dialogue_router import DialogueRoute
from ravora.ray_memory import character_source_allowed, claim_hook, played_hooks


class SentenceBuffer:
    def __init__(self,limit=240):self.text='';self.limit=limit

    def feed(self,delta,final=False):
        self.text+=delta;out=[]
        while self.text:
            cut=None
            for match in re.finditer(r'[。！？]+[」』”]*|[.!?]+["\u201d\u2019\']*(?=\s)',self.text):
                prefix=self.text[:match.end()]
                if re.search(r'\b(?:Mr|Mrs|Ms|Dr|Prof|St|vs|etc)\.$',prefix,re.I) or re.search(r'\b(?:[A-Za-z]\.){1,3}$',prefix):continue
                if match.end()<=self.limit:cut=match.end()
                break
            if cut is None and len(self.text)>self.limit:
                cut=self.text.rfind(' ',0,self.limit+1)
                if cut<self.limit//2:cut=self.limit
            if cut is None:
                if not final:break
                cut=len(self.text)
            sentence=self.text[:cut].strip();self.text=self.text[cut:].lstrip()
            if sentence:out.append(sentence)
        return out


class MemoryStore(Relationships,ConversationContext,SemanticMemory,MemoryRepository):
    scheduled_budget = False

    window = None

    def remove_source(self, db, receipt, now):
        super().remove_source(db,receipt,now)
        if self.window:
            self.window.invalidate()

    def delete_memory(self, key, *, reason='user_request'):
        super().delete_memory(key, reason=reason)
        if self.window:
            self.window.invalidate()

    def start(self,turn_id,viewer,comment,*,developer_id=None,window_binding=None):
        """Start a turn and identify the configured developer from its bound input."""
        now=time.time()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            source=db.execute('''SELECT s.id,s.is_deleted,s.raw_purged_at,i.session,s.stream_id,s.received_at,s.viewer_key
                FROM source_events s JOIN comment_inbox i ON i.receipt=s.id WHERE i.turn_id=?''',(turn_id,)).fetchone()
            status='source_removed' if source and source[1] else 'source_expired' if source and source[2] is not None else 'running'
            db.execute('''INSERT INTO conversation_turns(id,turn_id,source_event_id,viewer,comment,status,started,state,created_at,usage_session)
                VALUES(?,?,?,?,?,?,?,?,?,?)''',(turn_id,turn_id,source[0] if source else None,viewer,
                comment if status=='running' else '',status,dt.datetime.now(dt.timezone.utc).isoformat(),
                'SELECTING' if status=='running' else 'CANCELLED',now,source[3] if source else None))
            scope,_,owner=viewer.partition(':')
            platform=owner.partition(':')[0]
            developer_speaker=False
            if window_binding is not None and source and source[6]==owner and platform=='twitch':
                version = db.execute('SELECT version FROM session_evidence_revision WHERE id=1').fetchone()[0]
                window_binding.update(key=(viewer,source[4]), received=source[5],revision=version)
            if (source and developer_id and scope in ('cloud','offline') and platform in DEVELOPER_PLATFORMS
                    and owner==viewer_key(platform,developer_id)):
                developer_speaker=db.execute('''SELECT 1 FROM developer_memory_sources d
                    JOIN source_events s ON s.id=d.event_id
                    WHERE d.event_id=? AND d.owner_key=? AND s.viewer_key=d.owner_key AND s.viewer_id=? AND s.expires_at>?''',
                    (source[0],owner,developer_id,now)).fetchone() is not None
        if status!='running':raise ProviderError('COMMENT_NO_LONGER_AVAILABLE')
        return developer_speaker

    def history(self,viewer,count=4,*,turn_id=None):
        if not count:return []
        owner=viewer.partition(':')[2]
        where="t.viewer=? AND t.played=1 AND t.status='completed'"
        args=[viewer]
        if owner.partition(':')[0] in DEVELOPER_PLATFORMS:
            where+=''' AND EXISTS(SELECT 1 FROM developer_memory_sources d JOIN source_events s ON s.id=d.event_id
                WHERE d.event_id=t.source_event_id AND d.owner_key=? AND s.viewer_key=d.owner_key
                  AND s.is_deleted=0 AND s.raw_purged_at IS NULL AND s.expires_at>?)'''
            args.extend((owner,time.time()))
        if turn_id is not None:
            # Bind to the persisted source, including the owner and stream. A reconnect starts
            # a new conversation; explicitly relevant older reports go through retrieval.
            where+=''' AND EXISTS(SELECT 1 FROM source_events prior JOIN source_events current
                ON current.stream_id=prior.stream_id AND current.viewer_key=prior.viewer_key
                JOIN conversation_turns active ON active.source_event_id=current.id
                WHERE prior.id=t.source_event_id AND active.turn_id=? AND active.viewer=t.viewer
                AND (prior.received_at,prior.rowid)<(current.received_at,current.rowid)
                AND NOT EXISTS(SELECT 1 FROM relationship_events r WHERE r.source_event_id=prior.id
                    AND r.played_at>current.received_at)
                AND prior.is_deleted=0 AND prior.raw_purged_at IS NULL AND prior.expires_at>?)'''
            args.extend((turn_id,time.time()))
        with self.connect() as db:
            rows=db.execute('SELECT t.comment,t.reply FROM conversation_turns t WHERE '+where+
                            ' ORDER BY t.rowid DESC LIMIT ?',(*args,count*4 if turn_id else count)).fetchall()
        if turn_id is not None:
            viewer_chat = owner.partition(':')[0] in VIEWER_PLATFORMS
            rows=[(q, '' if sensitive(a) else a) for q,a in rows if not sensitive(q) and not (viewer_chat and name_command(q)[0])]
            # Keep the latest exchange and reserve space for content-bearing input.
            # A run of backchannels should not evict the speaker's last real question.
            rows=rows[:1]+[pair for pair in rows[1:] if not acknowledgment(pair[0])]
            # Preserve who said what while avoiding repeated assistant lines as style examples.
            seen = set()
            unique = []
            for question, reply in rows[:count]:
                key = ' '.join(reply.casefold().split())
                unique.append((question, '' if key in seen else reply))
                seen.add(key)
            rows = unique
        return [(role,text) for comment,reply in reversed(rows[:count])
                for role,text in [('user',comment),('model',reply)] if text]

    def followup_exchange(self,turn,owner,scope,*,opening=None):
        """Only replayable Ray speech from the exact acknowledged, still-available source."""
        try:
            with self.connect() as db:
                row = db.execute("""SELECT t.comment,t.reply FROM conversation_turns t
                    JOIN source_events s ON s.id=t.source_event_id
                    WHERE t.turn_id=? AND t.viewer=? AND t.played=1 AND t.status='completed'
                    AND s.viewer_key=? AND s.is_deleted=0 AND s.raw_purged_at IS NULL AND s.expires_at>?
                    AND ((? IS NULL AND s.platform!='ray-initiative') OR
                         (s.platform='ray-initiative' AND s.text=? AND t.comment=s.text))""",
                    (turn,scope+':'+owner,owner,time.time(),opening,opening)).fetchone()
        except sqlite3.Error:
            return None
        if row and all(text and len(text)<=1000 and character_source_allowed(text) for text in row):
            return row
        return None

    def followup_history(self,thread,scope):
        if isinstance(thread,CharacterOpening):
            history = []
            for ref in thread.previous[-2:]:
                source = self.followup_exchange(ref.turn,ref.owner,scope,opening=CharacterOpening(ref.topic).message)
                if source:
                    history.extend([('user','Earlier on-air opening by Ray. Use a different premise and first phrase this time.'),
                                    ('model',source[1][:160])])
            return history
        source = self.followup_exchange(thread.turn,thread.owner,scope)
        if not source:
            raise ProviderError('FOLLOWUP_SOURCE_UNAVAILABLE')
        return [('user','Earlier public remark by Ray follows.'),('model',source[1])]

    def can_initiate(self,thread,scope):
        return isinstance(thread,CharacterOpening) or bool(thread and self.followup_exchange(thread.turn,thread.owner,scope))

    def opening_history(self,character,epoch,scope):
        ref = character.opening_reference(epoch) if character else None
        if ref is None:
            return []
        source = self.followup_exchange(ref.turn,ref.owner,scope,opening=CharacterOpening(ref.topic).message)
        return [('user','Ray opened this conversation.'),('model',source[1])] if source else []

    def finish(self,turn_id,reply,status,metrics):
        with self.connect() as db:db.execute("""UPDATE conversation_turns SET
            reply=CASE WHEN status IN ('source_removed','source_expired','cancelled') THEN '' ELSE ? END,
            state=CASE WHEN status IN ('source_removed','source_expired','cancelled') THEN 'CANCELLED' WHEN ?='completed' THEN 'GENERATED' ELSE upper(?) END,
            closed_at=CASE WHEN ?='completed' THEN closed_at ELSE COALESCE(closed_at,?) END,
            status=CASE WHEN status IN ('source_removed','source_expired','cancelled') THEN status ELSE ? END,
            metrics=? WHERE id=?""",(reply,status,status,status,time.time(),status,json.dumps(metrics),turn_id))

    def played(self, turn_id, **kwargs):
        capture = []
        result = self._played(turn_id, capture=capture, **kwargs)
        if result and capture:
            self.window.remember(*capture[0])
        return result

    def _played(self,turn_id,*,capture,scope='offline',memory_enabled=True,memory_ttl_days=30,memory_capacity=16,
               memory_policy=None,memory_model=None,memory_min_confidence=.7,relationship_enabled=True,developer_id=None,program_latency=None,
               unspoken_hooks=None):
        now=time.time()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute("UPDATE conversation_turns SET played=1,state='COMPLETED',closed_at=COALESCE(closed_at,?) WHERE id=? AND status='completed' AND played=0 RETURNING source_event_id,viewer,comment,reply",(now,turn_id)).fetchone()
            if row:
                if row[0] and asked_listener_in_japanese(row[3]) and db.execute('PRAGMA user_version').fetchone()[0]>=24:
                    db.execute("""UPDATE chat_candidates SET asked=1 WHERE receipt=?
                        AND reply_kind='individual' AND lane!='continuation'""",(row[0],))
                if row[1].partition(':')[2].startswith('audience:'):
                    memory_enabled = relationship_enabled = False
                    unspoken_hooks = None
                if row[1].partition(':')[2].partition(':')[0] in VIEWER_PLATFORMS and name_command(row[2])[0]:
                    memory_enabled = False
                if program_latency is not None:
                    db.execute("UPDATE conversation_turns SET metrics=json_set(metrics,'$.live_program',json(?)) WHERE turn_id=?",
                               (json.dumps(program_latency),turn_id))
                if unspoken_hooks is not None:
                    db.execute('SAVEPOINT played_dialogue_hooks')
                    try:
                        played_hooks(db,turn_id,row[0],scope,row[1].partition(':')[2],row[2],row[3],unspoken_hooks,now)
                    except sqlite3.Error:
                        db.execute('ROLLBACK TO played_dialogue_hooks')
                        logging.getLogger(__name__).warning('dialogue_hook_save_unavailable')
                    finally:
                        db.execute('RELEASE played_dialogue_hooks')
                source=db.execute('SELECT platform,stream_id,viewer_key FROM source_events WHERE id=?',(row[0],)).fetchone()
                if self.window and source and source[0]=='twitch' and row[1]==scope+':'+source[2]:
                    ver,generated = db.execute("""SELECT version,json_extract(t.metrics,'$.session_window.revision')
                            FROM session_evidence_revision, conversation_turns t WHERE t.turn_id=?""",(turn_id,)).fetchone()
                    if generated is None or generated==ver:
                        capture.append(((row[1],source[1]), session_window.Exchange(row[0],turn_id,now,row[2],row[3],ver)))
                if source and source[0]=='ray-initiative':memory_enabled=False;relationship_enabled=False
                if relationship_enabled:self.record_relationship_played(db,turn_id,scope,now)
                db.execute("UPDATE utterances SET status='playback_acknowledged',playback_ack_at=COALESCE(playback_ack_at,?) WHERE turn_id=? AND status='claimed'",(now,turn_id))
                if (memory_enabled and developer_id and banter_preference(row[2])
                        and row[1].partition(':')[0]==scope and self.save_banter_feedback(
                            db,row[0],row[1].partition(':')[2],scope,developer_id,memory_ttl_days,now)):
                    return True
                if memory_enabled:
                    options={'policy':memory_policy} if memory_policy else {}
                    self.enqueue_memory_job(db,turn_id,scope=scope,ttl_seconds=memory_ttl_days*86400,capacity=memory_capacity,
                        model=memory_model,min_confidence=memory_min_confidence,**options)
            return bool(db.execute("SELECT 1 FROM conversation_turns WHERE id=? AND status='completed' AND played=1",(turn_id,)).fetchone())

    def cancel_turn(self,turn_id):
        with self.connect() as db:
            db.execute("UPDATE conversation_turns SET status='cancelled',state='CANCELLED',closed_at=COALESCE(closed_at,?) WHERE turn_id=? AND played=0 AND status IN ('running','completed')",(time.time(),turn_id))
            db.execute("UPDATE utterances SET status='cancelled' WHERE turn_id=? AND playback_ack_at IS NULL AND status NOT IN ('source_removed','source_expired')",(turn_id,))

    def reserve(self,characters,limit):
        month=dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).strftime('%Y-%m');key=uuid.uuid4().hex
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            used=db.execute('SELECT COALESCE(SUM(characters),0) FROM speech_usage WHERE month=?',(month,)).fetchone()[0]
            if used+characters>limit:raise ProviderError('LOCAL_MONTHLY_TTS_LIMIT')
            db.execute('INSERT INTO speech_usage VALUES(?,?,?,?)',(key,month,characters,'reserved_or_unknown'))
        return key

    def usage_complete(self,key):
        with self.connect() as db:db.execute("UPDATE speech_usage SET status='completed' WHERE id=?",(key,))

    @staticmethod
    def claim_input_reaction(db,input_id,stage,call_id,now):
        if stage not in ('partial','final'):raise ValueError('INVALID_REACTION_STAGE')
        row=db.execute('SELECT reaction_limit FROM developer_inputs WHERE id=?',(input_id,)).fetchone()
        if not row:return False
        if row[0]==1 and db.execute('SELECT 1 FROM input_reaction_claims WHERE input_id=?',(input_id,)).fetchone():return False
        if not db.execute('INSERT OR IGNORE INTO reaction_stage_claims VALUES(?,?,?,?)',(input_id,stage,call_id,now)).rowcount:return False
        db.execute('INSERT OR IGNORE INTO input_reaction_claims(input_id,stage,call_id,claimed_at) VALUES(?,?,?,?)',(input_id,stage,call_id,now))
        return True


    def prepare_call(self,turn_id,provider,operation,summary,*,model=None,mode='unknown',reaction_input_id=None):
        key=uuid.uuid4().hex;now=time.time()
        with self.connect() as db:
            if reaction_input_id is not None:
                if provider!='jev':raise ValueError('REACTION_CLAIM_REQUIRES_JEV')
                db.execute('BEGIN IMMEDIATE')
                if not db.execute('''SELECT 1 FROM developer_inputs d JOIN conversation_turns t
                    ON t.source_event_id=d.source_event_id WHERE d.id=? AND t.turn_id=?
                    AND d.input_state='accepted' AND t.status='running' ''',(reaction_input_id,turn_id)).fetchone():return None
                if not self.claim_input_reaction(db,reaction_input_id,'final',key,now):return None
            db.execute('INSERT INTO provider_calls VALUES(?,?,?,?,?,?,?,?,?,?)',(key,turn_id,provider,operation,'prepared',0,now,now,json.dumps(summary),None))
            db.execute('''INSERT INTO api_usage(id,turn_id,provider,model,mode,characters,status,created_at)
                VALUES(?,?,?,?,?,?,'prepared',?)''',(key,turn_id,provider,model,mode,summary.get('characters'),now))
        return key

    def call_state(self,key,state,*,attempted=False,code=None):
        with self.connect() as db:self._call_state(db,key,state,attempted=attempted,code=code)

    @staticmethod
    def _call_state(db,key,state,*,attempted=False,code=None):
        now=time.time()
        db.execute('UPDATE provider_calls SET state=?,attempted=MAX(attempted,?),updated=?,code=? WHERE id=?',(state,int(attempted),now,code,key))
        db.execute('''UPDATE api_usage SET status=?,attempted=MAX(attempted,?),
            latency_ms=CASE WHEN ? IN ('accepted','failed','outcome_unknown','discarded_after_cancel')
                THEN MAX(0,(?-created_at)*1000) ELSE latency_ms END WHERE id=?''',(state,int(attempted),state,now,key))

    def dispatch_scheduled_reaction(self,key):
        from ravora.scheduled_budget import reserve_auxiliary
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            reserve_auxiliary(db,key,time.time())
            self._call_state(db,key,'dispatched',attempted=True)

    def dispatch_budgeted_reply(self,key,input_bytes,output_tokens,attempts,monthly_usd):
        """Reserve estimated Flash-Lite cost in integer microdollars, including concurrent hedges."""
        # Text-only. Allow one input token per UTF-8 byte plus protocol overhead.
        # Standard price per million tokens: $0.30 input and $2.50 output (including thinking).
        per_attempt=(3*(input_bytes+256)+9)//10+(5*output_tokens+1)//2
        reservation=attempts*per_attempt
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            month=dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).strftime('%Y-%m')
            call=db.execute("""SELECT 1 FROM provider_calls p JOIN api_usage a ON a.id=p.id WHERE p.id=?
                AND p.provider='gemini' AND p.operation IN ('generate','stream_generate') AND p.state='prepared'
                AND p.attempted=0 AND a.model='gemini-3.5-flash-lite' AND a.mode='cloud'""",(key,)).fetchone()
            if not call:raise ProviderError('INVALID_REPLY_BUDGET_RESERVATION')
            # Old attempted calls have no reservation. Carry at least two cents per call,
            # or its recorded usage with all possible attempts, whichever is larger.
            used=db.execute("""SELECT COALESCE(SUM(COALESCE(json_extract(p.summary,'$.reply_reserved_microusd'),
                MAX(20000,(CASE p.operation WHEN 'stream_generate' THEN 3 ELSE 1 END)*
                    ((3*COALESCE(a.input_tokens,0)+9)/10+(5*MAX(400,COALESCE(a.output_tokens,0)+COALESCE(a.thought_tokens,0))+1)/2)))),0)
                FROM provider_calls p JOIN api_usage a ON a.id=p.id WHERE p.provider='gemini'
                AND p.operation IN ('generate','stream_generate') AND p.attempted=1
                AND COALESCE(json_extract(p.summary,'$.reply_budget_month'),
                    strftime('%Y-%m',a.created_at,'unixepoch','+9 hours'))=?""",(month,)).fetchone()[0]
            if used+reservation>monthly_usd*1_000_000:raise ProviderError('LOCAL_MONTHLY_REPLY_LLM_LIMIT')
            db.execute("""UPDATE provider_calls SET summary=json_set(summary,'$.reply_budget_month',?,
                '$.reply_reserved_microusd',?) WHERE id=?""",(month,reservation,key))
            self._call_state(db,key,'dispatched',attempted=True)

    def call_usage(self,key,usage):
        values=[]
        for name in ('input_tokens','output_tokens','total_tokens','thought_tokens','cached_input_tokens'):
            value=usage.get(name)
            values.append(value if type(value) is int and value>=0 else None)
        model=usage.get('response_model')
        if not isinstance(model,str) or not re.fullmatch(r'[a-zA-Z0-9._/-]{1,160}',model):model=None
        with self.connect() as db:
            db.execute('''UPDATE api_usage SET input_tokens=COALESCE(?,input_tokens),output_tokens=COALESCE(?,output_tokens),
                total_tokens=COALESCE(?,total_tokens),thought_tokens=COALESCE(?,thought_tokens),
                cached_input_tokens=COALESCE(?,cached_input_tokens),response_model=COALESCE(?,response_model) WHERE id=?''',(*values,model,key))

    def reply_cache_savings(self,session=None):
        """Input-only estimate for measured Flash-Lite replies in one developer connection."""
        with self.connect() as db:
            if session is None:
                latest=db.execute("SELECT session FROM developer_inputs WHERE mode='cloud' ORDER BY created_at DESC LIMIT 1").fetchone()
                session=latest[0] if latest else None
            calls,measured,inputs,hits=db.execute("""
                WITH replies AS (
                    SELECT a.input_tokens, a.cached_input_tokens,
                           a.input_tokens>0 AND a.cached_input_tokens BETWEEN 0 AND a.input_tokens AS measured
                    FROM developer_inputs d JOIN conversation_turns t ON t.source_event_id=d.source_event_id
                    JOIN api_usage a ON a.turn_id=t.turn_id JOIN provider_calls p ON p.id=a.id
                    WHERE d.session=? AND d.mode='cloud' AND a.mode='cloud' AND a.attempted=1
                      AND a.provider='gemini' AND a.model='gemini-3.5-flash-lite'
                      AND p.operation IN ('generate','stream_generate')
                )
                SELECT COUNT(*), COUNT(*) FILTER (WHERE measured),
                       SUM(input_tokens) FILTER (WHERE measured), SUM(cached_input_tokens) FILTER (WHERE measured)
                FROM replies""",(session,)).fetchone()
        # Gemini Developer API Standard text input, USD per million tokens, 2026-11-21.
        # Use integer nanodollars until presentation. One usage row covers the selected stream.
        return {'session':session,'model':'gemini-3.5-flash-lite','calls':calls,'measured_calls':measured,
                'unknown_calls':calls-measured,'input_tokens':inputs,'cached_input_tokens':hits,
                'uncached_baseline_usd':inputs*300/1e9 if measured else None,
                'estimated_input_usd':((inputs-hits)*300+hits*30)/1e9 if measured else None,
                'estimated_savings_usd':hits*270/1e9 if measured else None}

    def prepare_memory_call(self,job):
        key=self.prepare_call(job['turn_id'],'gemini','extract_memory',
            {'comment_characters':len(job['text']),'policy':job['policy']},model=job['model'],mode=job['scope'])
        with self.connect() as db:
            row=db.execute("""UPDATE memory_jobs SET api_call_id=?,request_state='prepared'
                WHERE turn_id=? AND status='running' AND claim_token=? AND lease_until>? AND request_attempted=0""",
                (key,job['turn_id'],job['claim_token'],time.time()))
        if not row.rowcount:self.call_state(key,'cancelled_before_send');return None
        return key

    def dispatch_memory_call(self,job,key):
        now=time.time()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute("""UPDATE memory_jobs SET request_state='dispatched',request_attempted=1
                WHERE turn_id=? AND api_call_id=? AND status='running' AND claim_token=? AND lease_until>?
                  AND request_attempted=0 AND EXISTS(SELECT 1 FROM source_events s JOIN conversation_turns t
                    ON t.source_event_id=s.id WHERE s.id=memory_jobs.source_event_id AND t.turn_id=memory_jobs.turn_id
                    AND s.text=? AND s.is_deleted=0 AND s.raw_purged_at IS NULL AND s.expires_at>?
                    AND t.played=1 AND t.status='completed' AND t.viewer=memory_jobs.scope||':'||s.viewer_key)""",
                (job['turn_id'],key,job['claim_token'],now,job['text'],now))
            if not row.rowcount:return False
            if self.scheduled_budget:
                from ravora.scheduled_budget import reserve_auxiliary
                reserve_auxiliary(db,key,now)
            db.execute("UPDATE provider_calls SET state='dispatched',attempted=1,updated=? WHERE id=?",(now,key))
            db.execute("UPDATE api_usage SET status='dispatched',attempted=1 WHERE id=?",(key,))
        return True

    def memory_call_state(self,job,key,state,*,code=None):
        self.call_state(key,state,code=code)
        with self.connect() as db:
            db.execute('UPDATE memory_jobs SET request_state=? WHERE turn_id=? AND api_call_id=?',(state,job['turn_id'],key))


class SpeechController:
    checks_spoken_language = True
    pins_reply_language = True

    def __init__(self,settings,store,output,*,providers=None,clock=time.monotonic,memory_extractor=None):
        self.s=settings;self.store=store;self.output=Path(output);self.clock=clock
        self.text,self.reaction,self.speech=providers or adapters(settings)
        if settings.twitch_recall_mode=='session' and settings.memory_enabled and (not store.window or store.window.closed):
            store.window = session_window.SessionWindow()
        self.program_pcm = None
        self.initiative_audio_limit = None
        self.language_voices = None
        self.reply_playable = None
        self.memory_worker=MemoryWorker(store,settings,extractor=memory_extractor)
        self.character=CharacterRuntime(idle_seconds=settings.character_idle_seconds,max_initiatives=settings.character_max_initiatives) if settings.character_enabled else None

    def run(self,comment,viewer,turn_id,emit,cancel,*,followup=None):
        start=self.clock();deadline=start+self.s.turn_timeout
        initiative=viewer.startswith('ray-initiative:')
        brief = initiative and self.initiative_audio_limit is not None
        character_epoch = None
        if self.character:
            with self.character.lock:
                if cancel.is_set():
                    return {}
                character_epoch = self.character.begin(turn_id,comment,initiative=initiative,viewer=viewer,opening=followup)
        metrics={'mode':self.s.mode,'first_text_ms':None,'first_sentence_ms':None,'first_audio_ready_ms':None,'reaction_ms':None,'chunks':0,'characters':0,'provider_requests':0}
        speech = self.speech
        voices = self.language_voices if self.character and isinstance(self.text, GeminiText) and not initiative else None
        fallback = self.s.language[:2]
        threshold = self.s.tts_language_confidence
        reply=[];sentences=queue.Queue(maxsize=2);producer_error=[];worker_stop=threading.Event();aux=[];metrics_lock=threading.Lock()
        def current():return not cancel.is_set() and not worker_stop.is_set() and self.clock()<deadline
        def publish(kind,**data):
            if current():emit({'kind':kind,'elapsed_ms':round((self.clock()-start)*1000,2),**data})
        def put(value):
            while current():
                try:sentences.put(value,timeout=.05);return
                except queue.Full:pass
        live_host = viewer == viewer_key('live-host','host')
        memory_viewer=self.s.mode+':'+viewer
        session_mode = bool(self.s.twitch_recall_mode=='session' and getattr(self.store,'window',None) and
                            self.s.memory_enabled and viewer.startswith('twitch:'))
        binding = {}
        developer_speaker=self.store.start(turn_id,memory_viewer,comment,developer_id=self.s.developer_id,
                                           window_binding=binding if session_mode else None)
        ram_rows = ()
        def read_window(*, touch=False):
            if not binding:
                return ()
            return tuple(r for r in self.store.window.previous(binding['key'],binding['received'],touch=touch)
                         if r.revision==binding['revision'])
        if session_mode:
            ram_rows = read_window(touch=True)
            metrics['session_window'] = dict(hit=bool(ram_rows), input_pairs=len(ram_rows),revision=binding.get('revision'))
        if self.character:self.memory_worker.wake()
        def prior(count):
            if session_mode:
                return session_window.messages(ram_rows,count)
            return self.store.history(memory_viewer,count,turn_id=turn_id)
        history=prior(self.s.memory_turns)
        recent_for_route = history if self.s.memory_turns >= 4 or not self.character else prior(4)
        recent_for_route = recent_for_route + self.store.opening_history(self.character,character_epoch,self.s.mode)
        route = self.character.route(character_epoch, comment, recent_for_route,
                                     initiative=initiative) if self.character else DialogueRoute()
        audience = viewer.startswith('audience:')
        rag = route.allow_retrieval(metrics, comment) and not audience
        memory_wait=self.clock()
        viewer_recall = (self.s.twitch_recall_mode if viewer.startswith('twitch:') and self.s.memory_enabled else 'off')
        use_viewer_recall = viewer_recall != 'off'
        semantic_enabled=rag and self.s.memory_enabled and self.s.semantic_memory_mode!='off' and not use_viewer_recall
        if semantic_enabled:self.memory_worker.wake()
        # A live reply uses committed memories while the remote extraction runs in the background.
        if rag and not (use_viewer_recall or semantic_enabled or self.memory_worker.live_extraction) and not self.memory_worker.wait_for(viewer,cancel=cancel):
            metrics['total_ms']=round((self.clock()-start)*1000,2)
            self.store.finish(turn_id,'','failed',metrics)
            if not cancel.is_set():emit({'kind':'error','status':'failed','code':'MEMORY_WORKER_NOT_READY','metrics':metrics})
            return metrics
        metrics['memory_wait_ms']=round((self.clock()-memory_wait)*1000,2)
        semantic_done=threading.Event();semantic_result={};generation_decision={}
        if use_viewer_recall:
            lookup = twitch_recall.candidates
            if session_mode:
                lookup = partial(viewer_recall_search.candidates,multiple=True)
            begin = self.clock()
            eligible = rag and twitch_recall.asks_for_memory(comment)
            if session_mode and rag and viewer_recall_search.COMPARISON.search(comment) and (
                    not ram_rows or viewer_recall_search.has_named_topics(comment)):
                from ravora.gemini_memory import source_block_reason
                eligible = eligible or not source_block_reason(comment)
            if session_mode and ram_rows and viewer_recall_search.recent_reference(comment):
                eligible = False
            candidates = lookup(self.store, viewer, comment, self.s, turn_id) if eligible else []
            recall = metrics['twitch_recall'] = dict(mode=viewer_recall, candidate_count=len(candidates),
                sqlite_ms=round((self.clock()-begin)*1000, 3) if eligible else 0, wait_ms=0,
                outcome='pending' if candidates else 'empty' if eligible else 'skipped', prompt_items=0)
            memories = []
        else:
            memories,_=self.store.conversation_memories(viewer,comment,self.s) if rag else ([], 'dialogue_gate')
            candidates=self.store.episode_candidates(viewer,comment,scope=self.s.mode,
                limit=self.s.semantic_candidate_limit,max_chars=self.s.semantic_max_chars) if semantic_enabled else []
        wait_for_selection = bool(candidates) and (viewer_recall == 'jev' if use_viewer_recall else self.s.semantic_memory_mode=='jev')
        previous_message=self.store.semantic_previous_message(viewer,scope=self.s.mode,turn_id=turn_id) if candidates and not use_viewer_recall else ''
        topic_evidence = None
        generation_history=history
        def prepare_context(key, *, repair=False):
            nonlocal generation_history, ram_rows, topic_evidence
            # Refresh before dispatch: a removed source cannot survive in the
            # adapter's context from an earlier turn.
            if use_viewer_recall:
                notes, method = [], 'twitch_' + viewer_recall
                selected = candidates[0]['memory_id'] if candidates and viewer_recall == 'sqlite' else (generation_decision.get('value') or {}).get('memory_id')
                if session_mode:
                    fresh = lookup(self.store,viewer,comment,self.s,turn_id) if candidates else []
                    notes = list(candidates) if fresh==candidates else []
                    if candidates:
                        recall['outcome'] = 'selected' if notes else 'no_longer_eligible'
                elif selected:
                    fresh = lookup(self.store, viewer, comment, self.s, turn_id)
                    notes = [note for note in fresh if note['memory_id'] == selected and note in candidates]
                    recall['outcome'] = 'selected' if notes else 'no_longer_eligible'
                recall['prompt_items'] = len(notes)
            else:
                notes,method=self.store.conversation_memories(viewer,comment,self.s,judgment=generation_decision.get('value')) if rag else ([], 'dialogue_gate')
                notes = self.store.prior_viewer_notes(notes,viewer,self.s.mode,turn_id)
            if rag and metrics['knowledge_rag'] != 'explicit_recall' and developer_speaker and isinstance(self.text,GeminiText):
                notes+=self.store.developer_banter_memory(viewer,self.s)
            if isinstance(self.text,GeminiText):
                self.text.set_retrieval_strategy(method,reaction=generation_decision.get('reaction'))
            elif hasattr(self.text,'set_retrieval_strategy'):self.text.set_retrieval_strategy(method)
            supported=hasattr(self.text,'set_memories')
            if supported:self.text.set_memories(notes)
            self.store.record_retrievals(turn_id,notes,context_prepared=supported,strategy=method,call_id=key)
            if use_viewer_recall and candidates:
                decision = generation_decision.get('value') if viewer_recall == 'jev' else {'memory_id':selected}
                if session_mode:
                    decision = {'memory_ids':[n['memory_id'] for n in notes]}
                self.store.record_semantic_retrieval(turn_id,comment,candidates,decision,used=bool(notes and supported))
            if semantic_enabled:
                self.store.record_semantic_retrieval(turn_id,comment,candidates,generation_decision.get('value'),used=generation_decision.get('value') is not None)
            context=self.store.recall_context(viewer,comment,scope=self.s.mode,developer_id=self.s.developer_id,
                limit=self.s.context_max_items,max_chars=self.s.context_max_chars,turn_id=turn_id) if not session_mode and rag and self.s.memory_enabled and self.s.context_enabled else []
            pace_questions=self.s.language=='ja-JP' and not initiative
            if session_mode:
                began = time.perf_counter()
                ram_rows = session_window.validated(self.store,binding.get('key'),read_window())
                metrics['session_window'].update(sent_pairs=len(ram_rows), validate_ms=round((time.perf_counter()-began)*1000,3))
            recent=prior(self.s.memory_turns)
            if pace_questions:
                cadence_history=recent if self.s.memory_turns>=JAPANESE_QUESTION_WINDOW else prior(JAPANESE_QUESTION_WINDOW)
                questions=[asked_listener_in_japanese(line) for role,line in cadence_history if role=='model']
                generation_decision['japanese_turn']=japanese_question_direction(questions[-JAPANESE_QUESTION_WINDOW:], comment)
            generation_history,context=speech_background(self.s,comment,bool(candidates) and not use_viewer_recall,generation_decision.get('value'),
                recent,context,selected=notes)
            own_detail = viewer_details_supplied(generation_history,context,notes)
            if followup is not None:
                generation_history = self.store.followup_history(followup,self.s.mode)
                context = []
            else:
                generation_history = generation_history + self.store.opening_history(self.character,character_epoch,self.s.mode)
            context_supported=hasattr(self.text,'set_context')
            if context_supported:self.text.set_context(context)
            self.store.record_context_retrievals(turn_id,context,call_id=key,prepared=context_supported)
            begin=time.monotonic()
            background=self.store.relationship_context(viewer,scope=self.s.mode,turn_id=turn_id) if not audience and self.s.memory_enabled and self.s.relationship_enabled else None
            if background is not None:
                background['viewer_details_supplied'] = own_detail
            relationship_supported=hasattr(self.text,'set_relationship')
            if relationship_supported:self.text.set_relationship(background)
            metrics['relationship_lookup_ms']=round((time.monotonic()-begin)*1000,3)
            metrics['relationship_context_chars']=len(relationship_text(background)) if background else 0
            metrics['relationship_context_prepared']=bool(background and relationship_supported)
            metrics['context_tiers']=[e['tier'] for e in context];metrics['context_items']=len(context)
            metrics['memory_ids']=[m['memory_id'] for m in notes];metrics['memory_context_prepared']=supported
            metrics['memory_strategy']=method
            character_begin=time.monotonic()
            memories=self.memory_worker.character_memories(viewer,route,comment) if self.character and not audience else ()
            if not current():raise ProviderError('TURN_CANCELLED')
            if repair:
                return  # Sources have been revalidated. Keep the plan and hook already claimed by this turn.
            hook=claim_hook(self.store,self.s.mode,viewer,turn_id,comment,route.unspoken_hooks) if route.dialogue_policy=='SHIFT_TO_HOOK' else ''
            plan=self.character.plan(comment,memories,background,initiative=initiative,epoch=character_epoch,
                                     host_speaking=live_host or developer_speaker,hook=hook) if self.character else None
            dialogue_mode = ''
            disclosure, public_lines = '', ()
            labels, exchange_count = (), 0
            if session_mode and voices and plan is not None:
                disclosure, public_lines = ray_self_disclosure.disclosure(comment, recent, self.s.language[:2])
                dialogue_mode = 'self_disclosure' if disclosure else viewer_dialogue.kind(comment)
                if not dialogue_mode and viewer_dialogue.asks_choice_reason(comment, recent):
                    dialogue_mode = 'choice_reason'
                if not public_lines:
                    public_lines = viewer_dialogue.capability_choices(dialogue_mode, comment, recent, self.s.language[:2])
                if dialogue_mode in ('day','shift','story','joke','wish','recap') and ram_rows and self.s.memory_turns:
                    topic_evidence = viewer_dialogue.session_topics(self.store, binding, turn_id)
                    labels, exchange_count = topic_evidence
            if isinstance(self.text,GeminiText):
                self.text.grounded_choices = (public_lines or viewer_dialogue.grounded_choices(
                    dialogue_mode, comment, recent, labels, self.s.language[:2])
                    if session_mode and voices and plan is not None else ())
                if public_lines and not eligible:
                    # Both sources above accept whole authored questions. An entirely
                    # Japanese question such as たとえば？ also identifies its language.
                    language = clear_reply_language(comment)
                    if not language and re.fullmatch(r'[ぁ-ゖァ-ヺ一-龯々ー、。？！?! 　]+', comment) and re.search(r'[ぁ-ゖ]', comment):
                        language = 'ja'
                    if language == self.s.language[:2]:
                        generation_decision['public_line'] = random.choice(public_lines)
            if dialogue_mode:
                instruction = disclosure or viewer_dialogue.direction(dialogue_mode, labels, exchange_count, viewer_dialogue.topics_in(comment), self.s.language[:2])
                if dialogue_mode == 'wish' and len(recent) >= 2 and viewer_dialogue.kind(recent[-2][1]) == 'wish':
                    instruction = 'Continue the concrete wish in your immediately previous answer; give one specific first step.'
                plan = viewer_dialogue.apply_plan(plan, dialogue_mode, instruction)
                metrics['viewer_dialogue'] = dict(mode=dialogue_mode, topics=labels)
            if hasattr(self.text,'set_character_plan'):self.text.set_character_plan(plan)
            generation_decision['character_plan']=plan
            self.memory_worker.character_referenced(memories)
            metrics['character_prepare_ms']=round((time.monotonic()-character_begin)*1000,3)
        models={'gemini':self.s.gemini_model,'jev':self.s.jev_model,'tts':getattr(self.speech,'voice',self.s.voice)} if self.s.mode=='cloud' else {'gemini':'english-fixture','jev':'fixture-reaction','tts':'flite-slt'}
        publish('request_prepared',comment_characters=len(comment),history_turns=len(history)//2,memory_items=len(memories),mode=self.s.mode)
        if self.s.mode=='cloud' and not self.s.api_enabled:
            for provider,operation in ([('gemini','generate')] if initiative else [('gemini','generate'),('jev','reaction')]):
                key=self.store.prepare_call(turn_id,provider,operation,{'comment_characters':len(comment),'history_turns':len(history)//2},model=models[provider],mode=self.s.mode)
                if provider=='gemini':prepare_context(key)
                self.store.call_state(key,'held',code='API_DISABLED')
            metrics['total_ms']=round((self.clock()-start)*1000,2)
            self.store.finish(turn_id,'','held',metrics)
            if not cancel.is_set():emit({'kind':'held','status':'held','code':'API_DISABLED','metrics':metrics})
            return metrics

        def prepared(provider,operation,summary):
            local = provider == 'tts' and getattr(speech, 'local', False)
            model = getattr(speech, 'voice', models['tts']) if provider == 'tts' else models[provider]
            key=self.store.prepare_call(turn_id,provider,operation,summary,model=model,mode='local' if local else self.s.mode)
            publish('call_prepared',call_id=key,provider=provider,operation=operation)
            return key
        def dispatch(key,provider,*,repair=False):
            if not current():self.store.call_state(key,'cancelled_before_send');return False
            if provider=='gemini':
                prepare_context(key,repair=repair)
                if not repair and 'public_line' in generation_decision:
                    self.store.call_state(key, 'held', code='AUTHORED_PUBLIC_REPLY')
                    publish('call_held',call_id=key,provider=provider,code='AUTHORED_PUBLIC_REPLY')
                    return True
            local = provider == 'tts' and getattr(speech, 'local', False)
            remote = self.s.mode == 'cloud' and not local
            if provider=='gemini' and remote and self.s.monthly_reply_llm_usd:
                return True  # GeminiText supplies the assembled payload size immediately before send.
            if provider=='jev' and remote and self.store.scheduled_budget:
                self.store.dispatch_scheduled_reaction(key)
            else:
                self.store.call_state(key,'dispatched' if remote or local else 'simulated',attempted=remote or local)
            if remote:
                with metrics_lock:metrics['provider_requests']+=1
            publish('call_dispatched' if remote or local else 'call_simulated',call_id=key,provider=provider)
            return True
        def received(key,provider):
            self.store.call_state(key,'response_received');publish('call_response_received',call_id=key,provider=provider)
        def accepted(key,provider):
            state='accepted' if current() else 'discarded_after_cancel'
            self.store.call_state(key,state);publish('call_'+state,call_id=key,provider=provider)
        def failed(key,provider,exc):
            code=str(exc) if isinstance(exc,ProviderError) else 'PROVIDER_STAGE_FAILED'
            uncertain='CONNECTION_FAILED' in code or 'DEADLINE' in code or 'STREAM_INTERRUPTED' in code
            state='outcome_unknown' if uncertain else 'failed'
            adapter={'gemini':self.text,'jev':self.reaction,'tts':speech}[provider]
            self.store.call_usage(key,getattr(adapter,'usage',{}))
            self.store.call_state(key,state,code=code);publish('call_failed',call_id=key,provider=provider,code=code,state=state)
            return code

        def generate():
            nonlocal speech
            buffer=SentenceBuffer(self.s.sentence_chars);characters=0;key=None;got_response=False

            def select_fixed(action, target, *, verify_memory=False):
                nonlocal speech
                text, recorded_voice = fixed_reply(action, target, speech)
                output = PublicOutput(text)
                if verify_memory:
                    checked = checked_memory_output(output, target)
                    if checked is not output:
                        return checked
                speech = recorded_voice
                metrics['speech_language_guard'].update(outcome=action, recorded=isinstance(speech, RecordedReply))
                return output

            try:
                if voices and explicit_unsupported_request(comment):
                    if not current():return
                    speech = voices[fallback]
                    metrics['tts_route'] = {'input_language':'und', 'confidence':None, 'threshold':threshold,
                                            'fallback_language':fallback, 'language':fallback, 'voice':speech.voice,
                                            'decision':'explicit_unsupported_request'}
                    metrics['speech_language_guard'] = {'language':fallback,'action':'available'}
                    sentence = select_fixed('available', fallback)
                    metrics['first_sentence_ms'] = metrics['public_output_ready_ms'] = round((self.clock()-start)*1000,2)
                    publish('character_action',**sentence.action())
                    put(sentence)
                    return
                if wait_for_selection:
                    wait_started=time.monotonic();deadline_at=wait_started+self.s.semantic_wait_ms/1000
                    semantic_done.wait(self.s.semantic_wait_ms/1000)
                    if semantic_result.get('ready_at',float('inf'))<=deadline_at:
                        generation_decision.update(value=semantic_result['value'],reaction=semantic_result['reaction'])
                    metrics['semantic_wait_ms']=round((time.monotonic()-wait_started)*1000,3)
                    if use_viewer_recall:
                        recall['wait_ms'] = metrics['semantic_wait_ms']
                        value = generation_decision.get('value') or {}
                        if value.get('reference') == 'unavailable' or (not value and semantic_done.is_set() and not semantic_result):
                            recall['outcome'] = 'unavailable'
                        else:
                            recall['outcome'] = 'abstained' if value else 'deadline'

                key=prepared('gemini','generate',{'comment_characters':len(comment),'history_turns':len(history)//2})
                if not dispatch(key,'gemini'):return
                speaker={'developer_speaker':developer_speaker} if isinstance(self.text,GeminiText) else {}
                if isinstance(self.text,GeminiText) and 'japanese_turn' in generation_decision:
                    speaker['japanese_turn']=generation_decision['japanese_turn']
                if brief and isinstance(self.text,GeminiText):
                    speaker['brief_opening'] = True
                    if getattr(followup,'greeting',False):
                        speaker['greet_room'] = True
                if isinstance(self.text,GeminiText) and live_host:
                    speaker['live_host']=True
                if self.s.mode=='cloud' and self.s.monthly_reply_llm_usd:
                    if not isinstance(self.text,GeminiText):raise ProviderError('REPLY_LLM_BUDGET_MODEL_NOT_PRICED')
                    def before_send(size,output,attempts):
                        if not current():raise ProviderError('TURN_CANCELLED')
                        self.store.dispatch_budgeted_reply(key,size,output,attempts,self.s.monthly_reply_llm_usd)
                        with metrics_lock:metrics['provider_requests']+=1
                        publish('call_dispatched',call_id=key,provider='gemini')
                    speaker['before_send']=before_send
                if voices:
                    speaker['language_route'] = (fallback, threshold)
                    pinned_language = clear_reply_language(comment)
                    if pinned_language:
                        speaker['reply_language'] = pinned_language
                    if ('Kokoro' in voices['en'].voice and
                            re.search(r'\b(?:tts|voice|engine|qwen[\d.]*|kokoro)\b|音声(?:合成|モデル)', comment, re.I)):
                        speaker['english_voice_fact'] = True
                plan=generation_decision.get('character_plan')
                decoder=PublicDecoder(structured=bool(getattr(self.text.stream,'ray_public_output',False)),buffer=buffer,plan=plan,limit=self.s.max_reply_chars,language_routing=bool(voices), expand_wire=getattr(self.text,'expand_studio_output',None)) if self.character else None
                pending = None
                def enqueue(sentence):
                    nonlocal pending, speech
                    if session_mode and eligible and recall.get('clarified'):
                        return
                    if metrics['first_sentence_ms'] is None:
                        metrics['first_sentence_ms']=round((self.clock()-start)*1000,2)
                    if not isinstance(sentence,PublicOutput):
                        line = checked_memory_output(sentence, fallback)
                        if line is not None:
                            put(line)
                        return
                    metrics.setdefault('public_output_ready_ms',round((self.clock()-start)*1000,2))
                    if voices:
                        detected, confidence = decoder.language_result
                        use_model = detected in voices and confidence >= threshold
                        language = pinned_language or (detected if use_model else fallback)
                        speech = voices[language]
                        metrics['tts_route'] = {'input_language': detected, 'confidence': confidence,
                                                'threshold': threshold, 'fallback_language': fallback,
                                                'language': language, 'voice': speech.voice,
                                                'decision': 'local_clear' if pinned_language else ('model' if use_model else 'fallback')}
                    target = language if voices else fallback
                    if not (voices and decoder.unsupported_language_requested):
                        sentence = checked_memory_output(sentence, target)
                        if sentence is None:
                            return
                    if voices or (initiative and getattr(speech, 'local', False)):
                        if voices and decoder.unsupported_language_requested:
                            pending = ('available', target)
                            return
                        if voices:
                            sentence = normalize_name(sentence, target)
                        if not readable(sentence.speech, target):
                            pending = ('repair', target)
                            return
                    emit_speech(sentence)

                def normalize_name(sentence, target):
                    text = normalize_voice_name(sentence.speech, target)
                    if text == sentence.speech:
                        return sentence
                    metrics['tts_name_normalized'] = True
                    return PublicOutput(text, sentence.expression, sentence.motion)

                def checked_memory_output(sentence, language):
                    nonlocal speech
                    if session_mode and ram_rows:
                        live_rows = read_window()
                        if not all(r in live_rows for r in ram_rows) or session_window.validated(self.store,binding['key'],ram_rows)!=ram_rows:
                            raise ProviderError('VIEWER_MEMORY_SOURCE_REVOKED')
                    if topic_evidence is not None and viewer_dialogue.session_topics(self.store, binding, turn_id) != topic_evidence:
                        raise ProviderError('VIEWER_MEMORY_SOURCE_REVOKED')
                    if session_mode and eligible:
                        began = time.monotonic()
                        answered = semantic_done.wait(.020)
                        recall['verify_wait_ms'] = round((time.monotonic()-began)*1000,3)
                        decision = semantic_result.get('value') or {}
                        ids = [n['memory_id'] for n in candidates]
                        if len(ids)>1:
                            confirmed = decision.get('reference')=='combined' and decision.get('memory_ids')==ids
                        elif ids:
                            confirmed = decision.get('memory_id')==ids[0]
                        else:
                            confirmed = decision.get('reference')=='none'
                        if not answered or decision.get('reference') in (None, 'unavailable') or metrics.get('memory_ids') != ids:
                            raise ProviderError('VIEWER_MEMORY_REPLY_HELD')
                        fresh = lookup(self.store,viewer,comment,self.s,turn_id) if ids else []
                        if not all(n in fresh for n in candidates):
                            raise ProviderError('VIEWER_MEMORY_SOURCE_REVOKED')
                        if recall.get('clarified'):
                            return None
                        if not confirmed or (not ids and not ram_rows):
                            reference = decision.get('reference')
                            reason = {'none': 'no_supporting_report', 'uncertain': 'low_confidence'}.get(reference, 'different_evidence')
                            kind = viewer_recall_search.clarification_kind(comment)
                            line, speech = fixed_reply(kind, language, speech)
                            recall.update(clarified=True, clarification_reason=reason,
                                          clarification_kind=kind, recorded=isinstance(speech, RecordedReply))
                            return PublicOutput(line) if isinstance(sentence, PublicOutput) else line
                    return sentence

                def emit_speech(sentence):
                    nonlocal characters
                    characters+=len(sentence.speech)
                    if characters>self.s.max_reply_chars:raise ProviderError('REPLY_CHARACTER_LIMIT')
                    pieces = [sentence.speech]
                    if (viewer.startswith('twitch:') and self.program_pcm and self.s.max_chunks >= 2
                            and not isinstance(speech, RecordedReply)
                            and metrics.get('tts_route', {}).get('language') == 'ja'):
                        # The producer has validated the full reply. The consumer numbers and
                        # publishes each sentence while the previous one plays.
                        parts = japanese_reply_parts(sentence.speech)
                        if not hasattr(speech, 'can_release_lead') or speech.can_release_lead(parts):
                            pieces = parts
                    if initiative and self.program_pcm and decoder.structured:
                        # The complete structured response has passed validation. Each media
                        # segment can now finish independently, including a streaming prefix.
                        pieces = SentenceBuffer(limit=len(sentence.speech)).feed(sentence.speech, final=True)
                        pieces = pieces[:self.s.max_chunks]
                    if brief:
                        # Only one validated, whole sentence is ever submitted to TTS for an opening.
                        # This leaves all further provider capacity for the next human turn.
                        if metrics.get('initiative_line_selected'):
                            return
                        pieces = SentenceBuffer(limit=len(sentence.speech)).feed(sentence.speech, final=True)[:1]
                        if not pieces:
                            return
                        line = pieces[0]
                        japanese = self.s.language.startswith('ja') or bool(re.search(r'[ぁ-ヿ一-鿿]',line))
                        if (len(line)>16 if japanese else len(line.split())>6 or len(line)>48):
                            metrics['initiative_hold'] = 'INITIATIVE_TEXT_TOO_LONG'
                            return
                        metrics['initiative_line_selected'] = True
                    for piece in pieces:
                        publish('character_action',**sentence.action())
                        put(PublicOutput(piece,sentence.expression,sentence.motion))
                if 'public_line' in generation_decision:
                    if not current():
                        return
                    # Any validation failure below belongs to this local turn; Gemini stayed held.
                    key = None
                    language = self.s.language[:2]
                    speech = voices[language]
                    line = generation_decision['public_line']
                    output = PublicOutput.validate(dict(speech=line,expression=plan.expression,motion=plan.motion),
                                                   limit=self.s.max_reply_chars)
                    checked = checked_memory_output(output, language)
                    if checked is not None and current():
                        metrics['tts_route'] = dict(input_language=language,confidence=None,threshold=threshold,
                                                    fallback_language=fallback,language=language,voice=speech.voice,
                                                    decision='authored_public_reply')
                        metrics['first_sentence_ms'] = metrics['public_output_ready_ms'] = round((self.clock()-start)*1000, 2)
                        emit_speech(checked)
                    return
                for delta in self.text.stream(comment,generation_history,**speaker):
                    if not got_response:received(key,'gemini');got_response=True
                    if not current():break
                    if not isinstance(delta,str):raise ProviderError('INVALID_GENERATED_TEXT')
                    if metrics['first_text_ms'] is None:metrics['first_text_ms']=round((self.clock()-start)*1000,2)
                    if not decoder:
                        characters+=len(delta)
                        if characters>self.s.max_reply_chars:raise ProviderError('REPLY_CHARACTER_LIMIT')
                    for sentence in (decoder.feed(delta) if decoder else buffer.feed(delta)):
                        enqueue(sentence)
                if current():
                    for sentence in (decoder.feed('',final=True) if decoder else buffer.feed('',final=True)):
                        enqueue(sentence)
                self.store.call_usage(key,getattr(self.text,'usage',{}));accepted(key,'gemini')
                if pending and current():
                    action, target = pending
                    metrics['speech_language_guard'] = {'language': target, 'action': action}
                    guard = metrics['speech_language_guard']
                    if action == 'repair':
                        key = prepared('gemini','generate',{'purpose':'speech_language_repair','language':target})
                        try:
                            self.text.usage = {}
                            if not dispatch(key,'gemini',repair=True):return
                            # Dispatch refreshed the retrieval context. Keep the original comment
                            # and target voice fixed, even if the second classification differs.
                            repair = PublicDecoder(structured=True, plan=generation_decision.get('character_plan'),
                                                   limit=self.s.max_reply_chars, language_routing=bool(voices),
                                                   expand_wire=getattr(self.text,'expand_studio_output',None))
                            started = False
                            for delta in self.text.stream(comment,generation_history,**speaker,speech_repair_language=target):
                                if not started:
                                    received(key,'gemini')
                                    started = True
                                if not current():break
                                repair.feed(delta)
                            if not current():
                                self.store.call_usage(key,getattr(self.text,'usage',{}))
                                accepted(key,'gemini')
                                return
                            result, = repair.feed('',final=True)
                            result = normalize_name(result, target)
                            self.store.call_usage(key,getattr(self.text,'usage',{}))
                            if repair.unsupported_language_requested:
                                action = 'available'
                            elif not readable(result.speech,target):
                                raise ProviderError('REPAIRED_SPEECH_LANGUAGE_MISMATCH')
                            else:
                                accepted(key,'gemini')
                                guard['outcome'] = 'repaired'
                                result = checked_memory_output(result, target)
                                if result is not None:
                                    emit_speech(result)
                                return
                            accepted(key,'gemini')
                        except (ProviderError, CharacterError) as exc:
                            guard['repair_error'] = failed(key,'gemini',exc)
                            action = 'clarify'
                    if current():
                        result = select_fixed(action, target, verify_memory=True)
                        if result is not None:
                            emit_speech(result)
            except Exception as exc:
                if isinstance(exc,CharacterError):
                    self.character.format_error()
                    metrics['character_output_format_errors']=1
                    exc=ProviderError(str(exc))
                try:code=failed(key,'gemini',exc) if key else (str(exc) if isinstance(exc,ProviderError) else 'CONVERSATION_STORAGE_FAILED')
                except Exception:code='CONVERSATION_STORAGE_FAILED'
                producer_error.append(code)
            finally:put(None)
        def react():
            key=None
            try:
                key=prepared('jev','reaction',{'comment_characters':len(comment)})
                if not dispatch(key,'jev'):return
                dialogue = {'dialogue':True} if isinstance(self.reaction,JevReaction) and live_host else {}
                if wait_for_selection and use_viewer_recall and isinstance(self.reaction,JevReaction):
                    fresh = lookup(self.store, viewer, comment, self.s, turn_id)
                    offered = [item for item in candidates if item in fresh]
                    value = self.reaction.classify(comment, viewer_memories=offered, **dialogue)
                elif session_mode and eligible and isinstance(self.reaction,JevReaction):
                    # Fetch a wider independently scoped pool, ensuring both proposed reports are visible.
                    fresh = lookup(self.store,viewer,comment,self.s,turn_id) if candidates else []
                    active = [n for n in candidates if n in fresh]
                    pool = twitch_recall.candidates(self.store,viewer,comment,self.s,turn_id)
                    offered = (active+[n for n in pool if n not in active])[:self.s.semantic_candidate_limit]
                    proposed = tuple(n['memory_id'] for n in active if n in offered)
                    value=self.reaction.classify(comment,viewer_memories=offered,proposed_memory_ids=proposed,**dialogue)
                    if not offered:
                        value['semantic']={'reference':'none','memory_id':None}
                elif wait_for_selection and isinstance(self.reaction,JevReaction):
                    current_candidates=self.store.revalidate_episode_candidates(viewer,comment,self.s,candidates)
                    value=self.reaction.classify(comment,candidates=current_candidates,previous_message=previous_message,**dialogue)
                else:value=self.reaction.classify(comment,**dialogue)
                received(key,'jev')
                if not isinstance(value,dict) or value.get('reaction') not in (('joy','surprise','neutral','thinking','concerned') if dialogue else ('joy','surprise','neutral')):raise ProviderError('INVALID_REACTION')
                if current():
                    if self.character:self.character.react(character_epoch,value['reaction'])
                    semantic=value.pop('semantic',None)
                    if semantic is not None:
                        semantic_result.update(value=semantic,reaction=value.get('reaction'),ready_at=time.monotonic())
                        semantic_done.set()
                        self.store.record_semantic_retrieval(turn_id,comment,candidates,semantic,call_id=key)
                    self.store.record_reaction(turn_id,value)
                    metrics['reaction_ms']=round((self.clock()-start)*1000,2);publish('reaction',**value)
                self.store.call_usage(key,getattr(self.reaction,'usage',{}))
                accepted(key,'jev')
            except Exception as exc:
                if key:
                    try:failed(key,'jev',exc)
                    except Exception:pass
                publish('reaction_unavailable',reaction='neutral')
            finally:semantic_done.set()
        for function in ((generate,) if initiative else (generate,react)):
            thread=threading.Thread(target=function,daemon=True);thread.start();aux.append(thread)
        status='completed';code=None
        try:
            while current():
                try:sentence=sentences.get(timeout=.05)
                except queue.Empty:continue
                if sentence is None:
                    if producer_error:raise ProviderError(producer_error[0])
                    break
                if metrics['chunks']>=self.s.max_chunks:raise ProviderError('SPEECH_CHUNK_LIMIT')
                if self.character:
                    if not isinstance(sentence,PublicOutput):raise ProviderError('PUBLIC_OUTPUT_TYPE_REQUIRED')
                    sentence=sentence.speech
                index=metrics['chunks'];name=f'{turn_id}-{index}.wav';path=self.output/name
                publish('sentence',index=index,text=sentence)
                key=prepared('tts','synthesize',{'characters':len(sentence),'chunk':index});reservation=None
                try:
                    if self.s.mode=='cloud' and not getattr(speech, 'local', False):
                        reservation=self.store.reserve(len(sentence),self.s.monthly_tts_chars)
                    if not dispatch(key,'tts'):break
                    tts_start=self.clock()
                    options = {}
                    sink = self.program_pcm
                    if not brief and self.character and sink and getattr(speech, 'streams_program_pcm', False) and index == 0:
                        def on_pcm(packet):
                            if not current():
                                raise ProviderError('TURN_CANCELLED')
                            if metrics.get('first_pcm_ms') is None:
                                metrics['first_pcm_ms'] = round((self.clock()-start)*1000,2)
                            sink(turn_id, sentence, packet)
                        options['on_pcm'] = on_pcm
                    try:
                        timing = speech.create(sentence, path, **options)
                    except ProviderError as exc:
                        backup = getattr(speech, 'fallback_voice', None)
                        if (str(exc) != 'LOCAL_TTS_HTTP_503' or backup is None or index != 0
                                or metrics.get('first_pcm_ms') is not None or not current()
                                or self.reply_playable is None or not self.reply_playable()):
                            raise
                        failed(key, 'tts', exc)
                        key = prepared('tts', 'synthesize', {'characters':len(sentence), 'chunk':index,
                                                            'purpose':'local_cpu_fallback'})
                        metrics['tts_failover'] = {'reason':str(exc), 'outcome':'attempted'}
                        if not dispatch(key, 'tts'):
                            break
                        # No PCM has been released. Use the same voice in a separate local worker.
                        try:
                            timing = backup.create(sentence, path, **options)
                        except Exception:
                            metrics['tts_failover']['outcome'] = 'failed'
                            raise
                        metrics['tts_failover']['outcome'] = 'synthesized'
                    received(key,'tts')
                    duration=timing.get('duration',0)
                    if not isinstance(duration,(int,float)) or not 0<duration<=60 or not path.is_file() or path.stat().st_size>12*1024*1024:raise ProviderError('INVALID_TTS_RESULT')
                    if reservation:self.store.usage_complete(reservation)
                    metrics['chunks']+=1;metrics['characters']+=len(sentence);reply.append(sentence)
                    accepted(key,'tts')
                except Exception as exc:raise ProviderError(failed(key,'tts',exc)) from None
                if not current():break
                if brief:
                    # Validate the exact WAV before publishing any prefix, subtitle or audio.
                    with wave.open(str(path),'rb') as wav:
                        seconds = wav.getnframes()/wav.getframerate()
                    if seconds > self.initiative_audio_limit:
                        metrics['initiative_hold'] = 'INITIATIVE_AUDIO_TOO_LONG'
                        break
                    duration = seconds
                self.store.record_utterance(turn_id,index,sentence,path,duration)
                if metrics['first_audio_ready_ms'] is None:metrics['first_audio_ready_ms']=round((self.clock()-start)*1000,2)
                publish('audio',index=index,text=sentence,word_starts=utterance_word_starts(timing),audio='/conversation/audio/'+name,duration=duration,visemes=timing.get('visemes',[]),mouth_plan=timing.get('mouth_plan',[]),lip_sync=timing.get('lip_sync',timing['source']),tts_ms=round((self.clock()-tts_start)*1000,2))
            if cancel.is_set():status='cancelled'
            elif self.clock()>=deadline:raise ProviderError('TURN_DEADLINE')
            elif brief and metrics.get('initiative_hold') and (not reply or metrics['initiative_hold']=='INITIATIVE_AUDIO_TOO_LONG'):
                status,code='held',metrics['initiative_hold']
            elif not reply:raise ProviderError('EMPTY_GENERATED_REPLY')
        except Exception as exc:status='failed';code=str(exc) if isinstance(exc,ProviderError) else 'SPEECH_PIPELINE_FAILED'
        finally:
            # Uncertain calls and partial speech are retained, never retried automatically.
            # Stop producers immediately on failure. On success let already-running
            # reaction bookkeeping finish before invalidating its current() fence.
            if status!='completed':
                worker_stop.set()
                if self.character:self.character.cancel(event=turn_id)
            for thread in aux:thread.join(timeout=self.s.timeout+.2)
            worker_stop.set()
            metrics['total_ms']=round((self.clock()-start)*1000,2)
            if self.character:
                metrics['character']=self.character.metrics()
                with self.memory_worker.lock:metrics['character_memory']=dict(self.memory_worker.character_counts)
            self.store.finish(turn_id,' '.join(reply),status,metrics)
        if not cancel.is_set():emit({'kind':'complete' if status=='completed' else 'held' if status=='held' else 'error','status':status,'code':code,'metrics':metrics})
        return metrics


class ConversationBridge:
    """One turn worker. Old events and duplicate chunk claims cannot reach playout."""
    def __init__(self,settings,store,output,fence,lock,*,providers=None,memory_extractor=None):
        self.controller=SpeechController(settings,store,output,providers=providers,memory_extractor=memory_extractor)
        self._init_playout(fence,lock)

    def _init_playout(self,fence,lock):
        self.fence=fence;self.lock=lock
        self.playout_state = self.playout_hooks = None
        self.viewer=None;self.events=[];self.cancel_event=threading.Event();self.worker=None;self.token=None;self.claims=set();self.finished=False

    def next_studio_turn(self, *, cancelled_initiative=False):
        """Fork playout state after generation, or after fencing an unheard scheduled opening."""
        with self.lock:
            retiring = (cancelled_initiative and self.cancel_event.is_set()
                        and self.viewer and self.viewer.startswith('ray-initiative:'))
            if not self.finished or (self.worker.is_alive() and not retiring):
                raise ValueError('TURN_BUSY')
            character = self.controller.character
            if character and self.playout_state is None:
                turn = self.token['turn_id']
                self.playout_state = character.playout_state(turn)
                self.playout_hooks = character.hooks_for(turn)
            branch = copy(self)
            branch.controller = copy(self.controller)
            branch._init_playout(type(self.fence)(), threading.RLock())
            return branch

    def begin(self,comment,viewer='preview',*,on_begin=None,on_terminal=None,followup=None):
        if not isinstance(comment,str) or not 0<len(comment.strip())<=1000 or not isinstance(viewer,str) or not 0<len(viewer)<=100:raise ValueError('INVALID_COMMENT')
        with self.lock:
            if self.worker and self.worker.is_alive():raise ValueError('TURN_BUSY')
            self.viewer=viewer
            self.playout_state = self.playout_hooks = None
            self.cancel_event=threading.Event();self.events=[];self.claims=set();self.finished=False
            self.token=self.fence.begin(uuid.uuid4().hex);token=dict(self.token)
            if on_begin:on_begin(token)
            def emit(value):
                accepted=False
                with self.lock:
                    if self.fence.current(token) and not self.cancel_event.is_set() and len(self.events)<128:
                        self.events.append({'seq':len(self.events),'token':token,**value});accepted=True
                        if value['kind'] in ('complete','error','held'):self.finished=True
                if accepted and value['kind'] in ('complete','error','held') and on_terminal:on_terminal(value)
            def run():
                try:self.controller.run(comment,viewer,token['turn_id'],emit,self.cancel_event,**({'followup':followup} if followup else {}))
                except Exception:emit({'kind':'error','status':'failed','code':'CONVERSATION_STORAGE_FAILED','metrics':{'provider_requests':0}})
            self.worker=threading.Thread(target=run,daemon=True)
            self.worker.start()
            return token

    def poll(self,after):
        with self.lock:return {'events':self.events[after:],'finished':self.finished,'token':self.token}

    def claim(self,token,index):
        with self.lock:
            key=(token.get('turn_id'),index)
            ready=any(e['kind']=='audio' and e['index']==index for e in self.events)
            if not self.fence.current(token) or key in self.claims or not ready:return False
            self.controller.store.chunk_claimed(token['turn_id'],index)
            self.claims.add(key);return True

    def cancel(self):
        with self.lock:
            if self.token and not self.cancel_event.is_set():
                self.events.append({'seq':len(self.events),'token':dict(self.token),'kind':'cancelled','code':'TURN_CANCELLED','metrics':{'provider_requests':0}})
                self.controller.store.cancel_turn(self.token['turn_id'])
            self.cancel_event.set();self.fence.cancel();self.finished=True
            character = getattr(self.controller,'character',None)
            if character:
                character.cancel(event=self.token['turn_id'] if self.token else None)

    def mark_played(self,token,*,program_latency=None):
        with self.lock:
            audio_count=sum(e['kind']=='audio' for e in self.events)
            if not self.fence.current(token) or not self.finished or len(self.claims)!=audio_count or not any(e['kind']=='complete' for e in self.events):return False
            s=self.controller.s
            character = self.controller.character
            hooks = self.playout_hooks if self.playout_state else (character.hooks_for(token['turn_id']) if character else None)
            accepted=self.controller.store.played(token['turn_id'],scope=s.mode,memory_enabled=s.memory_enabled,
                memory_ttl_days=s.memory_ttl_days,memory_capacity=s.memory_queue_size,
                memory_policy=self.controller.memory_worker.policy,memory_model=self.controller.memory_worker.model,
                memory_min_confidence=s.memory_min_confidence,relationship_enabled=s.memory_enabled and s.relationship_enabled,developer_id=s.developer_id,
                program_latency=program_latency,
                unspoken_hooks=hooks)
            audience = self.viewer.startswith('audience:')
            if accepted and not audience and s.memory_enabled and self.controller.store.memory_pending(self.viewer,s.mode):
                self.controller.memory_worker.wake()
            if accepted and self.controller.character:
                initiative = self.viewer.startswith('ray-initiative:')
                turn = token['turn_id']
                exchange = self.controller.store.followup_exchange(turn,self.viewer,s.mode) if s.character_initiative_enabled and not initiative and not audience else None
                self.controller.character.played(turn,turn,self.viewer,exchange,initiative=initiative,playout_state=self.playout_state)
                if not initiative and not audience:self.controller.memory_worker.offer_character(turn)
            return accepted
