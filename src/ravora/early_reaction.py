"""Bounded partial/final Jev stages, with durable reservations and monotonic results."""
import asyncio
from contextvars import ContextVar
import hashlib
import json
import re
import time
import uuid

from ravora.comments import clean
from ravora.providers import JevFinalConnection, JevReaction, ProviderError


def normalized(text):
    return ' '.join(re.findall(r"[^\W_]+(?:'[^\W_]+)*",text.casefold().replace('’',"'")))


class EarlyReactions:
    def __init__(self, settings, store, input_key, input_id, current, emit, *, mock=False, classify=None, dialogue=False):
        self.s=settings;self.store=store;self.key=input_key;self.input_id=input_id
        self.current=current;self.emit=emit;self.mock=mock;self.classify=classify or self._classify;self.dialogue=dialogue
        self.active=True;self.sealed=False;self.latest=None;self.changed_at=0.;self.turn=None;self.value=None
        self.sequence=0;self.emitted=0;self.request_phase=None;self.call_id=None
        self.phase_requests={'partial':0,'final':0};self.phase_emitted={'partial':0,'final':0}
        self.calls={};self.children=[];self.partial_started=False;self.final_started=False;self.finalized=False
        self._phase=ContextVar('reaction_phase',default='final')
        self.words=[];self.word_since=[];self.word_updates=[]
        self.candidates=[];self.semantic=None;self.semantic_viewer=None;self.previous_message=''
        self.final_done=asyncio.Event()
        self.semantic_ready_at=None
        self.connection=None;self.warm_task=None
        self.wake=asyncio.Event();self.task=asyncio.create_task(self.run())

    def warm_final_connection(self):
        if (self.mock or not self.s.api_enabled or not self.s.stt_api_enabled or not self.s.memory_enabled
                or self.s.semantic_memory_mode != 'jev' or self.connection is not None):
            return
        self.connection = JevFinalConnection(self.s)
        self.warm_task = asyncio.create_task(asyncio.to_thread(self.connection.warm))

    async def _classify(self,text):
        if self.mock:return {'reaction':'joy' if self._phase.get()=='partial' else 'neutral','source':'mock'},{}
        def call():
            transport=self.connection if self._phase.get()=='final' else None
            adapter=JevReaction(self.s, transport) if transport else JevReaction(self.s)
            candidates=self.candidates if self._phase.get()=='final' else []
            if candidates:
                candidates=self.store.revalidate_episode_candidates(self.semantic_viewer,text,self.s,candidates)
            value=adapter.classify(text,dialogue=self.dialogue,candidates=candidates,previous_message=self.previous_message)
            return value,adapter.usage
        return await asyncio.to_thread(call)

    def offer(self,value):
        if not self.active or self.sealed or not self.s.early_reaction_enabled or not self.current():return
        text=clean(value.get('text',''),1000)
        if not text:return
        stability=value.get('stability')
        if stability is not None and (type(stability) not in (int,float) or not 0<=stability<=1):return
        key=normalized(text)
        now=time.monotonic();words=key.split();common=0
        for old,new in zip(self.words,words):
            if old!=new:break
            common+=1
        self.word_since=self.word_since[:common]+[now]*(len(words)-common)
        self.word_updates=[n+1 for n in self.word_updates[:common]]+[1]*(len(words)-common)
        self.words=words
        if self.latest is None or key!=self.latest['key']:self.changed_at=now
        # Some streaming models report very low stability even for repeated words.
        # Require three unchanged leading words across two notifications, plus a
        # fourth word of context, before using that provisional hypothesis once.
        prefix_ready=(self.word_since[2]+self.s.early_reaction_debounce_ms/1000
            if len(words)>=4 and self.word_updates[2]>=2 else None)
        self.latest={'text':text,'key':key,'stability':stability,'segment_final':value.get('segment_final') is True,
            'prefix_ready_at':prefix_ready,'phase':'partial'}
        self.wake.set()

    def seal(self,text):
        # Stop new partial requests without cancelling one already in flight.
        self.sealed=True
        self.latest={'text':text,'key':normalized(text),'phase':'sealed'}
        self.wake.set()

    def finalize(self,text):
        if self.finalized:return
        self.finalized=True
        self.sealed=True
        self.latest={'text':text,'key':normalized(text),'phase':'final'}
        self.wake.set()

    def prepare(self,snapshot):
        if not self.current() or not self.mock and not (self.s.api_enabled and self.s.stt_api_enabled):return None
        if snapshot['phase']=='final':
            if self.turn is None:return None
            key=self.store.prepare_call(self.turn,'jev','reaction',{'profile':'fast_streaming','characters':len(snapshot['text'])},
                model=self.s.jev_model,mode='offline' if self.mock else 'cloud',reaction_input_id=self.key)
        else:
            with self.store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                row=db.execute('SELECT input_state FROM developer_inputs WHERE id=?',(self.key,)).fetchone()
                if not row or row[0] not in ('dispatched','simulated'):return None
                key=uuid.uuid4().hex;now=time.time()
                if not self.store.claim_input_reaction(db,self.key,'partial',key,now):return None
                db.execute('''INSERT INTO early_reaction_calls
                    (id,input_id,sequence,model,mode,text_sha256,characters,state,attempted,created_at,updated_at)
                    VALUES(?,?,1,?,?,?,?,'prepared',0,?,?)''',
                    (key,self.key,self.s.jev_model,'mock' if self.mock else 'cloud',
                     hashlib.sha256(snapshot['text'].encode()).hexdigest(),len(snapshot['text']),now,now))
        if key is not None:
            self.sequence+=1;self.call_id=key;self.request_phase=snapshot['phase']
            self.calls[key]=snapshot['phase'];self.phase_requests[snapshot['phase']]+=1
        return key

    def finish(self,key,state,*,attempted=False,value=None,usage=None,code=None,emitted=False):
        if self.calls[key]=='final':
            self.store.call_state(key,state,attempted=attempted,code=code)
            if usage:self.store.call_usage(key,usage)
            return
        now=time.time()
        with self.store.connect() as db:
            db.execute('''UPDATE early_reaction_calls SET state=?,attempted=MAX(attempted,?),updated_at=?,
                latency_ms=MAX(0,(?-created_at)*1000),reaction=COALESCE(?,reaction),usage=?,code=?,
                emitted_at=CASE WHEN ? THEN ? ELSE emitted_at END WHERE id=?''',
                (state,int(attempted),now,now,(value or {}).get('reaction'),json.dumps(usage or {}),code,int(emitted),now,key))

    @staticmethod
    def _turn_reaction(db,turn,payload):
        db.execute("""UPDATE conversation_turns SET reaction=?,
            state=CASE WHEN status='running' THEN 'REACTING' ELSE state END
            WHERE turn_id=? AND (status='running' OR (status='completed' AND played=0))""",(payload,turn))

    def accept(self,key,snapshot,value):
        rank=2 if snapshot['phase']=='final' else 1;now=time.time()
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            previous=db.execute('SELECT reaction_result FROM developer_inputs WHERE id=?',(self.key,)).fetchone()
            before=json.loads(previous[0])['reaction'] if previous and previous[0] else 'neutral'
            if (rank == 2 and before != value['reaction']
                    and before in value.get('continuity_reactions', ())):
                value = {**value, 'reaction':before, 'jev_recommendation':value['reaction'],
                         'reaction_selection':'kept_supported_expression'}
            payload = json.dumps(value)
            changed=db.execute('''UPDATE developer_inputs SET reaction_applied_sequence=?,reaction_result=?
                WHERE id=? AND reaction_applied_sequence<? AND input_state IN ('dispatched','simulated','accepted')
                  AND EXISTS(SELECT 1 FROM reaction_stage_claims WHERE input_id=developer_inputs.id AND stage=? AND call_id=?)
                  AND (source_event_id IS NULL OR EXISTS(
                    SELECT 1 FROM conversation_turns t JOIN source_events s ON s.id=t.source_event_id
                    WHERE t.source_event_id=developer_inputs.source_event_id AND t.played=0
                    AND t.status IN ('running','completed') AND s.is_deleted=0 AND s.raw_purged_at IS NULL AND s.expires_at>?))''',
                (rank,payload,self.key,rank,snapshot['phase'],key,now))
            if not changed.rowcount:return None
            if self.turn is not None:self._turn_reaction(db,self.turn,payload)
        self.value=value
        transition='continue' if before==value['reaction'] else 'return_neutral' if value['reaction']=='neutral' else 'switch'
        return value, {'reaction_sequence':rank,'previous_reaction':before,'transition':transition}

    async def run(self):
        while self.active:
            await self.wake.wait();self.wake.clear()
            if not self.active or not self.current() or self.latest is None:continue
            snapshot=dict(self.latest)
            if snapshot['phase']=='sealed':continue
            if snapshot['phase']=='final':
                if self.turn is None:continue
                if not self.final_started:
                    self.final_started=True
                    key=self.prepare(snapshot)
                    if key is not None:self.children.append(asyncio.create_task(self.evaluate(key,snapshot)))
                    else:self.final_done.set()
                break
            if snapshot['phase']=='partial':
                if self.partial_started or self.sealed:continue
                if len(snapshot['key'])<4:continue
                stability=snapshot['stability']
                now=time.monotonic();delay=None
                if snapshot['segment_final'] or stability is not None and stability>=self.s.early_reaction_stability:delay=0
                elif stability is None:delay=max(0,self.changed_at+self.s.early_reaction_debounce_ms/1000-now)
                if snapshot.get('prefix_ready_at') is not None:
                    prefix_delay=max(0,snapshot['prefix_ready_at']-now)
                    delay=prefix_delay if delay is None else min(delay,prefix_delay)
                if delay is None:continue
                if delay>0:
                    try:await asyncio.wait_for(self.wake.wait(),delay)
                    except TimeoutError:self.wake.set()
                    continue
            key=self.prepare(snapshot)
            self.partial_started=True
            if key is not None:self.children.append(asyncio.create_task(self.evaluate(key,snapshot)))
        if self.children:await asyncio.gather(*self.children,return_exceptions=True)

    async def evaluate(self,key,snapshot):
        if not self.active or not self.current():
            self.finish(key,'cancelled_before_send')
            return
        token=self._phase.set(snapshot['phase'])
        self.finish(key,'simulated' if self.mock else 'dispatched',attempted=not self.mock)
        value=None;usage=None;received=False
        try:
            async with asyncio.timeout(min(3,self.s.timeout)):
                value,usage=await self.classify(snapshot['text'])
            received=True
            semantic=value.pop('semantic',None)
            if not self.active or not self.current():
                self.finish(key,'discarded_after_cancel',value=value,usage=usage)
            elif snapshot['phase']=='partial' and not self.latest['key'].startswith(snapshot['key']):
                self.finish(key,'superseded',value=value,usage=usage)
            else:
                accepted=self.accept(key,snapshot,value)
                if accepted is None:
                    self.finish(key,'superseded_or_closed',value=value,usage=usage)
                    return
                value,update=accepted
                if snapshot['phase']=='final' and semantic is not None:
                    self.semantic=semantic
                    self.semantic_ready_at=time.monotonic()
                    self.store.record_semantic_retrieval(self.turn,snapshot['text'],self.candidates,semantic,call_id=key)
                self.finish(key,'accepted',value=value,usage=usage)
                if snapshot['phase']=='final':self.final_done.set()
                await self.emit({'kind':'reaction','phase':snapshot['phase'],'provisional':snapshot['phase']=='partial',
                    'input_id':self.input_id,**update,**value})
                self.finish(key,'accepted',value=value,usage=usage,emitted=True)
                self.emitted+=1;self.phase_emitted[snapshot['phase']]+=1
        except asyncio.CancelledError:
            self.finish(key,'discarded_after_cancel' if received or self.mock else 'outcome_unknown',
                value=value,usage=usage,code='INPUT_REACTION_CANCELLED')
            raise
        except Exception as exc:
            code=str(exc) if isinstance(exc,ProviderError) else 'INPUT_REACTION_TIMEOUT' if isinstance(exc,TimeoutError) else 'INPUT_REACTION_FAILED'
            state='accepted_delivery_unknown' if received else 'outcome_unknown' if isinstance(exc,TimeoutError) or 'CONNECTION' in code else 'failed'
            self.finish(key,state,value=value,usage=usage,code=code)
            if self.active and self.current():
                await self.emit({'kind':'reaction_unavailable','phase':snapshot['phase'],'input_id':self.input_id,'code':code})
        finally:
            if snapshot['phase']=='final':self.final_done.set()
            self._phase.reset(token)

    def stop(self):
        self.active=False;self.wake.set()
        if self.connection:self.connection.close()
        for child in self.children:
            if not child.done():child.cancel()

    async def close(self):
        self.stop();await asyncio.gather(self.task,*self.children,return_exceptions=True)

    def bind(self,turn):
        self.turn=turn
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('UPDATE early_reaction_calls SET turn_id=? WHERE input_id=?',(turn,self.key))
            row=db.execute('SELECT reaction_result FROM developer_inputs WHERE id=?',(self.key,)).fetchone()
            if row and row[0]:
                self.value=json.loads(row[0]);self._turn_reaction(db,turn,row[0])
        self.wake.set()
