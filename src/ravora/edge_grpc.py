"""Finite direct GCP clients. Reused TLS gRPC channels; retries and global fallback disabled."""
import asyncio
import io
import json
import re
import time
import wave
from ravora.context_prompt import CONTEXT_RULES,context_text
from ravora.relationships import RELATIONSHIP_RULES,relationship_text
from ravora.persona import conversation_prompt

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


class EdgeClients:
    def __init__(self,credential,*,stt_region='asia-northeast1',tts_region='asia-northeast1',project=PROJECT,
                 language='en-US',voice='en-US-Chirp3-HD-Aoede',speaking_rate=.96):
        if stt_region not in ('asia-northeast1','asia-southeast1','us','eu') or tts_region!='asia-northeast1':raise EdgeError('UNAPPROVED_API_REGION')
        if not re.fullmatch(r'[a-z][a-z0-9-]{4,28}[a-z0-9]',project) or language not in ('en-US','ja-JP'):raise EdgeError('INVALID_PROJECT_OR_LANGUAGE')
        self.credential=credential;self.stt_region=stt_region;self.tts_region=tts_region;self.channels=[]
        self.project=project;self.language=language;self.voice=voice;self.speaking_rate=speaking_rate
        self.endpoints={'stt':stt_region+'-speech.googleapis.com','llm':'generativelanguage.googleapis.com',
            'tts':tts_region+'-texttospeech.googleapis.com'}
    async def open(self):
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

    async def transcribe_frames(self,frames,emit,*,model='chirp_2',timeout=45,language=None):
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
            if response.speech_event_type:
                events.append({'type':response.speech_event_type.name,'offset_ms':response.speech_event_offset.total_seconds()*1000})
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
            if len(snapshot)>1000:raise EdgeError('EMPTY_OR_OVERSIZED_STT')
            if snapshot and (partial or segment_final):
                await emit({'kind':'stt_partial','text':snapshot,
                    'stability':min(stabilities) if stabilities and all(x is not None for x in stabilities) else None,
                    'segment_final':segment_final and not partial})
        text=' '.join(final).strip()
        if not text or len(text)>1000:raise EdgeError('EMPTY_OR_OVERSIZED_STT')
        return text,{'events':events,'final_segments':len(final),'model':model,'endpoint':self.endpoints['stt']}
    async def generate(self,text,*,model='gemini-3.5-flash-lite',prompt=None,history=(),memories=(),context=(),relationship=None):
        g=self.gen_types
        prompt=prompt or conversation_prompt()
        if context:prompt+=CONTEXT_RULES
        if relationship:prompt+=RELATIONSHIP_RULES
        contents=[g.Content(role=role,parts=[g.Part(text=value)]) for role,value in history]
        parts=[]
        if memories:parts.append(g.Part(text='Retrieved viewer reports (JSON):\n'+json.dumps(list(memories)[:10],ensure_ascii=False)))
        if context:parts.append(g.Part(text=context_text(context)))
        if relationship:parts.append(g.Part(text=relationship_text(relationship)))
        parts.append(g.Part(text=text));contents.append(g.Content(role='user',parts=parts))
        request=g.GenerateContentRequest(model='models/'+model,system_instruction=g.Content(parts=[g.Part(text=prompt)]),
            contents=contents,generation_config=g.GenerationConfig(max_output_tokens=400,temperature=.7))
        self.llm_usage={};size=0;finish=None
        stream=await self.llm.stream_generate_content(request=request,retry=None,timeout=25,metadata=(('x-goog-api-key',self.credential['gemini_key']),))
        async for response in stream:
            if response.usage_metadata:
                u=response.usage_metadata
                self.llm_usage.update(input_tokens=u.prompt_token_count,output_tokens=u.candidates_token_count,total_tokens=u.total_token_count,
                    thought_tokens=u.thoughts_token_count,response_model=response.model_version)
            for candidate in response.candidates:
                if candidate.index:continue
                for part in candidate.content.parts:
                    if part.text and not part.thought:
                        size+=len(part.text)
                        if size>1000:raise EdgeError('REPLY_CHARACTER_LIMIT')
                        yield part.text
                if candidate.finish_reason:finish=candidate.finish_reason.name
        self.llm_usage['finish_reason']=finish
        if finish!='STOP':raise EdgeError('INCOMPLETE_GENERATION')
    async def synthesize(self,text,*,chirp=False):
        t=self.tts_types;voice='en-US-Chirp3-HD-Aoede' if chirp else 'en-US-Wavenet-H'
        config={'audio_encoding':t.AudioEncoding.LINEAR16,'sample_rate_hertz':24000,'speaking_rate':.96}
        if not chirp:config['pitch']=2
        result=await self.tts.synthesize_speech(request=t.SynthesizeSpeechRequest(input=t.SynthesisInput(text=text),
            voice=t.VoiceSelectionParams(language_code='en-US',name=voice),audio_config=t.AudioConfig(**config)),
            retry=None,timeout=20,metadata=(('x-goog-api-key',self.credential['tts_key']),))
        return wave_pcm(result.audio_content)
    async def synthesize_stream(self,texts):
        t=self.tts_types
        async def requests():
            yield t.StreamingSynthesizeRequest(streaming_config=t.StreamingSynthesizeConfig(
                voice=t.VoiceSelectionParams(language_code='en-US',name=self.voice),
                streaming_audio_config=t.StreamingAudioConfig(audio_encoding=t.AudioEncoding.PCM,sample_rate_hertz=24000,speaking_rate=self.speaking_rate)))
            async for text in texts:yield t.StreamingSynthesizeRequest(input=t.StreamingSynthesisInput(text=text))
        stream=await self.tts.streaming_synthesize(requests=requests(),retry=None,timeout=30,metadata=(('x-goog-api-key',self.credential['tts_key']),))
        async for response in stream:
            data=bytes(response.audio_content)
            if data:
                if len(data)%2:raise EdgeError('ODD_PCM_BYTES')
                yield data
