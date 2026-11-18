"""Streaming developer conversations with durable calls, playback fencing and memory."""
import asyncio
from array import array
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import re
import threading
import time
from types import SimpleNamespace
import uuid

from ravora.comments import Comment,CommentInbox,clean
from ravora.input_records import InputRecords
from ravora.edge_grpc import EdgeError,pcm_wave
from ravora.early_reaction import EarlyReactions
from ravora.memory_worker import MemoryWorker
from ravora.persona import conversation_prompt
from ravora.providers import ProviderError
from ravora.safety import TurnFence


def error_code(exc):
    if isinstance(exc,(EdgeError,ProviderError)):return str(exc)
    code=getattr(exc,'code',None)
    if isinstance(code,int):return 'PROVIDER_HTTP_'+str(code)
    return {'TimeoutError':'TURN_DEADLINE','DeadlineExceeded':'PROVIDER_DEADLINE','ServiceUnavailable':'PROVIDER_CONNECTION_FAILED',
        'CancelledError':'TURN_CANCELLED'}.get(type(exc).__name__,'VOICE_PIPELINE_FAILED')


class FastVoiceSession:
    def __init__(self,settings,store,output,clients,send_json,send_pcm,*,mock=False,phone_aligner=None,dialogue_reactions=False):
        self.s=replace(settings,mode='offline' if mock else 'cloud',comment_source='developer')
        self.prompt=conversation_prompt(self.s.persona,self.s.persona_file,reply_style=self.s.reply_style)
        self.store=store;self.output=Path(output);self.clients=clients;self.send=send_json;self.send_pcm=send_pcm;self.mock=mock
        self.phone_aligner=phone_aligner;self.dialogue_reactions=dialogue_reactions
        self.inbox=CommentInbox(store,capacity=settings.comment_queue_size,ttl=settings.comment_ttl,
            scope=self.s.mode,relationships_enabled=self.s.memory_enabled and self.s.relationship_enabled)
        self.records=InputRecords(store,self.inbox.session,'mock' if mock else 'cloud',reaction_limit=self.s.early_reaction_max_requests)
        self.memory=MemoryWorker(store,self.s);self.memory.wake();self.fence=TurnFence();self.token=None;self.task=None
        self.phase='idle';self.input_id=None;self.key=None;self.receipt=None;self.viewer=None;self.expected_frames=0
        self.cancelled=threading.Event();self.queue=None;self.audio_end=False;self.audio_size=0;self.power=0;self.hash=None
        self.closed=False;self.early=None
    def public(self):
        return {'profile':'fast_streaming','api_enabled':self.s.api_enabled,'stt_api_enabled':self.s.stt_api_enabled,
            'input_language':self.s.voice_input_language,'stt_region':self.s.voice_stt_region,'stt_model':self.s.voice_stt_model,
            'llm_model':self.s.gemini_model,'persona':self.s.persona,'reply_style':self.s.reply_style,'tts_voice':self.s.voice_tts_voice,'tts_region':self.s.voice_tts_region,
            'sample_rate':24000,'input_chunk_ms':self.s.voice_input_chunk_ms,'jitter_buffer_ms':self.s.voice_jitter_buffer_ms,
            'max_seconds':self.s.stt_max_seconds,'memory_enabled':self.s.memory_enabled,
            'early_reaction_enabled':self.s.early_reaction_enabled,'early_reaction_max_requests':self.s.early_reaction_max_requests,
            'phone_alignment':self.phone_aligner is not None,'mock':self.mock}
    def allowed(self):
        if self.closed:raise EdgeError('SESSION_CLOSED')
        if not self.mock and not (self.s.api_enabled and self.s.stt_api_enabled):raise EdgeError('API_DISABLED')
        if self.phase not in ('idle','played','failed','cancelled') or self.task and not self.task.done():raise EdgeError('DEVELOPER_BUSY')
    def prepare(self,input_id,kind,payload):
        self.allowed()
        key=self.records.from_client(input_id)
        try:self.records.info(key)
        except ValueError:pass
        else:raise EdgeError('INPUT_ALREADY_RECORDED')
        key,_=self.records.create(input_id,kind,payload)
        self.fence.cancel();self.token=None
        self.input_id=input_id;self.key=key;self.cancelled=threading.Event();self.receipt=None;self.expected_frames=0
        self.early=None
        return key
    def input_current(self,key):
        return (self.key==key and not self.closed and not self.cancelled.is_set()
            and self.phase not in ('played','failed','cancelled'))
    def make_reactions(self,key,input_id):
        def current():
            return self.input_current(key) and (self.token is None or self.source_current(self.token['turn_id']))
        async def emit(value):
            if not current():return
            if self.token is not None:value={**value,'token':self.token}
            await self.send(value)
        return EarlyReactions(self.s,self.store,key,input_id,current,emit,mock=self.mock,dialogue=self.dialogue_reactions)
    async def start_audio(self,input_id,language):
        if language not in ('en-US','ja-JP'):raise EdgeError('INVALID_INPUT_LANGUAGE')
        key=self.prepare(input_id,'audio',b'');self.phase='listening';self.queue=asyncio.Queue(maxsize=16)
        self.audio_end=False;self.audio_size=0;self.power=0;self.hash=hashlib.sha256()
        # Reserve the maximum duration before an open-ended microphone RPC.
        # Actual transmitted duration is separately retained in diagnostics, never refunded.
        self.records.prepare_audio(key,SimpleNamespace(duration=self.s.stt_max_seconds),self.s,'cloud_speech_grpc',self.s.voice_stt_model)
        if not self.mock and not self.records.dispatch(key,self.s):
            self.phase='failed';await self.send({'kind':'error','code':self.records.info(key)['code']});return
        if self.mock:self.records.state(key,'simulated')
        if self.s.early_reaction_enabled:
            self.early=self.make_reactions(key,input_id)
        self.task=asyncio.create_task(self.audio_run(key,language))
        await self.send({'kind':'listening','input_id':input_id})
    async def audio_chunk(self,input_id,data):
        if input_id!=self.input_id or self.phase!='listening' or self.audio_end:raise EdgeError('STALE_MIC_AUDIO')
        if not data or len(data)%2 or len(data)>6400 or self.audio_size+len(data)>self.s.stt_max_seconds*32000:raise EdgeError('INPUT_AUDIO_LIMIT')
        samples=array('h');samples.frombytes(data)
        if __import__('sys').byteorder!='little':samples.byteswap()
        self.power+=sum(x*x for x in samples);self.audio_size+=len(data);self.hash.update(data)
        try:self.queue.put_nowait(data)
        except asyncio.QueueFull:raise EdgeError('INPUT_BACKPRESSURE_LIMIT') from None
    async def end_audio(self,input_id):
        if input_id!=self.input_id or self.phase!='listening' or self.audio_end:raise EdgeError('STALE_MIC_END')
        self.audio_end=True
        rms=(self.power/max(1,self.audio_size//2))**.5/32768
        if self.audio_size<6400 or rms<self.s.stt_min_rms:
            await self.cancel('NO_SPEECH');return
        try:self.queue.put_nowait(None)
        except asyncio.QueueFull:raise EdgeError('INPUT_BACKPRESSURE_LIMIT') from None
        self.phase='transcribing'
    async def audio_run(self,key,language):
        begin=time.monotonic();input_id=self.input_id;early=self.early
        async def frames():
            while True:
                value=await self.queue.get()
                if value is None:return
                yield value
        async def emit(value):
            if self.input_current(key):
                if early is not None and value.get('kind')=='stt_partial':early.offer(value)
                await self.send({**value,'input_id':input_id,'stt_elapsed_ms':(time.monotonic()-begin)*1000})
        try:
            text,diagnostics=await self.clients.transcribe_frames(frames(),emit,model=self.s.voice_stt_model,
                timeout=self.s.stt_timeout,language=language)
            diagnostics.update(actual_audio_seconds=self.audio_size/32000,pcm_sha256=self.hash.hexdigest(),reserved_audio_seconds=self.s.stt_max_seconds)
            self.records.usage(key,{'local_diagnostics':diagnostics},(time.monotonic()-begin)*1000)
            with self.store.connect() as db:db.execute('UPDATE developer_inputs SET payload_sha256=? WHERE id=?',(self.hash.hexdigest(),key))
            if early is not None:early.seal(clean(text,1000))
            if not self.input_current(key):return
            await self.respond(clean(text,1000),language)
        except BaseException as exc:
            if early is not None:await early.close()
            self.records.usage(key,{'local_diagnostics':{'actual_audio_seconds':self.audio_size/32000,
                'reserved_audio_seconds':self.s.stt_max_seconds}},(time.monotonic()-begin)*1000)
            if self.input_current(key):
                code=error_code(exc);self.records.state(key,'outcome_unknown' if 'CONNECTION' in code or 'DEADLINE' in code else 'failed',code)
                self.phase='failed';await self.send({'kind':'reaction_reset','input_id':input_id})
                await self.send({'kind':'error','code':code,'input_id':input_id})
        finally:
            if early is not None and not self.input_current(key):await early.close()
    async def start_text(self,input_id,text):
        text=clean(text,1000);key=self.prepare(input_id,'text',text.encode());self.phase='responding'
        self.task=asyncio.create_task(self.respond(text,'und'))
    def current(self,token):
        return self.fence.current(token) and not self.cancelled.is_set() and not self.closed
    def source_current(self,turn):
        with self.store.connect() as db:
            row=db.execute('SELECT t.status,s.is_deleted,s.raw_purged_at FROM conversation_turns t JOIN source_events s ON s.id=t.source_event_id WHERE t.turn_id=?',(turn,)).fetchone()
        return bool(row and row[0] not in ('cancelled','source_removed','source_expired') and row[1]==0 and row[2] is None)
    async def respond(self,text,language):
        key=self.key;input_id=self.input_id;begin=time.monotonic();token=None;tasks=[];stop=asyncio.Event();queue=asyncio.Queue(maxsize=4);errors=[];reply=[];audio=bytearray()
        metrics={'profile':'fast_streaming','first_text_ms':None,'first_pcm_ms':None,'characters':0,'pcm_packets':0,'provider_requests':0}
        try:
            comment=Comment('developer-mock' if self.mock else 'developer',self.inbox.session,input_id,self.s.developer_id,'Developer',text,time.time())
            if self.inbox.put(comment)!='queued':raise EdgeError('INPUT_NOT_QUEUED')
            selected=self.inbox.take()
            if not selected or selected['receipt']!=comment.receipt:raise EdgeError('INPUT_QUEUE_MISMATCH')
            _,viewer=self.inbox.claimed(comment.receipt);token=self.fence.begin(uuid.uuid4().hex);self.token=token;turn=token['turn_id']
            if not self.inbox.bind(comment.receipt,turn):raise EdgeError('COMMENT_NO_LONGER_AVAILABLE')
            self.receipt=comment.receipt;self.viewer=viewer;self.phase='responding'
            self.records.state(key,'accepted',language=language,source=comment.receipt);self.store.start(turn,self.s.mode+':'+viewer,text)
            if self.early is None:self.early=self.make_reactions(key,input_id)
            self.early.bind(turn)
            await self.send({'kind':'turn','input_id':input_id,'token':token,'text':text})
            self.early.finalize(text)
            ready=await asyncio.to_thread(self.memory.wait_for,viewer,cancel=self.cancelled)
            if not ready:raise EdgeError('MEMORY_WORKER_NOT_READY')
            history=self.store.history(self.s.mode+':'+viewer,self.s.memory_turns)
            elapsed=lambda:(time.monotonic()-begin)*1000
            async def emit(value):
                if self.current(token):await self.send({**value,'token':token,'input_id':input_id,'server_elapsed_ms':elapsed()})
            def prepare(provider,operation,model):
                call=self.store.prepare_call(turn,provider,operation,{'profile':'fast_streaming'},model=model,mode=self.s.mode)
                if not self.current(token) or not self.source_current(turn):
                    self.store.call_state(call,'cancelled_before_send');raise EdgeError('TURN_CANCELLED')
                self.store.call_state(call,'dispatched' if not self.mock else 'simulated',attempted=not self.mock)
                if not self.mock:metrics['provider_requests']+=1
                return call
            def failed(call,exc):
                code=error_code(exc);unknown='CONNECTION' in code or 'DEADLINE' in code or code=='TURN_CANCELLED'
                self.store.call_state(call,'outcome_unknown' if unknown else 'failed',code=code)
            async def put(value):
                while not stop.is_set():
                    try:await asyncio.wait_for(queue.put(value),.1);return
                    except TimeoutError:pass
            async def generate():
                call=None
                try:
                    call=prepare('gemini','stream_generate',self.s.gemini_model)
                    notes,strategy=self.store.recall_memory(viewer,text,scope=self.s.mode) if self.s.memory_enabled else ([], 'disabled')
                    self.store.record_retrievals(turn,notes,context_prepared=True,strategy=strategy,call_id=call)
                    metrics['memory_ids']=[x['memory_id'] for x in notes]
                    context=self.store.recall_context(viewer,text,scope=self.s.mode,developer_id=self.s.developer_id,
                        limit=self.s.context_max_items,max_chars=self.s.context_max_chars) if self.s.memory_enabled and self.s.context_enabled else []
                    self.store.record_context_retrievals(turn,context,call_id=call,prepared=True)
                    metrics['context_tiers']=[e['tier'] for e in context];metrics['context_items']=len(context)
                    lookup=time.monotonic()
                    background=self.store.relationship_context(viewer,scope=self.s.mode) if self.s.memory_enabled and self.s.relationship_enabled else None
                    metrics['relationship_lookup_ms']=round((time.monotonic()-lookup)*1000,3)
                    async for delta in self.clients.generate(text,model=self.s.gemini_model,prompt=self.prompt,history=history,memories=notes,context=context,relationship=background):
                        if not self.current(token) or not self.source_current(turn):raise EdgeError('TURN_CANCELLED')
                        metrics['characters']+=len(delta)
                        if metrics['characters']>self.s.max_reply_chars:raise EdgeError('REPLY_CHARACTER_LIMIT')
                        if metrics['first_text_ms'] is None:metrics['first_text_ms']=elapsed()
                        reply.append(delta);await emit({'kind':'text','text':delta});await put(delta)
                    self.store.call_state(call,'accepted')
                except BaseException as exc:
                    if call:failed(call,exc)
                    errors.append(error_code(exc))
                finally:
                    if call:self.store.call_usage(call,getattr(self.clients,'llm_usage',{}))
                    await put(None)
            reservation=self.store.reserve(self.s.max_reply_chars,self.s.monthly_tts_chars) if not self.mock else None
            tasks=[asyncio.create_task(generate())]
            call=prepare('tts','streaming_synthesize',self.s.voice_tts_voice)
            async def texts():
                while True:
                    value=await queue.get()
                    if value is None:
                        if errors:raise EdgeError(errors[0])
                        return
                    yield value
            try:
                async with asyncio.timeout(self.s.turn_timeout):
                    async for pcm in self.clients.synthesize_stream(texts()):
                        if not self.current(token) or not self.source_current(turn):raise EdgeError('TURN_CANCELLED')
                        if len(pcm)%2 or len(audio)+len(pcm)>30*48000:raise EdgeError('PCM_LIMIT_OR_ALIGNMENT')
                        if metrics['first_pcm_ms'] is None:metrics['first_pcm_ms']=elapsed()
                        audio.extend(pcm)
                        for index in range(0,len(pcm),4800):
                            if not self.current(token):raise EdgeError('TURN_CANCELLED')
                            await self.send_pcm(token,pcm[index:index+4800]);metrics['pcm_packets']+=1
                    await asyncio.gather(*tasks)
                if errors:raise EdgeError(errors[0])
                if not audio or not reply:raise EdgeError('EMPTY_GENERATED_REPLY')
                self.store.call_state(call,'accepted')
                if reservation:self.store.usage_complete(reservation)
            except BaseException as exc:failed(call,exc);raise
            if not self.source_current(turn):raise EdgeError('COMMENT_NO_LONGER_AVAILABLE')
            path=self.output/(turn+'-0.wav');path.write_bytes(pcm_wave(audio,24000));self.expected_frames=len(audio)//2
            self.store.record_utterance(turn,0,''.join(reply),path,self.expected_frames/24000)
            # The worklet can drain a short reply while its phonemes are still being estimated.
            await emit({'kind': 'audio_end', 'frames': self.expected_frames})
            if self.phone_aligner is not None:
                try:
                    aligned = await asyncio.to_thread(self.phone_aligner, ''.join(reply), bytes(audio))
                except Exception:
                    aligned = {'status': 'alignment_failed'}
                if not self.current(token) or not self.source_current(turn):
                    raise EdgeError('TURN_CANCELLED')
                metrics['phone_alignment'] = aligned['status']
                if aligned['status'] == 'aligned':
                    metrics['phone_alignment_ms'] = aligned['elapsed_ms']
                await emit({'kind': 'phonemes', **aligned})
            metrics.update(jev_requests=self.early.sequence,
                early_reaction_requests=self.early.phase_requests['partial'],final_reaction_requests=self.early.phase_requests['final'],
                early_reactions_emitted=self.early.phase_emitted['partial'])
            if not self.mock:metrics['provider_requests']+=self.early.sequence
            metrics.update(total_ms=elapsed(),pcm_frames=self.expected_frames)
            self.store.finish(turn,''.join(reply),'completed',metrics);self.inbox.finish(self.receipt,'generated');self.phase='generated'
            await emit({'kind':'complete','metrics':metrics})
        except BaseException as exc:
            code=error_code(exc)
            if token:
                self.store.finish(token['turn_id'],''.join(reply),'cancelled' if self.cancelled.is_set() else 'failed',metrics)
                if self.receipt:self.inbox.finish(self.receipt,'cancelled' if self.cancelled.is_set() else 'failed',code)
            if not self.cancelled.is_set():self.phase='failed';await self.send({'kind':'error','code':code,'input_id':input_id,'token':token})
        finally:
            stop.set()
            for task in tasks:
                if not task.done():task.cancel()
            if tasks:await asyncio.gather(*tasks,return_exceptions=True)
            if self.early is not None and not self.input_current(key):await self.early.close()
    def played(self,token,frames):
        if self.phase!='generated' or not self.current(token) or type(frames) is not int or frames!=self.expected_frames or not self.source_current(token['turn_id']):return False
        self.store.chunk_claimed(token['turn_id'],0)
        accepted=self.store.played(token['turn_id'],scope=self.s.mode,memory_enabled=self.s.memory_enabled,memory_ttl_days=self.s.memory_ttl_days,
            memory_capacity=self.s.memory_queue_size,memory_policy=self.memory.policy,memory_model=self.memory.model,memory_min_confidence=self.s.memory_min_confidence,
            relationship_enabled=self.s.memory_enabled and self.s.relationship_enabled)
        if accepted:
            self.inbox.finish(self.receipt,'played');self.phase='played'
            if self.early is not None:self.early.stop()
            if self.s.memory_enabled:self.memory.wake()
        return accepted
    async def cancel(self,reason='DEVELOPER_CANCELLED'):
        previous=self.phase;self.cancelled.set();token=self.token;input_id=self.input_id
        if self.early is not None:await self.early.close()
        self.fence.cancel();self.phase='cancelled'
        if previous!='played':
            if self.key:self.records.state(self.key,'cancelled',reason)
            if token:self.store.cancel_turn(token['turn_id'])
            if self.receipt:self.inbox.finish(self.receipt,'cancelled',reason)
        if self.task and not self.task.done():self.task.cancel();await asyncio.gather(self.task,return_exceptions=True)
        self.key=None;self.receipt=None;self.token=None
        await self.send({'kind':'reaction_reset','input_id':input_id})
        await self.send({'kind':'cancelled','code':reason,'input_id':input_id})
    async def close(self):
        if self.closed:return
        self.closed=True;await self.cancel('SESSION_CLOSED');self.inbox.close();await asyncio.to_thread(self.memory.close)
