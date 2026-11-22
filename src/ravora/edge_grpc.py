"""Finite direct GCP clients. Reused TLS gRPC channels; retries and global fallback disabled."""
import asyncio
from contextlib import aclosing
import io
import json
import re
import time
import wave
from ravora.context_prompt import CONTEXT_RULES,context_text
from ravora.relationships import RELATIONSHIP_RULES,relationship_text
from ravora.persona import conversation_prompt, name_question_instruction, reply_style_context
from ravora.ray_character import PUBLIC_OUTPUT_SCHEMA, PUBLIC_PROTOCOL
from ravora.settings import DEFAULT_STREAMING_VOICE, default_wavenet_voice, streaming_voice
from ravora.stream_pitch import load_live_shifter, pitch_shift_pcm

PROJECT='example-project'
PROFILES=('wavenet_sentence','chirp_sentence','chirp_stream')


class EdgeError(RuntimeError):pass


def pcm_wave(data,rate):
    out=io.BytesIO()
    with wave.open(out,'wb') as audio:
        audio.setnchannels(1);audio.setsampwidth(2);audio.setframerate(rate);audio.writeframes(data)
    return out.getvalue()


def wave_pcm(data,rate=24000):
    with wave.open(io.BytesIO(data)) as audio:
        if (audio.getnchannels(),audio.getsampwidth(),audio.getframerate())!=(1,2,rate):raise EdgeError('INVALID_PCM_WAV')
        count=audio.getnframes();pcm=audio.readframes(count)
        if len(pcm)!=count*2:raise EdgeError('TRUNCATED_PCM_WAV')
        return pcm


# Normal first responses arrive in 0.8-1.1 s; a request still silent after 1.3 s is likely a slow one.
HEDGE_AFTER=1.3
HEDGES=2


