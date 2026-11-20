"""Authenticated loopback streaming console; credentials remain server-side."""
import asyncio
import hmac
from http import HTTPStatus
import json
from pathlib import Path
import secrets
import re
from urllib.parse import parse_qs,urlsplit

from ravora.input_records import DeveloperError
from ravora.edge_grpc import EdgeClients,EdgeError
from ravora.fast_voice import FastVoiceSession

ROOT=Path(__file__).resolve().parent


class RotatingCredentials(dict):
    def __init__(self,settings,path):
        super().__init__(gemini_key=settings.gemini_key,tts_key=settings.tts_key);self.path=Path(path)
    def __getitem__(self,key):
        if key=='access_token':
            value=json.loads(self.path.read_text()).get('access_token')
            if not isinstance(value,str) or not value or '\n' in value:raise EdgeError('STT_AUTH_UNAVAILABLE')
            return value
        return super().__getitem__(key)


class MockVoiceClients:
    """Offline transport used by the same normal UI; no outbound calls."""
    llm_usage={}
    async def open(self):return self
    async def close(self):pass
    async def transcribe_frames(self,frames,emit,**_):
        import struct
        size=0;partial=False;voiced=False
        async for data in frames:
            # Synthetic activity events for the offline console only.
            power=sum(sample*sample for (sample,) in struct.iter_unpack('<h',data))
            active=power >= (len(data)//2)*(32768*.01)**2
            if active!=voiced:
                await emit({'kind':'stt_activity','type':'SPEECH_ACTIVITY_BEGIN' if active else 'SPEECH_ACTIVITY_END',
                            'offset_ms':size/32})
                voiced=active
            size+=len(data)
            if not partial and size>=6400:
                partial=True
                await emit({'kind':'stt_partial','text':'I like coffee','stability':.9,'segment_final':False})
        await emit({'kind':'input_complete','input_seconds':size/32000})
        return 'I like coffee and jazz.',{'mock':True}
    async def generate(self,text,*,memories=(),**_):
        reply='You mentioned that you like coffee. What kind do you enjoy?' if memories else 'That sounds lovely. I enjoy hearing about your interests.'
        for value in (reply[:25],reply[25:]):
            yield value
            await asyncio.sleep(.03)
    async def synthesize_stream(self,texts):
        import math,struct
        async for _ in texts:
            # Audible, bounded diagnostic tone. This is deliberately not a synthesized voice.
            for part in range(5):
                yield b''.join(struct.pack('<h',int(2500*math.sin(2*math.pi*220*i/24000))) for i in range(2400))
                await asyncio.sleep(.025)


class FastVoiceServer:
    def __init__(self,settings,store,output,*,mode='cloud',port=0,clients=None,avatar_binding=None,
                 default_input='windows',dell_device=None,default_end_mode='manual',end_silence_ms=800):
        self.demo_assets,self.demo_avatar=avatar_binding or ({},None)
        self.settings=settings;self.store=store;self.output=Path(output);self.mode=mode;self.port=port;self.port_requested=bool(port)
        self.default_input=default_input;self.dell_device=dell_device
        self.default_end_mode=default_end_mode;self.end_silence_ms=end_silence_ms
        self.secret=secrets.token_urlsafe(32);self.clients=clients;self.connection=None;self.session=None
        self.sessions=[];self.http=None;self.ws=None;self.http_active=0
    async def start(self):
        from websockets.asyncio.server import serve
        if self.clients is None:
            if self.mode=='mock':self.clients=MockVoiceClients()
            else:
                if not self.settings.cloud_project or not self.settings.api_enabled or not self.settings.stt_api_enabled:raise EdgeError('FAST_VOICE_API_NOT_READY')
                self.clients=EdgeClients(RotatingCredentials(self.settings,self.output/'auth/token.json'),
                    stt_region=self.settings.voice_stt_region,tts_region=self.settings.voice_tts_region,
                    project=self.settings.cloud_project,language=self.settings.voice_input_language,
                    voice=self.settings.voice_tts_voice,speaking_rate=self.settings.speaking_rate,
                    stream_pitch=self.settings.pitch)
        try:
            await self.clients.open()
            self.http=await asyncio.start_server(self.handle_http,'127.0.0.1',self.port,limit=8192)
            self.port=self.http.sockets[0].getsockname()[1];self.origin=f'http://127.0.0.1:{self.port}'
            self.ws=await serve(self.handle_ws,'127.0.0.1',self.port+1 if self.port_requested else 0,process_request=self.authorize,
                origins=[self.origin],max_size=16384,max_queue=16,compression=None,ping_interval=20,ping_timeout=10)
            self.ws_port=self.ws.sockets[0].getsockname()[1]
        except BaseException:
            await self.close();raise
        return self
    def authorize(self,connection,request):
        parsed=urlsplit(request.path);query=parse_qs(parsed.query)
        valid=parsed.path=='/voice' and set(query)=={'token'} and len(query['token'])==1
        if not valid or not hmac.compare_digest(query['token'][0],self.secret):
            return connection.respond(HTTPStatus.UNAUTHORIZED,'UNAUTHORIZED\n')
        if self.connection is not None:return connection.respond(HTTPStatus.CONFLICT,'OPERATOR_ALREADY_CONNECTED\n')
    async def handle_http(self,reader,writer):
        if self.http_active>=8:
            writer.close();await writer.wait_closed();return
        self.http_active+=1
        try:
            async with asyncio.timeout(5):
                request=(await reader.readuntil(b'\r\n\r\n')).decode('ascii')
                lines=request.split('\r\n');method,path,_=lines[0].split(' ')
                headers={key.lower():value.strip() for key,value in (line.split(':',1) for line in lines[1:] if line)}
                if headers.get('host')!=f'127.0.0.1:{self.port}' or headers.get('sec-fetch-site')=='cross-site':status,body,mime=403,b'FORBIDDEN','text/plain'
                elif method!='GET':status,body,mime=405,b'METHOD_NOT_ALLOWED','text/plain'
                else:
                    files={'/':('developer/fast-index.html','text/html; charset=utf-8'),'/app.js':('developer/fast-app.js','application/javascript'),
                        '/talk-avatar.js':('developer/talk-avatar.js','application/javascript'),
                        '/style.css':('developer/style.css','text/css'),'/mic-stream.js':('developer/mic-stream.js','application/javascript'),
                        '/pcm-playout.js':('pcm-playout.js','application/javascript'),'/latency-meter.js':('latency-meter.js','application/javascript')}
                    for module in ('pngtuber-surface','expressions','presence','articulation','mouth-plan'):
                        files['/'+module+'.mjs']=(module+'.mjs','application/javascript')
                    if path=='/connection':
                        # Existing players only request the port. The console can also check its access
                        # before the browser's WebSocket API hides a rejected handshake's HTTP status.
                        auth=headers.get('authorization')
                        if auth is not None and not hmac.compare_digest(auth,'Bearer '+self.secret):
                            status,body,mime=401,b'UNAUTHORIZED','text/plain'
                        elif auth is not None and self.connection is not None:
                            status,body,mime=409,b'OPERATOR_ALREADY_CONNECTED','text/plain'
                        else:
                            status,body,mime=200,json.dumps({'port':self.ws_port}).encode(),'application/json'
                    elif path in files:
                        name,mime=files[path];status,body=200,(ROOT/'web'/name).read_bytes()
                    elif path=='/demo-avatar' and self.demo_avatar is not None:
                        status,body,mime=200,json.dumps(self.demo_avatar).encode(),'application/json'
                    elif path in self.demo_assets:
                        status,body,mime=200,self.demo_assets[path].read_bytes(),'image/png'
                    else:status,body,mime=404,b'NOT_FOUND','text/plain'
                csp=f"default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self' ws://127.0.0.1:{self.ws_port}; worker-src 'self'; media-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
                response=(f'HTTP/1.1 {status} {HTTPStatus(status).phrase}\r\nContent-Type: {mime}\r\nContent-Length: {len(body)}\r\n'
                    f'Cache-Control: no-store\r\nX-Content-Type-Options: nosniff\r\nReferrer-Policy: no-referrer\r\nContent-Security-Policy: {csp}\r\n'
                    'Permissions-Policy: microphone=(self), camera=(), geolocation=()\r\nConnection: close\r\n\r\n').encode()
                writer.write(response+body);await writer.drain()
        except (ValueError,UnicodeError,TimeoutError,ConnectionError,asyncio.IncompleteReadError,asyncio.LimitOverrunError):pass
        finally:self.http_active-=1;writer.close();await writer.wait_closed()

    async def handle_ws(self,ws):
        from websockets.exceptions import ConnectionClosed
        if self.connection is not None:await ws.close(code=1008,reason='OPERATOR_ALREADY_CONNECTED');return
        async def send(value):
            if ws.close_code is None:
                try:await ws.send(json.dumps(value,ensure_ascii=False))
                except ConnectionClosed:pass
        async def pcm(token,data):
            if session.current(token):await ws.send(bytes.fromhex(token['turn_id'])+data)
        session=FastVoiceSession(self.settings,self.store,self.output,self.clients,send,pcm,mock=self.mode=='mock',
            end_silence_ms=self.end_silence_ms)
        self.connection=ws
        self.session=session;self.sessions.append(session)
        try:
            await send({'kind':'ready','config':{**session.public(),'default_input':self.default_input,
                'dell_mic_available':self.dell_device is not None,'end_mode':self.default_end_mode,'handsfree':True,'avatar_available':self.demo_avatar is not None}})
            async for raw in ws:
                try:
                    if isinstance(raw,bytes):
                        if len(raw)<18:raise EdgeError('INVALID_MIC_FRAME')
                        await session.audio_chunk(raw[:16].hex(),raw[16:]);continue
                    value=json.loads(raw)
                    if not isinstance(value,dict):raise EdgeError('INVALID_MESSAGE')
                    kind=value.get('kind')
                    if kind=='audio_start' and {'kind','input_id','language'} <= value.keys() <= {'kind','input_id','language','source','end_mode'}:
                        # Older clients and recorded fixtures always supply their own PCM.
                        source=value.get('source','windows')
                        if source not in ('windows','dell'):raise EdgeError('INVALID_MIC_SOURCE')
                        if source=='dell' and self.dell_device is None:raise EdgeError('DELL_MIC_UNAVAILABLE')
                        await session.start_audio(value['input_id'],value['language'],
                            dell_device=self.dell_device if source=='dell' else None,end_mode=value.get('end_mode','manual'))
                    elif kind=='audio_end' and set(value)=={'kind','input_id'}:await session.end_audio(value['input_id'])
                    elif kind=='text' and set(value)=={'kind','input_id','text'}:
                        if not isinstance(value['text'],str):raise EdgeError('INVALID_TEXT')
                        await session.start_text(value['input_id'],value['text'])
                    elif kind=='cancel' and set(value)=={'kind'}:await session.cancel()
                    elif kind=='played' and set(value)=={'kind','token','frames'}:
                        if not isinstance(value['token'],dict) or set(value['token'])!={'session','generation','turn_id'}:raise EdgeError('INVALID_TURN_TOKEN')
                        accepted=session.played(value['token'],value['frames'])
                        if accepted:await send({'kind':'reaction_reset','input_id':session.input_id,'token':value['token']})
                        await send({'kind':'played_ack','accepted':accepted,'token':value['token']})
                    else:raise EdgeError('INVALID_MESSAGE')
                except (ValueError,TypeError,KeyError,EdgeError,DeveloperError) as exc:
                    await session.cancel('INVALID_INPUT');await send({'kind':'error','code':str(exc) if isinstance(exc,(EdgeError,DeveloperError)) else 'INVALID_MESSAGE'})
        except ConnectionClosed:
            pass  # A lost operator connection releases the microphone below.
        finally:
            try:await session.close()
            finally:self.connection=None
    async def close(self):
        if self.ws:self.ws.close();await self.ws.wait_closed()
        if self.http:self.http.close();await self.http.wait_closed()
        if self.clients:await self.clients.close()


async def serve(settings,store,run,*,mode,port,seconds,write_json,default_input='windows',dell_device=None,default_end_mode='manual',end_silence_ms=800):
    server=await FastVoiceServer(settings,store,run,mode=mode,port=port,default_input=default_input,dell_device=dell_device,
        default_end_mode=default_end_mode,end_silence_ms=end_silence_ms).start()
    write_json(run/'connection.json',{'url':f'{server.origin}/#token={server.secret}','port':server.port,
        'websocket_port':server.ws_port,'mode':mode,'profile':'fast_streaming'})
    write_json(run/'server.json',{'pid':__import__('os').getpid(),'mode':mode,'profile':'fast_streaming',
        'default_input':default_input,'dell_mic_available':dell_device is not None,
        'end_mode':default_end_mode,'end_silence_ms':end_silence_ms,
        'audio_sink':'operator browser only','api_enabled':settings.api_enabled,'stt_api_enabled':settings.stt_api_enabled})
    try:
        deadline=asyncio.get_running_loop().time()+seconds
        while asyncio.get_running_loop().time()<deadline and not (run/'stop').exists():await asyncio.sleep(.25)
    finally:
        await server.close()
        write_json(run/'server-stopped.json',{'stopped':True,'connections_closed':server.connection is None,
            'input_worker_alive':any(s.task and not s.task.done() for s in server.sessions),
            'capture_worker_alive':any(s.capture_task and not s.capture_task.done() for s in server.sessions),
            'memory_worker_alive':any(s.memory.thread and s.memory.thread.is_alive() for s in server.sessions)})
