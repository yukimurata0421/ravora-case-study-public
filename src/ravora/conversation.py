"""Sentence-at-a-time speech controller, bounded queues, and local SQLite memory."""
from copy import copy
import datetime as dt
import json
import logging
from pathlib import Path
import queue
import re
import threading
import time
import uuid
import sqlite3

from ravora.speech import utterance_word_starts
from ravora.providers import ProviderError, GeminiText, JevReaction, adapters
from ravora.database import Connection as closing_connection
from ravora.developer_identity import DEVELOPER_PLATFORMS, viewer_key
from ravora.memory import MemoryRepository
from ravora.memory_worker import MemoryWorker
from ravora.conversation_context import ConversationContext, sensitive
from ravora.memory_extraction import acknowledgment, banter_preference
from ravora.persona import JAPANESE_QUESTION_WINDOW, asked_listener_in_japanese, japanese_question_direction
from ravora.relationships import Relationships,relationship_text
from ravora.semantic_memory import SemanticMemory, speech_background
from ravora.ray_character import CharacterOpening,CharacterRuntime,CharacterError,PublicDecoder,PublicOutput
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
    def start(self,turn_id,viewer,comment,*,developer_id=None):
        """Start a turn and identify the configured developer from its bound input."""
        now=time.time()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            source=db.execute('''SELECT s.id,s.is_deleted,s.raw_purged_at,i.session
                FROM source_events s JOIN comment_inbox i ON i.receipt=s.id WHERE i.turn_id=?''',(turn_id,)).fetchone()
            status='source_removed' if source and source[1] else 'source_expired' if source and source[2] is not None else 'running'
            db.execute('''INSERT INTO conversation_turns(id,turn_id,source_event_id,viewer,comment,status,started,state,created_at,usage_session)
                VALUES(?,?,?,?,?,?,?,?,?,?)''',(turn_id,turn_id,source[0] if source else None,viewer,
                comment if status=='running' else '',status,dt.datetime.now(dt.timezone.utc).isoformat(),
                'SELECTING' if status=='running' else 'CANCELLED',now,source[3] if source else None))
            scope,_,owner=viewer.partition(':')
            platform=owner.partition(':')[0]
            developer_speaker=False
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
                AND prior.is_deleted=0 AND prior.raw_purged_at IS NULL AND prior.expires_at>?)'''
            args.extend((turn_id,time.time()))
        with self.connect() as db:
            rows=db.execute('SELECT t.comment,t.reply FROM conversation_turns t WHERE '+where+
                            ' ORDER BY t.rowid DESC LIMIT ?',(*args,count*4 if turn_id else count)).fetchall()
        if turn_id is not None:
            rows=[(q, '' if sensitive(a) else a) for q,a in rows if not sensitive(q)]
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

    def played(self,turn_id,*,scope='offline',memory_enabled=True,memory_ttl_days=30,memory_capacity=16,
               memory_policy=None,memory_model=None,memory_min_confidence=.7,relationship_enabled=True,developer_id=None,program_latency=None,
               unspoken_hooks=None):
        now=time.time()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute("UPDATE conversation_turns SET played=1,state='COMPLETED',closed_at=COALESCE(closed_at,?) WHERE id=? AND status='completed' AND played=0 RETURNING source_event_id,viewer,comment,reply",(now,turn_id)).fetchone()
            if row:
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
                source=db.execute('SELECT platform FROM source_events WHERE id=?',(row[0],)).fetchone()
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
            db.execute("UPDATE provider_calls SET state='dispatched',attempted=1,updated=? WHERE id=?",(now,key))
            db.execute("UPDATE api_usage SET status='dispatched',attempted=1 WHERE id=?",(key,))
        return True

    def memory_call_state(self,job,key,state,*,code=None):
        self.call_state(key,state,code=code)
        with self.connect() as db:
            db.execute('UPDATE memory_jobs SET request_state=? WHERE turn_id=? AND api_call_id=?',(state,job['turn_id'],key))


class SpeechController:
    def __init__(self,settings,store,output,*,providers=None,clock=time.monotonic,memory_extractor=None):
        self.s=settings;self.store=store;self.output=Path(output);self.clock=clock
        self.text,self.reaction,self.speech=providers or adapters(settings)
        self.program_pcm = None
        self.memory_worker=MemoryWorker(store,settings,extractor=memory_extractor)
        self.character=CharacterRuntime(idle_seconds=settings.character_idle_seconds,max_initiatives=settings.character_max_initiatives) if settings.character_enabled else None

    def run(self,comment,viewer,turn_id,emit,cancel,*,followup=None):
        start=self.clock();deadline=start+self.s.turn_timeout
        initiative=viewer.startswith('ray-initiative:')
        character_epoch=self.character.begin(turn_id,comment,initiative=initiative,viewer=viewer,opening=followup) if self.character else None
        metrics={'mode':self.s.mode,'first_text_ms':None,'first_sentence_ms':None,'first_audio_ready_ms':None,'reaction_ms':None,'chunks':0,'characters':0,'provider_requests':0}
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
        developer_speaker=self.store.start(turn_id,memory_viewer,comment,developer_id=self.s.developer_id)
        if self.character:self.memory_worker.wake()
        history=self.store.history(memory_viewer,self.s.memory_turns,turn_id=turn_id)
        recent_for_route = history if self.s.memory_turns >= 4 or not self.character else self.store.history(memory_viewer,4,turn_id=turn_id)
        recent_for_route = recent_for_route + self.store.opening_history(self.character,character_epoch,self.s.mode)
        route = self.character.route(character_epoch, comment, recent_for_route,
                                     initiative=initiative) if self.character else DialogueRoute()
        rag = route.allow_retrieval(metrics, comment)
        memory_wait=self.clock()
        semantic_enabled=rag and self.s.memory_enabled and self.s.semantic_memory_mode!='off'
        if semantic_enabled:self.memory_worker.wake()
        # A live reply uses committed memories while the remote extraction runs in the background.
        if rag and not (semantic_enabled or self.memory_worker.live_extraction) and not self.memory_worker.wait_for(viewer,cancel=cancel):
            metrics['total_ms']=round((self.clock()-start)*1000,2)
            self.store.finish(turn_id,'','failed',metrics)
            if not cancel.is_set():emit({'kind':'error','status':'failed','code':'MEMORY_WORKER_NOT_READY','metrics':metrics})
            return metrics
        metrics['memory_wait_ms']=round((self.clock()-memory_wait)*1000,2)
        memories,strategy=self.store.conversation_memories(viewer,comment,self.s) if rag else ([], 'dialogue_gate')
        candidates=self.store.episode_candidates(viewer,comment,scope=self.s.mode,
            limit=self.s.semantic_candidate_limit,max_chars=self.s.semantic_max_chars) if semantic_enabled else []
        semantic_done=threading.Event();semantic_result={};generation_decision={}
        previous_message=self.store.semantic_previous_message(viewer,scope=self.s.mode,turn_id=turn_id) if candidates else ''
        generation_history=history
        def prepare_context(key):
            nonlocal generation_history
            # Refresh before dispatch: a removed source cannot survive in the
            # adapter's context from an earlier turn.
            notes,method=self.store.conversation_memories(viewer,comment,self.s,judgment=generation_decision.get('value')) if rag else ([], 'dialogue_gate')
            if rag and metrics['knowledge_rag'] != 'explicit_recall' and developer_speaker and isinstance(self.text,GeminiText):
                notes+=self.store.developer_banter_memory(viewer,self.s)
            if isinstance(self.text,GeminiText):
                self.text.set_retrieval_strategy(method,reaction=generation_decision.get('reaction'))
            elif hasattr(self.text,'set_retrieval_strategy'):self.text.set_retrieval_strategy(method)
            supported=hasattr(self.text,'set_memories')
            if supported:self.text.set_memories(notes)
            self.store.record_retrievals(turn_id,notes,context_prepared=supported,strategy=method,call_id=key)
            if semantic_enabled:
                self.store.record_semantic_retrieval(turn_id,comment,candidates,generation_decision.get('value'),used=generation_decision.get('value') is not None)
            context=self.store.recall_context(viewer,comment,scope=self.s.mode,developer_id=self.s.developer_id,
                limit=self.s.context_max_items,max_chars=self.s.context_max_chars,turn_id=turn_id) if rag and self.s.memory_enabled and self.s.context_enabled else []
            pace_questions=self.s.language=='ja-JP' and not initiative
            recent=self.store.history(memory_viewer,self.s.memory_turns,turn_id=turn_id)
            if pace_questions:
                cadence_history=recent if self.s.memory_turns>=JAPANESE_QUESTION_WINDOW else self.store.history(memory_viewer,JAPANESE_QUESTION_WINDOW,turn_id=turn_id)
                questions=[asked_listener_in_japanese(line) for role,line in cadence_history if role=='model']
                generation_decision['japanese_turn']=japanese_question_direction(questions[-JAPANESE_QUESTION_WINDOW:], comment)
            generation_history,context=speech_background(self.s,comment,bool(candidates),generation_decision.get('value'),
                recent,context,selected=notes)
            if followup is not None:
                generation_history = self.store.followup_history(followup,self.s.mode)
                context = []
            else:
                generation_history = generation_history + self.store.opening_history(self.character,character_epoch,self.s.mode)
            context_supported=hasattr(self.text,'set_context')
            if context_supported:self.text.set_context(context)
            self.store.record_context_retrievals(turn_id,context,call_id=key,prepared=context_supported)
            begin=time.monotonic()
            background=self.store.relationship_context(viewer,scope=self.s.mode) if self.s.memory_enabled and self.s.relationship_enabled else None
            relationship_supported=hasattr(self.text,'set_relationship')
            if relationship_supported:self.text.set_relationship(background)
            metrics['relationship_lookup_ms']=round((time.monotonic()-begin)*1000,3)
            metrics['relationship_context_chars']=len(relationship_text(background)) if background else 0
            metrics['relationship_context_prepared']=bool(background and relationship_supported)
            metrics['context_tiers']=[e['tier'] for e in context];metrics['context_items']=len(context)
            metrics['memory_ids']=[m['memory_id'] for m in notes];metrics['memory_context_prepared']=supported
            metrics['memory_strategy']=method
            character_begin=time.monotonic()
            memories=self.memory_worker.character_memories(viewer,route,comment) if self.character else ()
            if not current():raise ProviderError('TURN_CANCELLED')
            hook=claim_hook(self.store,self.s.mode,viewer,turn_id,comment,route.unspoken_hooks) if route.dialogue_policy=='SHIFT_TO_HOOK' else ''
            plan=self.character.plan(comment,memories,background,initiative=initiative,epoch=character_epoch,
                                     host_speaking=live_host or developer_speaker,hook=hook) if self.character else None
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
            key=self.store.prepare_call(turn_id,provider,operation,summary,model=models[provider],mode=self.s.mode)
            publish('call_prepared',call_id=key,provider=provider,operation=operation)
            return key
        def dispatch(key,provider):
            if not current():self.store.call_state(key,'cancelled_before_send');return False
            if provider=='gemini':prepare_context(key)
            remote=self.s.mode=='cloud'
            if provider=='gemini' and remote and self.s.monthly_reply_llm_usd:
                return True  # GeminiText supplies the assembled payload size immediately before send.
            self.store.call_state(key,'dispatched' if remote else 'simulated',attempted=remote)
            if remote:
                with metrics_lock:metrics['provider_requests']+=1
            publish('call_dispatched' if remote else 'call_simulated',call_id=key,provider=provider)
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
            adapter={'gemini':self.text,'jev':self.reaction,'tts':self.speech}[provider]
            self.store.call_usage(key,getattr(adapter,'usage',{}))
            self.store.call_state(key,state,code=code);publish('call_failed',call_id=key,provider=provider,code=code,state=state)
            return code

        def generate():
            buffer=SentenceBuffer(self.s.sentence_chars);characters=0;key=None;got_response=False
            try:
                if candidates and self.s.semantic_memory_mode=='jev':
                    wait_started=time.monotonic();deadline_at=wait_started+self.s.semantic_wait_ms/1000
                    semantic_done.wait(self.s.semantic_wait_ms/1000)
                    if semantic_result.get('ready_at',float('inf'))<=deadline_at:
                        generation_decision.update(value=semantic_result['value'],reaction=semantic_result['reaction'])
                    metrics['semantic_wait_ms']=round((time.monotonic()-wait_started)*1000,3)
                key=prepared('gemini','generate',{'comment_characters':len(comment),'history_turns':len(history)//2})
                if not dispatch(key,'gemini'):return
                speaker={'developer_speaker':developer_speaker} if isinstance(self.text,GeminiText) else {}
                if isinstance(self.text,GeminiText) and 'japanese_turn' in generation_decision:
                    speaker['japanese_turn']=generation_decision['japanese_turn']
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
                plan=generation_decision.get('character_plan')
                decoder=PublicDecoder(structured=bool(getattr(self.text.stream,'ray_public_output',False)),buffer=buffer,plan=plan,limit=self.s.max_reply_chars) if self.character else None
                for delta in self.text.stream(comment,generation_history,**speaker):
                    if not got_response:received(key,'gemini');got_response=True
                    if not current():break
                    if not isinstance(delta,str):raise ProviderError('INVALID_GENERATED_TEXT')
                    if metrics['first_text_ms'] is None:metrics['first_text_ms']=round((self.clock()-start)*1000,2)
                    if not decoder:
                        characters+=len(delta)
                        if characters>self.s.max_reply_chars:raise ProviderError('REPLY_CHARACTER_LIMIT')
                    for sentence in (decoder.feed(delta) if decoder else buffer.feed(delta)):
                        if metrics['first_sentence_ms'] is None:metrics['first_sentence_ms']=round((self.clock()-start)*1000,2)
                        if isinstance(sentence,PublicOutput):
                            characters+=len(sentence.speech)
                            if characters>self.s.max_reply_chars:raise ProviderError('REPLY_CHARACTER_LIMIT')
                            metrics.setdefault('public_output_ready_ms',round((self.clock()-start)*1000,2))
                            publish('character_action',**sentence.action())
                        put(sentence)
                if current():
                    for sentence in (decoder.feed('',final=True) if decoder else buffer.feed('',final=True)):
                        if metrics['first_sentence_ms'] is None:metrics['first_sentence_ms']=round((self.clock()-start)*1000,2)
                        if isinstance(sentence,PublicOutput):
                            characters+=len(sentence.speech)
                            if characters>self.s.max_reply_chars:raise ProviderError('REPLY_CHARACTER_LIMIT')
                            metrics.setdefault('public_output_ready_ms',round((self.clock()-start)*1000,2))
                            publish('character_action',**sentence.action())
                        put(sentence)
                self.store.call_usage(key,getattr(self.text,'usage',{}));accepted(key,'gemini')
            except Exception as exc:
                if isinstance(exc,CharacterError):
                    self.character.format_error()
                    metrics['character_output_format_errors']=1
                    exc=ProviderError(str(exc))
                try:code=failed(key,'gemini',exc) if key else 'CONVERSATION_STORAGE_FAILED'
                except Exception:code='CONVERSATION_STORAGE_FAILED'
                producer_error.append(code)
            finally:put(None)
        def react():
            key=None
            try:
                key=prepared('jev','reaction',{'comment_characters':len(comment)})
                if not dispatch(key,'jev'):return
                dialogue = {'dialogue':True} if isinstance(self.reaction,JevReaction) and live_host else {}
                if candidates and self.s.semantic_memory_mode=='jev' and isinstance(self.reaction,JevReaction):
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
                    if self.s.mode=='cloud':reservation=self.store.reserve(len(sentence),self.s.monthly_tts_chars)
                    if not dispatch(key,'tts'):break
                    tts_start=self.clock()
                    options = {}
                    sink = self.program_pcm
                    if self.character and sink and getattr(self.speech, 'streams_program_pcm', False) and index == 0:
                        def on_pcm(packet):
                            if not current():
                                raise ProviderError('TURN_CANCELLED')
                            if metrics.get('first_pcm_ms') is None:
                                metrics['first_pcm_ms'] = round((self.clock()-start)*1000,2)
                            sink(turn_id, sentence, packet)
                        options['on_pcm'] = on_pcm
                    timing=self.speech.create(sentence,path,**options);received(key,'tts')
                    duration=timing.get('duration',0)
                    if not isinstance(duration,(int,float)) or not 0<duration<=60 or not path.is_file() or path.stat().st_size>12*1024*1024:raise ProviderError('INVALID_TTS_RESULT')
                    if reservation:self.store.usage_complete(reservation)
                    metrics['chunks']+=1;metrics['characters']+=len(sentence);reply.append(sentence)
                    accepted(key,'tts')
                except Exception as exc:raise ProviderError(failed(key,'tts',exc)) from None
                if not current():break
                self.store.record_utterance(turn_id,index,sentence,path,duration)
                if metrics['first_audio_ready_ms'] is None:metrics['first_audio_ready_ms']=round((self.clock()-start)*1000,2)
                publish('audio',index=index,text=sentence,word_starts=utterance_word_starts(timing),audio='/conversation/audio/'+name,duration=duration,visemes=timing.get('visemes',[]),mouth_plan=timing.get('mouth_plan',[]),lip_sync=timing.get('lip_sync',timing['source']),tts_ms=round((self.clock()-tts_start)*1000,2))
            if cancel.is_set():status='cancelled'
            elif self.clock()>=deadline:raise ProviderError('TURN_DEADLINE')
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
        if not cancel.is_set():emit({'kind':'complete' if status=='completed' else 'error','status':status,'code':code,'metrics':metrics})
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

    def next_studio_turn(self):
        """Generation is serial. Keep the playing turn's claims, callbacks and ACK independently fenced."""
        with self.lock:
            if not self.finished or self.worker.is_alive():
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
            if accepted and s.memory_enabled and self.controller.store.memory_pending(self.viewer,s.mode):
                self.controller.memory_worker.wake()
            if accepted and self.controller.character:
                initiative = self.viewer.startswith('ray-initiative:')
                turn = token['turn_id']
                exchange = self.controller.store.followup_exchange(turn,self.viewer,s.mode) if s.character_initiative_enabled and not initiative else None
                self.controller.character.played(turn,turn,self.viewer,exchange,initiative=initiative,playout_state=self.playout_state)
                if not initiative:self.controller.memory_worker.offer_character(turn)
            return accepted
