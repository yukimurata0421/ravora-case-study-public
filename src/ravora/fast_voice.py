"""Streaming developer conversations with durable calls, playback fencing and memory."""
import asyncio
from array import array
from collections import deque
from contextlib import aclosing
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
from ravora.dell_microphone import open_microphone
from ravora.input_records import InputRecords
from ravora.edge_grpc import EdgeClients,EdgeError,pcm_wave
from ravora.early_reaction import EarlyReactions
from ravora.memory_worker import MemoryWorker
from ravora.persona import (AVATAR_NAME, DEVELOPER_SPEAKER, JAPANESE_QUESTION_WINDOW, asked_listener_in_japanese,
                           conversation_prompt, japanese_question_direction, listener_instruction)
from ravora.providers import ProviderError
from ravora.safety import TurnFence
from ravora.settings import CONVERSATION_LANGUAGES, streaming_voice
from ravora.semantic_memory import load_speech_background
from ravora.conversation import SentenceBuffer
from ravora.ray_character import CharacterError,CharacterRuntime,PublicDecoder,PublicOutput,initiative_message,public_prompt
from ravora.dialogue_router import DialogueRoute
from ravora.ray_memory import claim_hook


def error_code(exc):
    if isinstance(exc,(EdgeError,ProviderError,CharacterError)):return str(exc)
    code=getattr(exc,'code',None)
    if isinstance(code,int):return 'PROVIDER_HTTP_'+str(code)
    return {'TimeoutError':'TURN_DEADLINE','DeadlineExceeded':'PROVIDER_DEADLINE','ServiceUnavailable':'PROVIDER_CONNECTION_FAILED',
        'CancelledError':'TURN_CANCELLED'}.get(type(exc).__name__,'VOICE_PIPELINE_FAILED')


class SpeechEndpoint:
    """Per-input speech activity, measured on the captured 16 kHz audio timeline."""

    def __init__(self, silence_ms, *, local_onset=False):
        self.silence_ms = silence_ms
        self.local_onset = local_onset
        self.event_ms = -1
        self.speech_ended = False
        self.last_loud_ms = 0 if local_onset else None
        self.has_text = False

    def activity(self, event):
        offset = event['offset_ms']
        if offset < self.event_ms:
            return
        self.event_ms = offset
        self.speech_ended = event['type'] == 'SPEECH_ACTIVITY_END'

    def observe_pcm(self, end_ms, power, samples):
        # Local energy vetoes a late cloud END if the speaker has already resumed.
        if power >= samples * (32768 * .01) ** 2:  # -40 dBFS RMS
            self.last_loud_ms = end_ms

    def end_reason(self, end_ms):
        if self.speech_ended:
            pause = self.silence_ms
            reason = 'speech_activity_end'
        elif self.last_loud_ms is not None and (self.has_text or self.local_onset):
            # A noise-triggered hands-free input may receive neither text nor activity events.
            pause = max(1500, 2 * self.silence_ms)
            reason = 'transcript_silence' if self.has_text else 'onset_silence'
        else:
            return None
        if end_ms - max(self.event_ms, self.last_loud_ms or 0) >= pause:
            return reason
        return None


class SpeechOnset:
    """Keep 240 ms of local PCM and require 80 ms of consecutive voiced energy."""
    def __init__(self, threshold_dbfs, *, wait_for_quiet=False, sample_rate=16000):
        samples = sample_rate // 50
        self.frame_bytes = samples * 2
        self.frame_power_threshold = samples * 32768**2 * 10**(threshold_dbfs / 10)
        self.history = deque(maxlen=12)
        self.pending = bytearray()
        self.voiced = 0
        self.quiet_needed = 10 if wait_for_quiet else 0

    def feed(self, data):
        self.pending.extend(data)
        while len(self.pending) >= self.frame_bytes:
            frame = bytes(self.pending[:self.frame_bytes])
            del self.pending[:self.frame_bytes]
            self.history.append(frame)
            samples = array('h', frame)
            if __import__('sys').byteorder != 'little':
                samples.byteswap()
            loud = sum(x*x for x in samples) >= self.frame_power_threshold
            if self.quiet_needed:
                # If STT rejected a still-open utterance, do not submit its tail as a new one.
                self.quiet_needed = 10 if loud else self.quiet_needed - 1
                if not self.quiet_needed:
                    self.history.clear()
                continue
            self.voiced = self.voiced + 1 if loud else 0
            if self.voiced >= 4:
                return b''.join(self.history) + bytes(self.pending)
        return None