class EdgeClients:
    def __init__(self,credential,*,stt_region='asia-northeast1',tts_region='asia-northeast1',project=PROJECT,
                 language='en-US',voice=DEFAULT_STREAMING_VOICE,speaking_rate=.96,stream_pitch=0.):
        if stt_region not in ('asia-northeast1','asia-southeast1','us','eu') or tts_region!='asia-northeast1':raise EdgeError('UNAPPROVED_API_REGION')
        if not re.fullmatch(r'[a-z][a-z0-9-]{4,28}[a-z0-9]',project) or language not in ('en-US','ja-JP'):raise EdgeError('INVALID_PROJECT_OR_LANGUAGE')
        self.credential=credential;self.stt_region=stt_region;self.tts_region=tts_region;self.channels=[]
        self.project=project;self.language=language;self.voice=voice;self.speaking_rate=speaking_rate
        self.stream_pitch=stream_pitch
        self.endpoints={'stt':stt_region+'-speech.googleapis.com','llm':'generativelanguage.googleapis.com',
            'tts':tts_region+'-texttospeech.googleapis.com'}
    async def open(self):
        if self.stream_pitch:
            load_live_shifter()
        import grpc
        from google.auth.credentials import AnonymousCredentials
        from google.cloud import speech_v2 as speech,texttospeech_v1 as tts
        from google.ai import generativelanguage_v1beta as gen
        from google.cloud.speech_v2.services.speech.transports import SpeechGrpcAsyncIOTransport
        from google.cloud.texttospeech_v1.services.text_to_speech.transports import TextToSpeechGrpcAsyncIOTransport
        from google.ai.generativelanguage_v1beta.services.generative_service.transports import GenerativeServiceGrpcAsyncIOTransport
        self.speech_types=speech.types;self.tts_types=tts;self.gen_types=gen
        options=(('grpc.enable_retries',0),('grpc.keepalive_time_ms',60000),('grpc.keepalive_timeout_ms',10000),('grpc.keepalive_permit_without_calls',0),('grpc.max_receive_message_length',16*1024*1024))
        for name in ('stt','llm','tts'):
            channel=grpc.aio.secure_channel(self.endpoints[name],grpc.ssl_channel_credentials(),options=options)
            self.channels.append(channel)
        self.stt=speech.SpeechAsyncClient(transport=SpeechGrpcAsyncIOTransport(channel=self.channels[0],credentials=AnonymousCredentials()))
        self.llm=gen.GenerativeServiceAsyncClient(transport=GenerativeServiceGrpcAsyncIOTransport(channel=self.channels[1],credentials=AnonymousCredentials()))
        self.tts=tts.TextToSpeechAsyncClient(transport=TextToSpeechGrpcAsyncIOTransport(channel=self.channels[2],credentials=AnonymousCredentials()))
        await asyncio.wait_for(asyncio.gather(*(channel.channel_ready() for channel in self.channels)),12)
        return self
    async def close(self):
        for channel in self.channels:await channel.close(grace=0)
    async def transcribe(self,pcm,emit,*,model='chirp_3',pace=True):
        begin=time.monotonic()
        async def frames():
            for index in range(0,len(pcm),3200):
                if pace:await asyncio.sleep(max(0,begin+index/32000-time.monotonic()))
                yield pcm[index:index+3200]
            if pace:await asyncio.sleep(max(0,begin+len(pcm)/32000-time.monotonic()))
        return await self.transcribe_frames(frames(),emit,model=model,timeout=20)

    async def transcribe_frames(self,frames,emit,*,model='chirp_2',timeout=45,language=None,billing_usage=None):
        s=self.speech_types;begin=time.monotonic();final=[];events=[];sent=0
        language=language or self.language
        if language not in ('en-US','ja-JP'):raise EdgeError('INVALID_INPUT_LANGUAGE')
        config=s.StreamingRecognitionConfig(config=s.RecognitionConfig(
            explicit_decoding_config=s.ExplicitDecodingConfig(encoding=s.ExplicitDecodingConfig.AudioEncoding.LINEAR16,
                sample_rate_hertz=16000,audio_channel_count=1),model=model,language_codes=[language],
            features=s.RecognitionFeatures(enable_automatic_punctuation=True)),
            streaming_features=s.StreamingRecognitionFeatures(interim_results=True,enable_voice_activity_events=True))
        async def requests():
            nonlocal sent
            yield s.StreamingRecognizeRequest(recognizer=f'projects/{self.project}/locations/{self.stt_region}/recognizers/_',streaming_config=config)
            async for data in frames:
                sent+=len(data)
                if not data or len(data)%2 or len(data)>25000 or sent>30*32000:raise EdgeError('INVALID_STREAMING_PCM')
                yield s.StreamingRecognizeRequest(audio=data)
            await emit({'kind':'input_complete','input_seconds':sent/32000,'input_send_ms':(time.monotonic()-begin)*1000})
        stream=await self.stt.streaming_recognize(requests=requests(),retry=None,timeout=timeout,metadata=(
            ('authorization','Bearer '+self.credential['access_token']),('x-goog-user-project',self.project)))
        async for response in stream:
            # Google reports a per-request total, including on some streams without text.
            if billing_usage is not None and 'total_billed_duration' in response.metadata:
                billing_usage['provider_billed_seconds'] = response.metadata.total_billed_duration.total_seconds()
            if response.speech_event_type:
                event={'type':response.speech_event_type.name,'offset_ms':response.speech_event_offset.total_seconds()*1000}
                events.append(event)
                if event['type'] in ('SPEECH_ACTIVITY_BEGIN','SPEECH_ACTIVITY_END'):
                    await emit({'kind':'stt_activity',**event})
            partial=[];stabilities=[];segment_final=False
            for result in response.results:
                if not result.alternatives:continue
                value=result.alternatives[0].transcript.strip()
                if not value:continue
                if result.is_final:final.append(value);segment_final=True
                else:
                    partial.append(value)
                    # 0.0 is the API's sentinel for an unspecified stability.
                    stability=float(getattr(result,'stability',0))
                    stabilities.append(stability if stability>0 else None)
            snapshot=' '.join([*final,*partial]).strip()
            if len(snapshot)>1000:
                stream.cancel()
                raise EdgeError('STT_TRANSCRIPT_TOO_LONG')
            if snapshot and (partial or segment_final):
                await emit({'kind':'stt_partial','text':snapshot,
                    'stability':min(stabilities) if stabilities and all(x is not None for x in stabilities) else None,
                    'segment_final':segment_final and not partial})
        text=' '.join(final).strip()
        if not text:raise EdgeError('STT_NO_FINAL_TRANSCRIPT')
        return text,{'events':events,'final_segments':len(final),'model':model,'endpoint':self.endpoints['stt']}
    async def generate(self,text,*,model='gemini-3.5-flash-lite',prompt=None,history=(),memories=(),context=(),relationship=None,timings=None,before_send=None,developer_speaker=False,language=None,usage=None):
        if usage is None:usage={}
        self.llm_usage=usage
        build_start=time.monotonic()
        g=self.gen_types
        style,reports=reply_style_context(text,history,memories,developer_speaker=developer_speaker,language=language or self.language)
        prompt=(prompt or conversation_prompt(language=language or self.language)) + name_question_instruction(text,history)+style
        if context:prompt+=CONTEXT_RULES
        if relationship:prompt+=RELATIONSHIP_RULES
        contents=[g.Content(role=role,parts=[g.Part(text=value)]) for role,value in history]
        parts=[]
        if reports:parts.append(g.Part(text='Retrieved viewer reports (JSON):\n'+json.dumps(reports[:10],ensure_ascii=False)))
        if context:parts.append(g.Part(text=context_text(context)))
        if relationship:parts.append(g.Part(text=relationship_text(relationship)))
        parts.append(g.Part(text=text));contents.append(g.Content(role='user',parts=parts))
        config={'max_output_tokens':400,'temperature':.7}
        structured=PUBLIC_PROTOCOL in prompt
        if structured:
            config.update(response_mime_type='application/json',response_json_schema=PUBLIC_OUTPUT_SCHEMA)
        request=g.GenerateContentRequest(model='models/'+model,system_instruction=g.Content(parts=[g.Part(text=prompt)]),
            contents=contents,generation_config=g.GenerationConfig(**config))
        size=0;finish=None
        if before_send is not None:
            # Reserve the full text payload and every possible hedge before the first RPC.
            size_bytes=len(prompt.encode())+sum(len(part.text.encode())+32 for item in contents for part in item.parts)
            if structured:size_bytes+=len(json.dumps(PUBLIC_OUTPUT_SCHEMA).encode())
            before_send(size_bytes,request.generation_config.max_output_tokens,1+HEDGES)
        rpc_start=time.monotonic()
        def elapsed_ms():
            return round((time.monotonic()-rpc_start)*1000,3)
        heartbeat=None
        if timings is not None:
            timings['request_build_ms']=round((rpc_start-build_start)*1000,3)
            timings['channel_state']=self.channels[1].get_state().name if self.channels else 'unobserved'
            timings['max_loop_lag_ms']=0.
            loop=asyncio.get_running_loop()
            def watch_loop(expected):
                nonlocal heartbeat
                now=loop.time()
                timings['max_loop_lag_ms']=max(timings['max_loop_lag_ms'],round((now-expected)*1000,3))
                heartbeat=loop.call_at(now+.1,watch_loop,now+.1)
            scheduled=loop.time()+.1
            heartbeat=loop.call_at(scheduled,watch_loop,scheduled)
        # Hedged requests: gemini-3.5-flash-lite answers these short turns in ~0.8-1.1 s, but about one
        # call in three takes 5-12 s on the server (gateway-reported) whatever the history or context
        # (measured 2026-11-19). If no response has arrived after HEDGE_AFTER, the same request is sent
        # again (at most twice) and the first stream to respond is used; the others are cancelled.
        streams=[]
        async def open_stream(index):
            stream=await self.llm.stream_generate_content(request=request,retry=None,timeout=25,metadata=(('x-goog-api-key',self.credential['gemini_key']),))
            streams.append(stream)
            gateway=None;opened_ms=elapsed_ms();headers_ms=None
            if hasattr(stream,'initial_metadata'):
                # If present, retain only the numeric gateway duration. Metadata can contain identifiers.
                for name,value in await stream.initial_metadata():
                    if name=='server-timing' and isinstance(value,str):
                        duration=re.search(r'(?:^|,)\s*gfet4t7;\s*dur=(\d{1,6}(?:\.\d{1,6})?)\b',value)
                        if duration:gateway=float(duration[1])
                headers_ms=elapsed_ms()
            responses=stream.__aiter__()
            try:first=await responses.__anext__()
            except StopAsyncIteration:first=None
            return index,stream,responses,first,gateway,opened_ms,headers_ms
        attempts=[asyncio.create_task(open_stream(0))];opened=1;winner=None;failure=None
        try:
            while winner is None:
                wait=HEDGE_AFTER*opened-(time.monotonic()-rpc_start) if opened<=HEDGES else None
                done,_=await asyncio.wait(attempts,timeout=None if wait is None else max(0,wait),return_when=asyncio.FIRST_COMPLETED)
                if not done:
                    attempts.append(asyncio.create_task(open_stream(opened)));opened+=1;continue
                for task in done:
                    attempts.remove(task)
                    if task.exception() is None and winner is None:winner=task.result()
                    elif failure is None:failure=task.exception()
                if winner is None and not attempts:raise failure
        finally:
            for task in attempts:task.cancel()
            for other in streams:
                if winner is None or other is not winner[1]:other.cancel()
            if winner is None:
                if timings is not None:timings['rpc_exit_ms']=elapsed_ms()
                if heartbeat is not None:heartbeat.cancel()
        index,stream,responses,first,gateway,opened_ms,headers_ms=winner
        if timings is not None:
            timings['hedged_requests']=opened-1;timings['hedge_winner']=index
            timings['rpc_open_ms']=opened_ms
            if headers_ms is not None:timings['headers_ms']=headers_ms
            if gateway is not None:timings['gateway_reported_ms']=gateway
        async def chained():
            if first is not None:yield first
            async for response in responses:yield response
        try:
            async for response in chained():
                if timings is not None and 'first_response_ms' not in timings:
                    timings['first_response_ms']=elapsed_ms()
                if response.usage_metadata:
                    u=response.usage_metadata
                    # Streamed token counts are cumulative. An output-only chunk can omit earlier counters.
                    if u.prompt_token_count:
                        usage['input_tokens']=max(usage.get('input_tokens',0),u.prompt_token_count)
                    # Proto3 scalar defaults to zero when the measured prompt had no cache hits.
                    if u.prompt_token_count or u.cached_content_token_count:
                        usage['cached_input_tokens']=max(usage.get('cached_input_tokens',0),u.cached_content_token_count)
                    for key,value in (('output_tokens',u.candidates_token_count),('total_tokens',u.total_token_count),
                                      ('thought_tokens',u.thoughts_token_count)):
                        usage[key]=max(usage.get(key,0),value)
                    if response.model_version:usage['response_model']=response.model_version
                for candidate in response.candidates:
                    if candidate.index:continue
                    for part in candidate.content.parts:
                        if part.text and not part.thought:
                            size+=len(part.text)
                            if size>1000+(2048 if structured else 0):raise EdgeError('REPLY_CHARACTER_LIMIT')
                            if timings is not None and 'first_text_ms' not in timings:
                                timings['first_text_ms']=elapsed_ms()
                                heartbeat.cancel()
                            yield part.text
                    if candidate.finish_reason:finish=candidate.finish_reason.name
        finally:
            if timings is not None:
                timings['rpc_exit_ms']=elapsed_ms()
            if heartbeat is not None:
                heartbeat.cancel()
        usage['finish_reason']=finish
        if finish!='STOP':raise EdgeError('INCOMPLETE_GENERATION')
    async def synthesize(self,text,*,chirp=False,language=None):
        language=language or self.language
        t=self.tts_types;voice=streaming_voice(self.voice,language) if chirp else default_wavenet_voice(language)
        config={'audio_encoding':t.AudioEncoding.LINEAR16,'sample_rate_hertz':24000,'speaking_rate':.96}
        if not chirp:config['pitch']=2
        result=await self.tts.synthesize_speech(request=t.SynthesizeSpeechRequest(input=t.SynthesisInput(text=text),
            voice=t.VoiceSelectionParams(language_code=language,name=voice),audio_config=t.AudioConfig(**config)),
            retry=None,timeout=20,metadata=(('x-goog-api-key',self.credential['tts_key']),))
        return wave_pcm(result.audio_content)
    async def synthesize_stream(self,texts,*,language=None):
        t=self.tts_types
        language=language or self.language
        voice=streaming_voice(self.voice,language)
        # Open the TTS stream with the first reply text, not before it: the service aborts an idle
        # stream (gRPC ABORTED, seen as PROVIDER_HTTP_409) after ~6 s without input, and the LLM's
        # first text took 5.5 s and more in later turns. Synthesis cannot start earlier anyway.
        texts=texts.__aiter__()
        try:first=await texts.__anext__()
        except StopAsyncIteration:return
        async def requests():
            yield t.StreamingSynthesizeRequest(streaming_config=t.StreamingSynthesizeConfig(
                voice=t.VoiceSelectionParams(language_code=language,name=voice),
                streaming_audio_config=t.StreamingAudioConfig(audio_encoding=t.AudioEncoding.PCM,sample_rate_hertz=24000,speaking_rate=self.speaking_rate)))
            yield t.StreamingSynthesizeRequest(input=t.StreamingSynthesisInput(text=first))
            async for text in texts:yield t.StreamingSynthesizeRequest(input=t.StreamingSynthesisInput(text=text))
        stream=await self.tts.streaming_synthesize(requests=requests(),retry=None,timeout=30,metadata=(('x-goog-api-key',self.credential['tts_key']),))
        async def packets():
            async for response in stream:
                data=bytes(response.audio_content)
                if data:
                    if len(data)%2:raise EdgeError('ODD_PCM_BYTES')
                    yield data
        try:
            async with aclosing(pitch_shift_pcm(packets(), self.stream_pitch)) as converted:
                async for data in converted:
                    yield data
        finally:
            stream.cancel()

# Capability is on the method, so an overridden test/custom method must opt in.
EdgeClients.generate.ray_public_output = True
