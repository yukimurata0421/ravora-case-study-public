"""Sentence-at-a-time speech controller, bounded queues, and local SQLite memory."""
import datetime as dt
import json
from pathlib import Path
import queue
import re
import threading
import time
import uuid

from ravora.providers import ProviderError, adapters
from ravora.database import Connection as closing_connection
from ravora.memory import MemoryRepository
from ravora.memory_worker import MemoryWorker
from ravora.conversation_context import ConversationContext
from ravora.relationships import Relationships,relationship_text


class SentenceBuffer:
    def __init__(self,limit=240):self.text='';self.limit=limit

    def feed(self,delta,final=False):
        self.text+=delta;out=[]
        while self.text:
            cut=None
            for match in re.finditer(r'[.!?]+["\u201d\u2019\']*(?=\s)',self.text):
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


class MemoryStore(Relationships,ConversationContext,MemoryRepository):
    def start(self,turn_id,viewer,comment):
        now=time.time()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            source=db.execute('SELECT s.id,s.is_deleted,s.raw_purged_at FROM source_events s JOIN comment_inbox i ON i.receipt=s.id WHERE i.turn_id=?',(turn_id,)).fetchone()
            status='source_removed' if source and source[1] else 'source_expired' if source and source[2] is not None else 'running'
            db.execute('''INSERT INTO conversation_turns(id,turn_id,source_event_id,viewer,comment,status,started,state,created_at)
                VALUES(?,?,?,?,?,?,?,?,?)''',(turn_id,turn_id,source[0] if source else None,viewer,
                comment if status=='running' else '',status,dt.datetime.now(dt.timezone.utc).isoformat(),
                'SELECTING' if status=='running' else 'CANCELLED',now))
        if status!='running':raise ProviderError('COMMENT_NO_LONGER_AVAILABLE')

    def history(self,viewer,count=4):
        if not count:return []
        with self.connect() as db:
            rows=db.execute("SELECT comment,reply FROM conversation_turns WHERE viewer=? AND played=1 AND status='completed' ORDER BY rowid DESC LIMIT ?",(viewer,count)).fetchall()
        return [(role,text) for comment,reply in reversed(rows) for role,text in [('user',comment),('model',reply)]]

    def finish(self,turn_id,reply,status,metrics):
        with self.connect() as db:db.execute("""UPDATE conversation_turns SET
            reply=CASE WHEN status IN ('source_removed','source_expired','cancelled') THEN '' ELSE ? END,
            state=CASE WHEN status IN ('source_removed','source_expired','cancelled') THEN 'CANCELLED' WHEN ?='completed' THEN 'GENERATED' ELSE upper(?) END,
            closed_at=CASE WHEN ?='completed' THEN closed_at ELSE COALESCE(closed_at,?) END,
            status=CASE WHEN status IN ('source_removed','source_expired','cancelled') THEN status ELSE ? END,
            metrics=? WHERE id=?""",(reply,status,status,status,time.time(),status,json.dumps(metrics),turn_id))

    def played(self,turn_id,*,scope='offline',memory_enabled=True,memory_ttl_days=30,memory_capacity=16,
               memory_policy=None,memory_model=None,memory_min_confidence=.7,relationship_enabled=True):
        now=time.time()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute("UPDATE conversation_turns SET played=1,state='COMPLETED',closed_at=COALESCE(closed_at,?) WHERE id=? AND status='completed' AND played=0",(now,turn_id))
            if row.rowcount:
                if relationship_enabled:self.record_relationship_played(db,turn_id,scope,now)
                db.execute("UPDATE utterances SET status='playback_acknowledged',playback_ack_at=COALESCE(playback_ack_at,?) WHERE turn_id=? AND status='claimed'",(now,turn_id))
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
        now=time.time()
        with self.connect() as db:
            db.execute('UPDATE provider_calls SET state=?,attempted=MAX(attempted,?),updated=?,code=? WHERE id=?',(state,int(attempted),now,code,key))
            # This measures controller stage latency, not provider billing or wire time.
            db.execute('''UPDATE api_usage SET status=?,attempted=MAX(attempted,?),
                latency_ms=CASE WHEN ? IN ('accepted','failed','outcome_unknown','discarded_after_cancel')
                    THEN MAX(0,(?-created_at)*1000) ELSE latency_ms END WHERE id=?''',(state,int(attempted),state,now,key))

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
        self.memory_worker=MemoryWorker(store,settings,extractor=memory_extractor)

    def run(self,comment,viewer,turn_id,emit,cancel):
        start=self.clock();deadline=start+self.s.turn_timeout
        metrics={'mode':self.s.mode,'first_text_ms':None,'first_sentence_ms':None,'first_audio_ready_ms':None,'reaction_ms':None,'chunks':0,'characters':0,'provider_requests':0}
        reply=[];sentences=queue.Queue(maxsize=2);producer_error=[];worker_stop=threading.Event();aux=[];metrics_lock=threading.Lock()
        def current():return not cancel.is_set() and not worker_stop.is_set() and self.clock()<deadline
        def publish(kind,**data):
            if current():emit({'kind':kind,'elapsed_ms':round((self.clock()-start)*1000,2),**data})
        def put(value):
            while current():
                try:sentences.put(value,timeout=.05);return
                except queue.Full:pass
        memory_viewer=self.s.mode+':'+viewer
        self.store.start(turn_id,memory_viewer,comment)
        memory_wait=self.clock()
        if not self.memory_worker.wait_for(viewer,cancel=cancel):
            metrics['total_ms']=round((self.clock()-start)*1000,2)
            self.store.finish(turn_id,'','failed',metrics)
            if not cancel.is_set():emit({'kind':'error','status':'failed','code':'MEMORY_WORKER_NOT_READY','metrics':metrics})
            return metrics
        metrics['memory_wait_ms']=round((self.clock()-memory_wait)*1000,2)
        history=self.store.history(memory_viewer,self.s.memory_turns)
        memories,strategy=self.store.recall_memory(viewer,comment,scope=self.s.mode) if self.s.memory_enabled else ([], 'disabled')
        def prepare_context(key):
            # Refresh before dispatch: a removed source cannot survive in the
            # adapter's context from an earlier turn.
            notes,method=self.store.recall_memory(viewer,comment,scope=self.s.mode) if self.s.memory_enabled else ([], 'disabled')
            supported=hasattr(self.text,'set_memories')
            if supported:self.text.set_memories(notes)
            self.store.record_retrievals(turn_id,notes,context_prepared=supported,strategy=method,call_id=key)
            context=self.store.recall_context(viewer,comment,scope=self.s.mode,developer_id=self.s.developer_id,
                limit=self.s.context_max_items,max_chars=self.s.context_max_chars) if self.s.memory_enabled and self.s.context_enabled else []
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
        models={'gemini':self.s.gemini_model,'jev':self.s.jev_model,'tts':self.s.voice} if self.s.mode=='cloud' else {'gemini':'english-fixture','jev':'fixture-reaction','tts':'flite-slt'}
        publish('request_prepared',comment_characters=len(comment),history_turns=len(history)//2,memory_items=len(memories),mode=self.s.mode)
        if self.s.mode=='cloud' and not self.s.api_enabled:
            for provider,operation in [('gemini','generate'),('jev','reaction')]:
                key=self.store.prepare_call(turn_id,provider,operation,{'comment_characters':len(comment),'history_turns':len(history)//2},model=models[provider],mode=self.s.mode)
                if provider=='gemini':prepare_context(key)
                self.store.call_state(key,'held',code='API_DISABLED')
            metrics['total_ms']=round((self.clock()-start)*1000,2)
            self.store.finish(turn_id,'','held',metrics)
            if not cancel.is_set():emit({'kind':'held','status':'held','code':'API_DISABLED','metrics':metrics})
            return metrics

        def prepared(provider,operation,summary):
            key=self.store.prepare_call(turn_id,provider,operation,summary,model=models[provider],mode=self.s.mode)
            if provider=='gemini':prepare_context(key)
            publish('call_prepared',call_id=key,provider=provider,operation=operation)
            return key
        def dispatch(key,provider):
            if not current():self.store.call_state(key,'cancelled_before_send');return False
            if provider=='gemini':prepare_context(key)
            remote=self.s.mode=='cloud'
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
                key=prepared('gemini','generate',{'comment_characters':len(comment),'history_turns':len(history)//2})
                if not dispatch(key,'gemini'):return
                for delta in self.text.stream(comment,history):
                    if not got_response:received(key,'gemini');got_response=True
                    if not current():break
                    if not isinstance(delta,str):raise ProviderError('INVALID_GENERATED_TEXT')
                    if metrics['first_text_ms'] is None:metrics['first_text_ms']=round((self.clock()-start)*1000,2)
                    characters+=len(delta)
                    if characters>self.s.max_reply_chars:raise ProviderError('REPLY_CHARACTER_LIMIT')
                    for sentence in buffer.feed(delta):
                        if metrics['first_sentence_ms'] is None:metrics['first_sentence_ms']=round((self.clock()-start)*1000,2)
                        put(sentence)
                if current():
                    for sentence in buffer.feed('',final=True):
                        if metrics['first_sentence_ms'] is None:metrics['first_sentence_ms']=round((self.clock()-start)*1000,2)
                        put(sentence)
                self.store.call_usage(key,getattr(self.text,'usage',{}));accepted(key,'gemini')
            except Exception as exc:
                try:code=failed(key,'gemini',exc) if key else 'CONVERSATION_STORAGE_FAILED'
                except Exception:code='CONVERSATION_STORAGE_FAILED'
                producer_error.append(code)
            finally:put(None)
        def react():
            key=None
            try:
                key=prepared('jev','reaction',{'comment_characters':len(comment)})
                if not dispatch(key,'jev'):return
                value=self.reaction.classify(comment);received(key,'jev')
                if not isinstance(value,dict) or value.get('reaction') not in ('joy','surprise','neutral'):raise ProviderError('INVALID_REACTION')
                if current():
                    self.store.record_reaction(turn_id,value)
                    metrics['reaction_ms']=round((self.clock()-start)*1000,2);publish('reaction',**value)
                self.store.call_usage(key,getattr(self.reaction,'usage',{}))
                accepted(key,'jev')
            except Exception as exc:
                if key:
                    try:failed(key,'jev',exc)
                    except Exception:pass
                publish('reaction_unavailable',reaction='neutral')
        for function in (generate,react):
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
                index=metrics['chunks'];name=f'{turn_id}-{index}.wav';path=self.output/name
                publish('sentence',index=index,text=sentence)
                key=prepared('tts','synthesize',{'characters':len(sentence),'chunk':index});reservation=None
                try:
                    if self.s.mode=='cloud':reservation=self.store.reserve(len(sentence),self.s.monthly_tts_chars)
                    if not dispatch(key,'tts'):break
                    tts_start=self.clock();timing=self.speech.create(sentence,path);received(key,'tts')
                    duration=timing.get('duration',0)
                    if not isinstance(duration,(int,float)) or not 0<duration<=60 or not path.is_file() or path.stat().st_size>12*1024*1024:raise ProviderError('INVALID_TTS_RESULT')
                    if reservation:self.store.usage_complete(reservation)
                    metrics['chunks']+=1;metrics['characters']+=len(sentence);reply.append(sentence)
                    accepted(key,'tts')
                except Exception as exc:raise ProviderError(failed(key,'tts',exc)) from None
                if not current():break
                self.store.record_utterance(turn_id,index,sentence,path,duration)
                if metrics['first_audio_ready_ms'] is None:metrics['first_audio_ready_ms']=round((self.clock()-start)*1000,2)
                publish('audio',index=index,text=sentence,audio='/conversation/audio/'+name,duration=duration,visemes=timing.get('visemes',[]),mouth_plan=timing.get('mouth_plan',[]),lip_sync=timing.get('lip_sync',timing['source']),tts_ms=round((self.clock()-tts_start)*1000,2))
            if cancel.is_set():status='cancelled'
            elif self.clock()>=deadline:raise ProviderError('TURN_DEADLINE')
            elif not reply:raise ProviderError('EMPTY_GENERATED_REPLY')
        except Exception as exc:status='failed';code=str(exc) if isinstance(exc,ProviderError) else 'SPEECH_PIPELINE_FAILED'
        finally:
            # Uncertain calls and partial speech are retained, never retried automatically.
            # Stop producers immediately on failure. On success let already-running
            # reaction bookkeeping finish before invalidating its current() fence.
            if status!='completed':worker_stop.set()
            for thread in aux:thread.join(timeout=self.s.timeout+.2)
            worker_stop.set()
            metrics['total_ms']=round((self.clock()-start)*1000,2)
            self.store.finish(turn_id,' '.join(reply),status,metrics)
        if not cancel.is_set():emit({'kind':'complete' if status=='completed' else 'error','status':status,'code':code,'metrics':metrics})
        return metrics


class ConversationBridge:
    """One turn worker. Old events and duplicate chunk claims cannot reach playout."""
    def __init__(self,settings,store,output,fence,lock,*,providers=None,memory_extractor=None):
        self.controller=SpeechController(settings,store,output,providers=providers,memory_extractor=memory_extractor);self.fence=fence;self.lock=lock
        self.events=[];self.cancel_event=threading.Event();self.worker=None;self.token=None;self.claims=set();self.finished=False

    def begin(self,comment,viewer='preview',*,on_begin=None,on_terminal=None):
        if not isinstance(comment,str) or not 0<len(comment.strip())<=1000 or not isinstance(viewer,str) or not 0<len(viewer)<=100:raise ValueError('INVALID_COMMENT')
        with self.lock:
            if self.worker and self.worker.is_alive():raise ValueError('TURN_BUSY')
            self.viewer=viewer
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
                try:self.controller.run(comment,viewer,token['turn_id'],emit,self.cancel_event)
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

    def mark_played(self,token):
        with self.lock:
            audio_count=sum(e['kind']=='audio' for e in self.events)
            if not self.fence.current(token) or not self.finished or len(self.claims)!=audio_count or not any(e['kind']=='complete' for e in self.events):return False
            s=self.controller.s
            accepted=self.controller.store.played(token['turn_id'],scope=s.mode,memory_enabled=s.memory_enabled,
                memory_ttl_days=s.memory_ttl_days,memory_capacity=s.memory_queue_size,
                memory_policy=self.controller.memory_worker.policy,memory_model=self.controller.memory_worker.model,
                memory_min_confidence=s.memory_min_confidence,relationship_enabled=s.memory_enabled and s.relationship_enabled)
            if accepted and s.memory_enabled and self.controller.store.memory_pending(self.viewer,s.mode):
                self.controller.memory_worker.wake()
            return accepted