class FastVoiceSession:
    def __init__(self,settings,store,output,clients,send_json,send_pcm,*,mock=False,phone_aligner=None,dialogue_reactions=False,end_silence_ms=800,evaluation=None):
        self.s=replace(settings,mode='offline' if mock else 'cloud',comment_source='developer')
        self.conversation_language=self.s.voice_input_language
        self.recent_questions=deque(maxlen=JAPANESE_QUESTION_WINDOW)
        self.generated_asked=None
        self.prompts={lang:conversation_prompt(self.s.persona,self.s.persona_file,reply_style=self.s.reply_style,language=lang)
                      for lang in CONVERSATION_LANGUAGES}
        self.store=store;self.output=Path(output);self.clients=clients;self.send=send_json;self.send_pcm=send_pcm;self.mock=mock
        self.phone_aligner=phone_aligner;self.dialogue_reactions=dialogue_reactions
        self.inbox=CommentInbox(store,capacity=settings.comment_queue_size,ttl=settings.comment_ttl,
            scope=self.s.mode,relationships_enabled=self.s.memory_enabled and self.s.relationship_enabled)
        self.records=InputRecords(store,self.inbox.session,'mock' if mock else 'cloud',reaction_limit=self.s.early_reaction_max_requests)
        self.memory=MemoryWorker(store,self.s);self.memory.wake();self.fence=TurnFence();self.token=None;self.task=None
        self.phase='idle';self.input_id=None;self.key=None;self.receipt=None;self.viewer=None;self.expected_frames=0
        self.cancelled=threading.Event();self.queue=None;self.audio_end=False;self.audio_size=0;self.power=0;self.hash=None
        self.closed=False;self.early=None
        self.capture_task=None;self.input_source=None;self.capture_pending=bytearray()
        self.end_silence_ms=end_silence_ms;self.endpoint=None;self.onset=None;self.end_reason=None
        self.wait_for_quiet=False
        self.mic_muted=False
        self.evaluation=evaluation
        self.character=CharacterRuntime(idle_seconds=self.s.character_idle_seconds,max_initiatives=self.s.character_max_initiatives) if self.s.character_enabled else None
        self.character_epoch=0;self.initiative=False
    def evaluate(self,method,*args,**kwargs):
        if self.evaluation is not None:
            return self.evaluation.call(method,self.key,*args,**kwargs)
    def public(self):
        return {'profile':'fast_streaming','avatar_name':AVATAR_NAME,'api_enabled':self.s.api_enabled,'stt_api_enabled':self.s.stt_api_enabled,
            'input_language':self.s.voice_input_language,'stt_region':self.s.voice_stt_region,'stt_model':self.s.voice_stt_model,
            'llm_model':self.s.gemini_model,'persona':self.s.persona,'reply_style':self.s.reply_style,'tts_voice':streaming_voice(self.s.voice_tts_voice,self.s.voice_input_language),'tts_region':self.s.voice_tts_region,
            'tts_pitch_semitones':0 if self.mock else self.s.pitch,'tts_speaking_rate':self.s.speaking_rate,
            'sample_rate':24000,'input_chunk_ms':self.s.voice_input_chunk_ms,'jitter_buffer_ms':self.s.voice_jitter_buffer_ms,
            'handsfree_onset_dbfs':self.s.handsfree_onset_dbfs,
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
        self.initiative=False
        if self.character:
            self.character.cancel();self.character_epoch=self.character.epoch
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
            if self.character and value.get('kind')=='reaction':self.character.react(self.character_epoch,value.get('reaction'))
            if self.token is not None:value={**value,'token':self.token}
            await self.send(value)
        return EarlyReactions(self.s,self.store,key,input_id,current,emit,mock=self.mock,dialogue=self.dialogue_reactions)
    async def set_mic_muted(self, muted):
        self.mic_muted = muted
        if muted and self.phase in ('opening', 'armed', 'listening') and not self.audio_end:
            self.end_reason = 'MIC_MUTED'
            await self.cancel('MIC_MUTED')

    async def start_audio(self, input_id, language, *, dell_device=None, end_mode='manual'):
        if self.mic_muted:return
        if end_mode not in ('auto', 'manual', 'handsfree'):
            raise EdgeError('INVALID_END_MODE')
        if language not in CONVERSATION_LANGUAGES:
            raise EdgeError('INVALID_INPUT_LANGUAGE')
        key = self.prepare(input_id, 'audio', b'')
        self.end_reason = None
        self.end_mode = end_mode
        handsfree = end_mode == 'handsfree'
        self.endpoint = SpeechEndpoint(self.end_silence_ms, local_onset=handsfree) if end_mode != 'manual' else None
        self.onset = SpeechOnset(self.s.handsfree_onset_dbfs, wait_for_quiet=self.wait_for_quiet) if handsfree else None
        self.wait_for_quiet = False
        self.input_language = self.conversation_language = language
        self.queue = asyncio.Queue(maxsize=16)
        self.audio_end = False
        self.audio_size = self.power = 0
        self.hash = hashlib.sha256()
        self.input_source = 'windows' if dell_device is None else 'dell'
        self.evaluate('start',input_id,self.inbox.session,source=self.input_source,end_mode=end_mode,language=language,settings=self.s)
        if dell_device is None:
            if self.onset:
                self.phase = 'armed'
                await self.send({'kind':'armed','input_id':input_id})
            else:
                await self.start_stt(key, input_id, language)
        else:
            self.phase = 'opening'
            self.capture_pending.clear()
            self.capture_task = asyncio.create_task(self.capture_dell(key, input_id, language, dell_device))

    async def stop_capture(self):
        task = self.capture_task
        if task is None or task is asyncio.current_task():
            return
        self.capture_task = None
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    def flush_capture(self, input_id):
        # Only the incomplete final sample, if any, is discarded at release.
        size = len(self.capture_pending) & ~1
        if size:
            self.accept_pcm(input_id, bytes(self.capture_pending[:size]), source='dell')
            del self.capture_pending[:size]

    async def capture_dell(self, key, input_id, language, device):
        # The WebSocket remains responsive while the device opens. Reserve STT only after PCM arrives.
        chunk_bytes = self.s.voice_input_chunk_ms * 32
        limit = self.s.stt_max_seconds * 32000
        loop = asyncio.get_running_loop()
        reason = 'duration_limit'
        try:
            async with open_microphone(device) as stream:
                first = await asyncio.wait_for(stream.readexactly(min(640, chunk_bytes)), 2)
                if self.onset:
                    self.phase = 'armed'
                    await self.send({'kind':'armed','input_id':input_id})
                    buffered = self.onset.feed(first)
                    while buffered is None:
                        buffered = self.onset.feed(await asyncio.wait_for(stream.readexactly(640), 2))
                    first = buffered
                    self.onset = None
                await self.start_stt(key, input_id, language)
                if not self.input_current(key):
                    return
                self.capture_pending.extend(first)
                while len(self.capture_pending) >= chunk_bytes:
                    self.accept_pcm(input_id, bytes(self.capture_pending[:chunk_bytes]), source='dell')
                    del self.capture_pending[:chunk_bytes]
                deadline = loop.time() + self.s.stt_max_seconds
                while self.audio_size < limit:
                    size = min(chunk_bytes, limit - self.audio_size) - len(self.capture_pending)
                    if not size:
                        self.flush_capture(input_id)
                        if self.endpoint:
                            detected = self.endpoint.end_reason(self.audio_size / 32)
                            if detected:
                                reason = detected
                                break
                        continue
                    if loop.time() >= deadline:
                        break
                    try:
                        async with asyncio.timeout_at(min(deadline, loop.time() + 2)):
                            data = await stream.read(size)
                    except TimeoutError:
                        if loop.time() >= deadline:
                            break
                        raise
                    if not data:
                        raise EdgeError('DELL_MIC_UNAVAILABLE')
                    self.capture_pending.extend(data)
            if self.input_current(key) and not self.audio_end:
                await self.finish_capture(input_id, reason)
        except Exception as exc:
            if self.input_current(key):
                code = ('DELL_MIC_UNAVAILABLE' if isinstance(exc, (TimeoutError, EOFError, OSError))
                        else error_code(exc))
                await self.cancel(code)
        finally:
            self.capture_pending.clear()

    async def start_stt(self, key, input_id, language):
        self.phase = 'listening'
        # Reserve the maximum duration before an open-ended microphone RPC.
        # Actual transmitted duration is separately retained in diagnostics, never refunded.
        self.records.prepare_audio(key,SimpleNamespace(duration=self.s.stt_max_seconds),self.s,'cloud_speech_grpc',self.s.voice_stt_model)
        if not self.mock and not self.records.dispatch(key,self.s):
            self.phase='failed';await self.send({'kind':'error','code':self.records.info(key)['code']});return
        if self.mock:self.records.state(key,'simulated')
        if self.s.early_reaction_enabled:
            self.early=self.make_reactions(key,input_id)
            self.early.warm_final_connection()
        self.task=asyncio.create_task(self.audio_run(key,language))
        await self.send({'kind':'listening','input_id':input_id})
    async def finish_capture(self, input_id, reason):
        key = self.key
        await self.end_audio(input_id, reason=reason)
        if self.input_current(key):
            await self.send({'kind':'capture_ended','input_id':input_id,'reason':reason})

    async def audio_chunk(self, input_id, data):
        if self.mic_muted:return
        # Windows may have PCM in flight when the server seals an automatic input.
        if self.input_source == 'windows' and self.endpoint and self.audio_end and input_id == self.input_id:
            return
        if self.phase == 'armed':
            if self.input_source != 'windows' or input_id != self.input_id:
                raise EdgeError('MIC_SOURCE_MISMATCH')
            if not data or len(data) > 6400 or len(data) % 2:
                raise EdgeError('INPUT_AUDIO_LIMIT')
            data = self.onset.feed(data)
            if data is None:
                return
            self.onset = None
            await self.start_stt(self.key, input_id, self.input_language)
            if self.phase != 'listening':
                return
            while len(data) > 3200:
                self.accept_pcm(input_id, data[:3200], source='windows')
                data = data[3200:]
        limit = self.s.stt_max_seconds * 32000
        if self.endpoint and 0 < len(data) <= 6400 and len(data) % 2 == 0:
            data = data[:limit-self.audio_size]
        self.accept_pcm(input_id, data, source='windows')
        if self.endpoint:
            reason = self.endpoint.end_reason(self.audio_size / 32)
            if reason or self.audio_size == limit:
                await self.finish_capture(input_id, reason or 'duration_limit')

    def accept_pcm(self,input_id,data,*,source):
        if source!=self.input_source:raise EdgeError('MIC_SOURCE_MISMATCH')
        if input_id!=self.input_id or self.phase!='listening' or self.audio_end:raise EdgeError('STALE_MIC_AUDIO')
        if not data or len(data)%2 or len(data)>6400 or self.audio_size+len(data)>self.s.stt_max_seconds*32000:raise EdgeError('INPUT_AUDIO_LIMIT')
        samples=array('h');samples.frombytes(data)
        if __import__('sys').byteorder!='little':samples.byteswap()
        power=sum(x*x for x in samples)
        self.power+=power;self.audio_size+=len(data);self.hash.update(data)
        if self.endpoint:self.endpoint.observe_pcm(self.audio_size/32,power,len(samples))
        try:self.queue.put_nowait(data)
        except asyncio.QueueFull:raise EdgeError('INPUT_BACKPRESSURE_LIMIT') from None
        self.evaluate('pcm',data)
    async def end_audio(self,input_id,*,reason='client_end'):
        if self.mic_muted:return
        if (self.input_source=='dell' or self.endpoint) and input_id==self.input_id and self.audio_end:return
        if input_id!=self.input_id or self.phase!='listening' or self.audio_end:raise EdgeError('STALE_MIC_END')
        if self.input_source=='dell':self.flush_capture(input_id)
        self.audio_end=True;self.end_reason=reason
        self.evaluate('end',reason)
        rms=(self.power/max(1,self.audio_size//2))**.5/32768
        if self.audio_size<6400 or rms<self.s.stt_min_rms:
            if self.end_mode=='handsfree' and reason in ('duration_limit','client_end'):
                self.wait_for_quiet=True
            await self.cancel('NO_SPEECH');return
        try:self.queue.put_nowait(None)
        except asyncio.QueueFull:raise EdgeError('INPUT_BACKPRESSURE_LIMIT') from None
        self.phase='transcribing'
        await self.stop_capture()
    async def audio_run(self,key,language):
        begin=time.monotonic();input_id=self.input_id;early=self.early
        billing_usage = {}
        onset_settings = {'handsfree_onset_dbfs':self.s.handsfree_onset_dbfs} if self.end_mode=='handsfree' else {}
        queue=self.queue
        async def frames():
            while True:
                value=await queue.get()
                if value is None:return
                yield value
        async def emit(value):
            if self.input_current(key):
                if value.get('kind')=='stt_activity':
                    self.evaluate('event',{'kind':'stt_activity','type':value.get('type'),'offset_ms':value.get('offset_ms')})
                    if self.endpoint and not self.audio_end:self.endpoint.activity(value)
                    return
                if value.get('kind')=='stt_partial':
                    if self.endpoint and value.get('text'):self.endpoint.has_text=True
                    if early is not None:early.offer(value)
                await self.send({**value,'input_id':input_id,'stt_elapsed_ms':(time.monotonic()-begin)*1000})
        try:
            text,diagnostics=await self.clients.transcribe_frames(frames(),emit,model=self.s.voice_stt_model,
                timeout=self.s.stt_timeout,language=language,billing_usage=billing_usage)
            if (self.input_source=='dell' or self.endpoint) and not self.audio_end:raise EdgeError('STT_ENDED_BEFORE_INPUT')
            diagnostics.update(**billing_usage,**onset_settings,input_source=self.input_source,end_mode=self.end_mode,end_reason=self.end_reason,
                actual_audio_seconds=self.audio_size/32000,pcm_sha256=self.hash.hexdigest(),reserved_audio_seconds=self.s.stt_max_seconds)
            self.records.usage(key,{'local_diagnostics':diagnostics},(time.monotonic()-begin)*1000)
            self.evaluate('update',transcript=clean(text,1000),stt_complete_server_ms=time.monotonic()*1000,stt_pcm_sha256=self.hash.hexdigest())
            with self.store.connect() as db:db.execute('UPDATE developer_inputs SET payload_sha256=? WHERE id=?',(self.hash.hexdigest(),key))
            if early is not None:early.seal(clean(text,1000))
            if not self.input_current(key):return
            await self.respond(clean(text,1000),language)
            early=None  # respond owns final reaction cleanup once it accepts the transcript.
        except BaseException as exc:
            code=error_code(exc)
            if self.key==key:
                self.evaluate('end',self.end_reason or code)
                self.evaluate('update',outcome='empty' if code=='STT_NO_FINAL_TRANSCRIPT' else 'failed',error=code)
            if self.input_current(key):
                if self.end_mode=='handsfree' and (not self.audio_end or self.end_reason in ('duration_limit','client_end')):
                    self.wait_for_quiet=True  # The rejected input may still be audible.
                self.audio_end=True  # Discard browser PCM already in flight while cleanup runs.
                self.phase='failed'
                state='outcome_unknown' if 'CONNECTION' in code or 'DEADLINE' in code else 'failed'
                self.records.state(key,'empty' if code=='STT_NO_FINAL_TRANSCRIPT' else state,code)
            if early is not None:
                await early.close()
                early=None
            self.records.usage(key,{'local_diagnostics':{**billing_usage,**onset_settings,'input_source':self.input_source,'end_mode':self.end_mode,
                'end_reason':self.end_reason,'actual_audio_seconds':self.audio_size/32000,
                'reserved_audio_seconds':self.s.stt_max_seconds}},(time.monotonic()-begin)*1000)
            if self.key==key and self.phase=='failed' and not self.cancelled.is_set() and not self.closed:
                await self.stop_capture();await self.send({'kind':'reaction_reset','input_id':input_id})
                await self.send({'kind':'error','code':code,'input_id':input_id})
        finally:
            if early is not None and not self.input_current(key):await early.close()
    def select_language(self,language):
        if language not in CONVERSATION_LANGUAGES:raise EdgeError('INVALID_INPUT_LANGUAGE')
        self.conversation_language=language

    async def start_text(self,input_id,text,*,language=None):
        if language is not None and language not in CONVERSATION_LANGUAGES:
            raise EdgeError('INVALID_INPUT_LANGUAGE')
        text=clean(text,1000);key=self.prepare(input_id,'text',text.encode());self.phase='responding'
        if language is not None:self.conversation_language=language
        self.task=asyncio.create_task(self.respond(text,self.conversation_language))
    def current(self,token):
        return self.fence.current(token) and not self.cancelled.is_set() and not self.closed
    def source_current(self,turn):
        with self.store.connect() as db:
            row=db.execute('SELECT t.status,s.is_deleted,s.raw_purged_at FROM conversation_turns t JOIN source_events s ON s.id=t.source_event_id WHERE t.turn_id=?',(turn,)).fetchone()
        return bool(row and row[0] not in ('cancelled','source_removed','source_expired') and row[1]==0 and row[2] is None)
    async def respond(self,text,language):
        key=self.key;input_id=self.input_id;begin=time.monotonic();token=None;tasks=[];stop=asyncio.Event();queue=asyncio.Queue(maxsize=4);errors=[];reply=[];audio=bytearray()
        failure=None
        metrics={'profile':'fast_streaming','first_text_ms':None,'first_pcm_ms':None,'characters':0,'pcm_packets':0,'provider_requests':0}
        try:
            comment=Comment('ray-initiative' if self.initiative else 'developer-mock' if self.mock else 'developer',
                self.inbox.session,input_id,'ray' if self.initiative else self.s.developer_id,'Ray' if self.initiative else 'Developer',text,time.time())
            if self.inbox.put(comment)!='queued':raise EdgeError('INPUT_NOT_QUEUED')
            selected=self.inbox.take()
            if not selected or selected['receipt']!=comment.receipt:raise EdgeError('INPUT_QUEUE_MISMATCH')
            _,viewer=self.inbox.claimed(comment.receipt);token=self.fence.begin(uuid.uuid4().hex);self.token=token;turn=token['turn_id']
            if not self.inbox.bind(comment.receipt,turn):raise EdgeError('COMMENT_NO_LONGER_AVAILABLE')
            self.receipt=comment.receipt;self.viewer=viewer;self.phase='responding'
            if self.character:
                self.character_epoch=self.character.begin(key,text,initiative=self.initiative)
                # begin() incorporates the final transcript; preserve the face already shown while listening.
                if self.early and self.early.value:
                    self.character.react(self.character_epoch,self.early.value['reaction'])
            self.memory.wake()
            self.records.state(key,'accepted',language=language,source=comment.receipt)
            self.evaluate('update',turn_id=turn,transcript=text,outcome='responding')
            developer_speaker=self.store.start(turn,self.s.mode+':'+viewer,text,developer_id=self.s.developer_id)
            if self.early is None:self.early=self.make_reactions(key,input_id)
            self.early.bind(turn)
            await self.send({'kind':'turn','input_id':input_id,'token':token,'text':text})
            route = DialogueRoute()
            if self.character:
                route_history = await asyncio.to_thread(self.store.history,self.s.mode+':'+viewer,4,turn_id=turn)
                route = self.character.route(self.character_epoch,text,route_history,initiative=self.initiative)
            rag = route.allow_retrieval(metrics)
            semantic_enabled=rag and self.s.memory_enabled and self.s.semantic_memory_mode!='off'
            if semantic_enabled:
                # Extraction remains asynchronous; this path never blocks speech on a remote job.
                self.memory.wake()
                self.early.semantic_viewer=viewer
                lookup=time.monotonic()
                candidates=await asyncio.to_thread(self.store.episode_candidates,viewer,text,scope=self.s.mode,
                    limit=self.s.semantic_candidate_limit,max_chars=self.s.semantic_max_chars)
                metrics['semantic_lookup_ms']=round((time.monotonic()-lookup)*1000,3)
                metrics['semantic_candidates']=len(candidates)
                if self.s.semantic_memory_mode=='jev':self.early.candidates=candidates
                self.early.previous_message=self.store.semantic_previous_message(viewer,scope=self.s.mode,turn_id=turn) if candidates else ''
            self.early.finalize(text)
            if rag and not semantic_enabled:
                ready=await asyncio.to_thread(self.memory.wait_for,viewer,cancel=self.cancelled)
                if not ready:raise EdgeError('MEMORY_WORKER_NOT_READY')
            decision_deadline=time.monotonic()+self.s.semantic_wait_ms/1000
            elapsed=lambda:(time.monotonic()-begin)*1000
            async def emit(value):
                if self.current(token):await self.send({**value,'token':token,'input_id':input_id,'server_elapsed_ms':elapsed()})
            def prepare(provider,operation,model):
                call=self.store.prepare_call(turn,provider,operation,{'profile':'fast_streaming'},model=model,mode=self.s.mode)
                if not self.current(token) or not self.source_current(turn):
                    self.store.call_state(call,'cancelled_before_send');raise EdgeError('TURN_CANCELLED')
                if provider=='gemini' and not self.mock and self.s.monthly_reply_llm_usd:return call
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
                usage={}
                generation_started=False
                try:
                    # Bound the final JEV wait independently of provider setup.
                    wait_started=time.monotonic()
                    if self.early.candidates:
                        try:await asyncio.wait_for(self.early.final_done.wait(),max(0,decision_deadline-wait_started))
                        except TimeoutError:pass
                        metrics['semantic_wait_ms']=round((time.monotonic()-wait_started)*1000,3)
                    arrived=self.early.semantic_ready_at
                    semantic_decision=self.early.semantic if arrived is not None and arrived<=decision_deadline else None
                    call=prepare('gemini','stream_generate',self.s.gemini_model)
                    notes,strategy=self.store.conversation_memories(viewer,text,self.s,judgment=semantic_decision) if rag else ([], 'dialogue_gate')
                    if rag and developer_speaker:notes+=self.store.developer_banter_memory(viewer,self.s)
                    metrics['memory_strategy']=strategy
                    if semantic_enabled:
                        self.store.record_semantic_retrieval(turn,text,candidates,semantic_decision,used=semantic_decision is not None)
                    self.store.record_retrievals(turn,notes,context_prepared=True,strategy=strategy,call_id=call)
                    metrics['memory_ids']=[x['memory_id'] for x in notes]
                    def read_context():
                        if not (rag and self.s.memory_enabled and self.s.context_enabled):return []
                        return self.store.recall_context(viewer,text,scope=self.s.mode,developer_id=self.s.developer_id,
                            limit=self.s.context_max_items,max_chars=self.s.context_max_chars,turn_id=turn)
                    history,context=load_speech_background(self.s,text,bool(self.early.candidates),semantic_decision,
                        lambda:self.store.history(self.s.mode+':'+viewer,self.s.memory_turns,turn_id=turn),read_context,selected=notes)
                    self.store.record_context_retrievals(turn,context,call_id=call,prepared=True)
                    metrics['context_tiers']=[e['tier'] for e in context];metrics['context_items']=len(context)
                    lookup=time.monotonic()
                    background=self.store.relationship_context(viewer,scope=self.s.mode) if self.s.memory_enabled and self.s.relationship_enabled else None
                    metrics['relationship_lookup_ms']=round((time.monotonic()-lookup)*1000,3)
                    reaction=(self.early.value or {}).get('reaction') if semantic_decision else None
                    prompt=self.prompts[language]+listener_instruction(strategy,reaction)
                    if developer_speaker:prompt+=DEVELOPER_SPEAKER
                    character_begin=time.monotonic()
                    character_memories=self.memory.character_memories(viewer) if self.character and rag else ()
                    hook=await asyncio.to_thread(claim_hook,self.store,self.s.mode,viewer,turn,text,
                                                 route.unspoken_hooks) if route.dialogue_policy=='SHIFT_TO_HOOK' else ''
                    plan=self.character.plan(text,character_memories,background,initiative=self.initiative,epoch=self.character_epoch,
                                             host_speaking=developer_speaker,hook=hook) if self.character else None
                    structured=bool(plan and getattr(self.clients.generate,'ray_public_output',False))
                    decoder=PublicDecoder(structured=structured,buffer=SentenceBuffer(self.s.sentence_chars),plan=plan,limit=self.s.max_reply_chars) if plan else None
                    if plan:
                        prompt=public_prompt(prompt,plan) if structured else prompt+plan.instruction()
                        self.memory.character_referenced(character_memories)
                    if language=='ja-JP' and not self.initiative:
                        prompt+=japanese_question_direction(self.recent_questions, text)
                    metrics['character_prepare_ms']=round((time.monotonic()-character_begin)*1000,3)
                    metrics['llm_generate_start_ms']=round(elapsed(),3)
                    timing_args={}
                    if isinstance(self.clients,EdgeClients):
                        metrics['gemini_stream']={}
                        timing_args['timings']=metrics['gemini_stream']
                        timing_args['usage']=usage
                    if not self.mock and self.s.monthly_reply_llm_usd:
                        if not isinstance(self.clients,EdgeClients):raise EdgeError('REPLY_LLM_BUDGET_MODEL_NOT_PRICED')
                        def before_send(size,output,attempts):
                            if not self.current(token) or not self.source_current(turn):raise EdgeError('TURN_CANCELLED')
                            self.store.dispatch_budgeted_reply(call,size,output,attempts,self.s.monthly_reply_llm_usd)
                            metrics['provider_requests']+=1
                        timing_args['before_send']=before_send
                    generation_started=True
                    async for delta in self.clients.generate(text,**timing_args,developer_speaker=developer_speaker,model=self.s.gemini_model,prompt=prompt,history=history,memories=notes,context=context,relationship=background,language=language):
                        if not self.current(token) or not self.source_current(turn):raise EdgeError('TURN_CANCELLED')
                        if metrics['first_text_ms'] is None:metrics['first_text_ms']=elapsed()
                        if decoder:
                            outputs=decoder.feed(delta)
                            for output in outputs:
                                metrics['characters']+=len(output.speech)
                                if metrics['characters']>self.s.max_reply_chars:raise EdgeError('REPLY_CHARACTER_LIMIT')
                                metrics.setdefault('public_output_ready_ms',elapsed())
                                reply.append((' ' if reply else '')+output.speech)
                                await emit({'kind':'character_action',**output.action()})
                                await emit({'kind':'text','text':output.speech});await put(output)
                        else:
                            metrics['characters']+=len(delta)
                            if metrics['characters']>self.s.max_reply_chars:raise EdgeError('REPLY_CHARACTER_LIMIT')
                            reply.append(delta);await emit({'kind':'text','text':delta});await put(delta)
                    if decoder:
                        for output in decoder.feed('',final=True):
                            metrics['characters']+=len(output.speech)
                            if metrics['characters']>self.s.max_reply_chars:raise EdgeError('REPLY_CHARACTER_LIMIT')
                            metrics.setdefault('public_output_ready_ms',elapsed())
                            reply.append((' ' if reply else '')+output.speech)
                            await emit({'kind':'character_action',**output.action()})
                            await emit({'kind':'text','text':output.speech});await put(output)
                        metrics['character_state_updates']=self.character.metrics()['state_updates']
                    self.store.call_state(call,'accepted')
                except BaseException as exc:
                    if isinstance(exc,CharacterError):
                        self.character.format_error()
                        metrics['character_output_format_errors']=1
                        exc=EdgeError(str(exc))
                    if call:failed(call,exc)
                    errors.append(error_code(exc))
                finally:
                    if call and generation_started:
                        self.store.call_usage(call,usage if isinstance(self.clients,EdgeClients) else getattr(self.clients,'llm_usage',{}))
                    await put(None)
            reservation=self.store.reserve(self.s.max_reply_chars,self.s.monthly_tts_chars) if not self.mock else None
            tasks=[asyncio.create_task(generate())]
            call=None
            first_delta=None
            async def texts():
                if first_delta is not None:
                    yield first_delta.speech if isinstance(first_delta,PublicOutput) else first_delta
                while True:
                    value=await queue.get()
                    if value is None:
                        if errors:raise EdgeError(errors[0])
                        return
                    if self.character and not isinstance(value,PublicOutput):raise EdgeError('PUBLIC_OUTPUT_TYPE_REQUIRED')
                    yield value.speech if isinstance(value,PublicOutput) else value
            try:
                async with asyncio.timeout(self.s.turn_timeout):
                    if self.character or self.early.candidates or (not self.mock and self.s.monthly_reply_llm_usd):
                        # An unadmitted generation cannot dispatch TTS. The wire adapter also waits for text.
                        first_delta=await queue.get()
                        if first_delta is None:raise EdgeError(errors[0] if errors else 'EMPTY_GENERATED_REPLY')
                    call=prepare('tts','streaming_synthesize',streaming_voice(self.s.voice_tts_voice,language))
                    tts_options={'language':language} if isinstance(self.clients,EdgeClients) else {}
                    async with aclosing(self.clients.synthesize_stream(texts(),**tts_options)) as packets:
                        async for pcm in packets:
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
            except BaseException as exc:
                if call:failed(call,exc)
                raise
            if not self.source_current(turn):raise EdgeError('COMMENT_NO_LONGER_AVAILABLE')
            path=self.output/(turn+'-0.wav');path.write_bytes(pcm_wave(audio,24000));self.expected_frames=len(audio)//2
            self.store.record_utterance(turn,0,''.join(reply),path,self.expected_frames/24000)
            # The worklet can drain a short reply while its phonemes are still being estimated.
            await emit({'kind': 'audio_end', 'frames': self.expected_frames})
            if self.phone_aligner is not None and language=='en-US':
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
            if self.character:
                metrics['character']=self.character.metrics()
                with self.memory.lock:metrics['character_memory']=dict(self.memory.character_counts)
            spoken_reply=''.join(reply)
            self.generated_asked=asked_listener_in_japanese(spoken_reply) if language=='ja-JP' else None
            self.store.finish(turn,spoken_reply,'completed',metrics);self.inbox.finish(self.receipt,'generated');self.phase='generated'
            self.evaluate('update',reply=spoken_reply,backend=metrics,outcome='generated')
            await emit({'kind':'complete','metrics':metrics})
        except BaseException as exc:
            code=error_code(exc)
            if self.key==key:self.evaluate('update',outcome='failed',error=code)
            if token:
                self.store.finish(token['turn_id'],''.join(reply),'cancelled' if self.cancelled.is_set() else 'failed',metrics)
                if self.receipt:self.inbox.finish(self.receipt,'cancelled' if self.cancelled.is_set() else 'failed',code)
            if not self.cancelled.is_set():
                self.phase='failed'
                failure={'kind':'error','code':code,'input_id':input_id,'token':token}
                if metrics.get('character_output_format_errors') and code.startswith('PUBLIC_'):
                    failure['reply_rejected']=True
        finally:
            stop.set()
            for task in tasks:
                if not task.done():task.cancel()
            if tasks:await asyncio.gather(*tasks,return_exceptions=True)
            if self.early is not None and not self.input_current(key):await self.early.close()
        if failure and self.key==key and not self.cancelled.is_set():
            await self.send(failure)
    def played(self,token,frames):
        if self.phase!='generated' or not self.current(token) or type(frames) is not int or frames!=self.expected_frames or not self.source_current(token['turn_id']):return False
        self.store.chunk_claimed(token['turn_id'],0)
        accepted=self.store.played(token['turn_id'],scope=self.s.mode,memory_enabled=self.s.memory_enabled,memory_ttl_days=self.s.memory_ttl_days,
            memory_capacity=self.s.memory_queue_size,memory_policy=self.memory.policy,memory_model=self.memory.model,memory_min_confidence=self.s.memory_min_confidence,
            relationship_enabled=self.s.memory_enabled and self.s.relationship_enabled,developer_id=self.s.developer_id,
            unspoken_hooks=self.character.hooks_for(self.key) if self.character else None)
        if accepted:
            if not self.initiative:
                if self.generated_asked is None:self.recent_questions.clear()
                else:self.recent_questions.append(self.generated_asked)
            self.inbox.finish(self.receipt,'played');self.phase='played'
            self.evaluate('update',outcome='played',played_server_ms=time.monotonic()*1000)
            if self.early is not None:self.early.stop()
            if self.s.memory_enabled:self.memory.wake()
            if self.character:
                self.character.played(initiative=self.initiative)
                if not self.initiative:self.memory.offer_character(token['turn_id'])
        return accepted
    async def cancel(self,reason='DEVELOPER_CANCELLED'):
        previous=self.phase;self.cancelled.set();token=self.token;input_id=self.input_id
        await self.stop_capture()
        self.capture_pending.clear()
        self.onset=None
        if self.early is not None:await self.early.close()
        self.fence.cancel();self.phase='cancelled'
        if previous!='played':
            if self.key and previous!='failed':self.records.state(self.key,'cancelled',reason)
            if token:self.store.cancel_turn(token['turn_id'])
            if self.receipt:self.inbox.finish(self.receipt,'cancelled',reason)
        if self.task and not self.task.done():self.task.cancel();await asyncio.gather(self.task,return_exceptions=True)
        self.evaluate('end',self.end_reason or reason)
        if previous not in ('played','failed'):
            self.evaluate('update',outcome='interrupted' if reason=='SESSION_CLOSED' else 'cancelled',error=reason)
        self.key=None;self.receipt=None;self.token=None
        if self.character:self.character.cancel()
        await self.send({'kind':'reaction_reset','input_id':input_id})
        await self.send({'kind':'cancelled','code':reason,'input_id':input_id})
    async def close(self):
        if self.closed:return
        self.closed=True;await self.cancel('SESSION_CLOSED');self.inbox.close();await asyncio.to_thread(self.memory.close)
        if self.character:self.character.dispose()

    async def maybe_initiate(self):
        if (not self.character or not self.s.character_initiative_enabled or self.closed
                or self.phase not in ('idle','played') or self.task and not self.task.done()):return False
        memories=self.memory.character_memories(self.viewer) if self.viewer else ()
        topic=self.character.idle_topic(memories)
        if topic is None:return False
        input_id=uuid.uuid4().hex
        text=initiative_message(topic)
        self.prepare(input_id,'text',text.encode())
        self.initiative=True;self.phase='responding'
        await self.send({'kind':'initiative','input_id':input_id,'topic':topic})
        self.task=asyncio.create_task(self.respond(text,self.conversation_language))
        return True
