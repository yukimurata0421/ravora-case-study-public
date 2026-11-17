"""Small replaceable provider adapters; credentials never enter browser data."""
import base64
import hashlib
import io
import json
from pathlib import Path
import ssl
import time
import urllib.error
import urllib.request
import wave

from ravora.speech import LocalSpeech
from ravora.context_prompt import CONTEXT_RULES,context_text
from ravora.relationships import RELATIONSHIP_RULES,relationship_text
from ravora.persona import conversation_prompt


class ProviderError(RuntimeError):
    """Only a stable code/status is exposed, never provider bodies or credentials."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        raise ProviderError('PROVIDER_REDIRECT_REJECTED')


class Transport:
    def __init__(self, timeout=10, *, enabled=False):
        self.timeout=timeout;self.enabled=enabled
        self.opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect(),urllib.request.HTTPSHandler(context=ssl.create_default_context()))

    def open(self,url,headers,payload=None):
        if not self.enabled:raise ProviderError('API_DISABLED')
        if not url.startswith(('https://generativelanguage.googleapis.com/','https://texttospeech.googleapis.com/','https://api.typesafe.ai/')):
            raise ProviderError('UNSUPPORTED_PROVIDER_ORIGIN')
        body=None if payload is None else json.dumps(payload).encode()
        request=urllib.request.Request(url,data=body,headers={'Content-Type':'application/json',**headers})
        try:return self.opener.open(request,timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            exc.close()
            raise ProviderError('PROVIDER_HTTP_'+str(exc.code)) from None
        except (OSError,urllib.error.URLError):raise ProviderError('PROVIDER_CONNECTION_FAILED') from None

    def json(self,url,headers,payload=None):
        with self.open(url,headers,payload) as response:
            data=response.read(12*1024*1024+1)
        if len(data)>12*1024*1024:raise ProviderError('PROVIDER_RESPONSE_TOO_LARGE')
        try:return json.loads(data)
        except (ValueError,UnicodeError):raise ProviderError('INVALID_PROVIDER_JSON') from None


def sse_events(response, deadline):
    data=[];total=0
    for raw in response:
        if time.monotonic()>deadline:raise ProviderError('GENERATION_DEADLINE')
        total+=len(raw)
        if total>256*1024:raise ProviderError('GENERATION_RESPONSE_TOO_LARGE')
        try:line=raw.decode('utf-8').rstrip('\r\n')
        except UnicodeError:raise ProviderError('INVALID_SSE_ENCODING') from None
        if not line:
            if data:
                try:yield json.loads('\n'.join(data))
                except ValueError:raise ProviderError('INVALID_SSE_JSON') from None
                data=[]
        elif line.startswith('data:'):data.append(line[5:].lstrip())
    if data:
        try:yield json.loads('\n'.join(data))
        except ValueError:raise ProviderError('INVALID_SSE_JSON') from None


class GeminiText:
    def __init__(self,settings,transport=None):
        self.prompt=conversation_prompt(settings.persona,settings.persona_file,reply_style=settings.reply_style)
        self.s=settings;self.http=transport or Transport(settings.timeout,enabled=settings.api_enabled)
        self.memories=[];self.usage={};self.context=[];self.relationship=None

    def set_memories(self,memories):self.memories=memories[:10]

    def set_context(self,context):self.context=context[:12]

    def set_relationship(self,background):self.relationship=background

    def stream(self,comment,history):
        prompt=self.prompt
        if self.context:prompt+=CONTEXT_RULES
        if self.relationship:prompt+=RELATIONSHIP_RULES
        self.usage={}
        contents=[{'role':role,'parts':[{'text':text}]} for role,text in history]
        parts=[]
        if self.memories:parts.append({'text':'Retrieved viewer reports (JSON):\n'+json.dumps(self.memories,ensure_ascii=False)})
        if self.context:parts.append({'text':context_text(self.context)})
        if self.relationship:parts.append({'text':relationship_text(self.relationship)})
        parts.append({'text':comment});contents.append({'role':'user','parts':parts})
        payload={'systemInstruction':{'parts':[{'text':prompt}]},'contents':contents,'generationConfig':{'maxOutputTokens':400,'temperature':.7}}
        url=f'https://generativelanguage.googleapis.com/v1beta/models/{self.s.gemini_model}:streamGenerateContent?alt=sse'
        ended=False;size=0
        with self.http.open(url,{'x-goog-api-key':self.s.gemini_key},payload) as response:
            for event in sse_events(response,time.monotonic()+self.s.turn_timeout):
                usage=event.get('usageMetadata',{})
                if isinstance(usage,dict):
                    for source,target in [('promptTokenCount','input_tokens'),('candidatesTokenCount','output_tokens'),('totalTokenCount','total_tokens'),('thoughtsTokenCount','thought_tokens'),('cachedContentTokenCount','cached_input_tokens')]:
                        value=usage.get(source)
                        if type(value) is int and value>=0:self.usage[target]=value
                if isinstance(event.get('modelVersion'),str):self.usage['response_model']=event['modelVersion']
                if event.get('promptFeedback',{}).get('blockReason'):raise ProviderError('GENERATION_BLOCKED')
                for candidate in event.get('candidates',[]):
                    if candidate.get('index',0)!=0:continue
                    reason=candidate.get('finishReason')
                    if reason and reason not in ('STOP','MAX_TOKENS'):raise ProviderError('GENERATION_BLOCKED')
                    for part in candidate.get('content',{}).get('parts',[]):
                        if part.get('thought') or not isinstance(part.get('text'),str):continue
                        text=part['text'];size+=len(text)
                        if size>self.s.max_reply_chars:raise ProviderError('REPLY_CHARACTER_LIMIT')
                        if text:yield text
                    if reason:ended=True
        if not ended:raise ProviderError('GENERATION_STREAM_INTERRUPTED')


class JevReaction:
    def __init__(self,settings,transport=None):self.s=settings;self.http=transport or Transport(min(settings.timeout,3),enabled=settings.api_enabled)

    def classify(self,comment,*,dialogue=False):
        self.usage={}
        labels={'joy':'Does this viewer message express joy, friendliness or thanks?','surprise':'Does this viewer message express surprise or amazement?','concern':'Does this viewer message express worry, sadness or frustration?'}
        if dialogue:
            current = 'Use the feeling by the end of this spoken transcript. Earlier feelings may be reversed or overtaken. '
            labels = {
                'joy': current + 'Is the speaker mainly expressing present delight, pride, or gratitude?',
                'surprise': current + 'Is the speaker reacting to a sudden or unexpected discovery with shock, amazement, or disbelief? Surprise can be pleasant.',
                'concern': current + 'Is the speaker now upset, disappointed, or worried about a setback or problem?',
            }
        payload={'model':self.s.jev_model,'state':comment,'questions':{k:{'type':'noul','instructions':v} for k,v in labels.items()}}
        answer=self.http.json('https://api.typesafe.ai/v1/systemone',{'Authorization':'Bearer '+self.s.jev_key},payload)
        self.usage={'response_model':answer.get('model')}
        scores={}
        for name in labels:
            item=answer.get('answers',{}).get(name,{})
            value=item.get('noul')
            if item.get('type')!='noul' or isinstance(value,bool) or not isinstance(value,(float,int)) or not 0<=value<=1:raise ProviderError('INVALID_JEV_REACTION')
            scores[name]=value
        label=max(scores,key=scores.get)
        allowed = ('joy', 'surprise', 'concern') if dialogue else ('joy', 'surprise')
        reaction=label if scores[label]>=.6 and label in allowed else 'neutral'
        if reaction == 'concern':
            reaction = 'concerned'
        return {'reaction':reaction,'scores':scores,'source':'jev-noul','model':answer.get('model',self.s.jev_model)}


class WaveNetSpeech:
    def __init__(self,settings,transport=None):self.s=settings;self.http=transport or Transport(settings.timeout,enabled=settings.api_enabled)

    def headers(self):
        headers={}
        if self.s.gcp_access_token:headers['Authorization']='Bearer '+self.s.gcp_access_token
        else:headers['x-goog-api-key']=self.s.tts_key
        if self.s.quota_project:headers['x-goog-user-project']=self.s.quota_project
        return headers

    def voices(self):
        reply=self.http.json('https://texttospeech.googleapis.com/v1/voices?languageCode='+self.s.language,self.headers())
        return [v for v in reply.get('voices',[]) if '-Wavenet-' in v.get('name','') and v.get('ssmlGender')=='FEMALE']

    def create(self,text,output:Path):
        if not isinstance(text,str) or not text.strip() or len(text.encode())>4500:raise ProviderError('INVALID_TTS_TEXT')
        payload={'input':{'text':text},'voice':{'languageCode':self.s.language,'name':self.s.voice},'audioConfig':{'audioEncoding':'LINEAR16','sampleRateHertz':48000,'speakingRate':self.s.speaking_rate,'pitch':self.s.pitch}}
        reply=self.http.json('https://texttospeech.googleapis.com/v1/text:synthesize',self.headers(),payload)
        duration,data=self.save_wave(reply,output)
        # Plain synthesis returns no phoneme/viseme timings.
        return {'source':'google-wavenet-waveform','voice':self.s.voice,'duration':duration,'audio_sha256':hashlib.sha256(data).hexdigest(),'visemes':[],'lip_sync':'decoded-waveform-spectral-heuristic'}

    def create_marked(self,text,output:Path,reference):
        from ravora.marked_speech import marked_input,retime_reference
        ssml,tokens=marked_input(text)
        payload={'input':{'ssml':ssml},'voice':{'languageCode':self.s.language,'name':self.s.voice},
                 'audioConfig':{'audioEncoding':'LINEAR16','sampleRateHertz':48000,'speakingRate':self.s.speaking_rate,'pitch':self.s.pitch},
                 'enableTimePointing':['SSML_MARK']}
        reply=self.http.json('https://texttospeech.googleapis.com/v1beta1/text:synthesize',self.headers(),payload)
        duration,data=self.save_wave(reply,output)
        try:timing=retime_reference(reference,tokens,reply.get('timepoints',[]),duration)
        except (ValueError,TypeError,KeyError):raise ProviderError('INVALID_WORD_TIMEPOINTS') from None
        return {'source':'google-wavenet-ssml-word-anchors-flite-phones','voice':self.s.voice,'pitch':self.s.pitch,
                'speaking_rate':self.s.speaking_rate,'duration':duration,'audio_sha256':hashlib.sha256(data).hexdigest(),
                'lip_sync':'provider-word-anchors-estimated-intraword-phones-v12',**timing}

    @staticmethod
    def save_wave(reply,output):
        try:
            data=base64.b64decode(reply['audioContent'],validate=True)
            with wave.open(io.BytesIO(data)) as reader:
                if reader.getnchannels()!=1 or reader.getsampwidth()!=2 or reader.getframerate()!=48000 or reader.getcomptype()!='NONE':raise ValueError()
                frames=reader.getnframes();duration=frames/48000
                if not 0<duration<=60 or len(reader.readframes(frames))!=frames*2:raise ValueError()
        except (ValueError,KeyError,TypeError,wave.Error,EOFError):raise ProviderError('INVALID_TTS_WAV') from None
        Path(output).write_bytes(data)
        return duration,data


# Each role has its own interface and factory, so adding a TTS adapter does not
# change generation, emotion classification, sentence control, or browser playback.
class FixtureText:
    def __init__(self):self.memories=[]

    def set_memories(self,memories):self.memories=memories[:10]

    def stream(self,comment,history):
        text='Welcome back. Let us take a quiet moment together. What would you like to talk about?'
        if self.memories:
            text=self.memories[0]['summary'].replace('Viewer said they ','You mentioned that you ',1)
            text+=' Would you like to tell me more about that?'
        for i in range(0,len(text),9):yield text[i:i+9]


class FixtureReaction:
    def classify(self,comment):return {'reaction':'joy','scores':{},'source':'offline-fixture'}


class FixtureSpeech:
    def create(self,text,output):
        with LocalSpeech() as voice:return voice.create(text,output)


def adapters(settings):
    settings.require_ready()
    if settings.mode=='offline':return FixtureText(),FixtureReaction(),FixtureSpeech()
    return GeminiText(settings),JevReaction(settings),WaveNetSpeech(settings)
