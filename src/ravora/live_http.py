"""Loopback stage feed and authenticated host microphone/monitor."""
from dataclasses import asdict
import re

from ravora.developer_http import Handler, RequestError, WEB
from ravora.live_mic import PACKET_BYTES


class LiveHandler(Handler):
    def get(self):
        path = self.path
        files = {'/': (WEB / 'live-host.html', 'text/html; charset=utf-8'),
                 '/host.js': (WEB / 'live-host.js', 'application/javascript'),
                 '/live-monitor.js': (WEB / 'live-monitor.js', 'application/javascript'),
                 '/live-metrics.js': (WEB / 'live-metrics.js', 'application/javascript'),
                 '/live-metrics.css': (WEB / 'live-metrics.css', 'text/css; charset=utf-8'),
                 '/mic-stream.js': (WEB / 'mic-stream.js', 'application/javascript'),
                 '/recorder.js': (WEB / 'recorder.js', 'application/javascript')}
        live = WEB.parent/'live'
        files.update({'/overlay':(live/'overlay.html','text/html; charset=utf-8'),
                      '/overlay.js':(live/'overlay.js','application/javascript'),
                      '/overlay.css':(live/'overlay.css','text/css; charset=utf-8')})
        if getattr(self.server, 'avatar_assets', None) is not None:
            files.update({'/program-avatar':(live/'avatar.html','text/html; charset=utf-8'),
                          '/program-avatar.js':(live/'avatar.js','application/javascript')})
            files['/talk-avatar.js'] = WEB/'talk-avatar.js', 'application/javascript'
            for name in ('pngtuber-surface.mjs','expressions.mjs','presence.mjs','articulation.mjs','mouth-plan.mjs'):
                files['/'+name] = WEB.parent/name, 'application/javascript'
            if path in self.server.avatar_assets:
                files[path] = self.server.avatar_assets[path], 'image/png'
            if path in ('/demo-avatar','/avatar-background.jpg'):
                self.guard(api=False)
                if path == '/demo-avatar':
                    self.reply(200, self.server.avatar_manifest)
                else:
                    self.reply(200, self.server.scene.avatar_background(), 'image/jpeg')
                return
        self.guard(api=path not in files and path not in ('/v1/stage','/overlay/frame.jpg'))
        program = self.server.session
        if path in files:
            file, mime = files[path]
            self.reply(200, file.read_bytes(), mime)
        elif path == '/overlay/frame.jpg':
            revision, data = self.server.scene.preview(self.headers.get('X-Overlay-Revision'))
            self.send_response(200 if data else 204)
            self.send_header('Content-Type','image/jpeg')
            self.send_header('Content-Length',str(len(data) if data else 0))
            self.send_header('X-Overlay-Revision',str(revision))
            self.send_header('Cache-Control','no-store')
            self.end_headers()
            if data:
                self.wfile.write(data)
        elif path == '/v1/stage':
            with program.lock:
                stage = asdict(program.stage)
            self.reply(200, stage)
        elif path == '/v1/host/metrics':
            self.reply(200, self.server.metrics.snapshot())
        elif path == '/v1/status':
            self.reply(200, {**program.status(), 'mic_source': 'dell' if getattr(self.server, 'dell_mic', None) else 'browser'})
        elif path == '/v1/monitor/next':
            after = self.headers.get('X-Ray-After')
            if after is not None and not re.fullmatch(r'[0-9]{1,15}', after):
                raise RequestError('INVALID_MONITOR_CURSOR')
            sequence, generation, frame, offset, join, data = program.monitor_packet(int(after) if after is not None else None)
            self.reply(200, data, 'audio/wav' if data else 'application/octet-stream', headers={
                'X-Ray-Sequence': sequence, 'X-Ray-Generation': generation, 'X-Ray-Frame': frame,
                'X-Ray-Offset-Samples': offset, 'X-Ray-Join': int(join)})
        elif re.fullmatch(r'/v1/monitor/[0-9a-f]{32}-[0-9]{1,2}(?:-p[0-9]{1,4})?\.wav', path):
            with program.lock:
                monitor = program.monitor
                if not monitor or path != '/v1/monitor/' + monitor[0] or not program.bridge.fence.current(program.token):
                    raise RequestError('AUDIO_EXPIRED', 404)
                data = monitor[1]
            self.reply(200, data, 'audio/wav')
        else:
            raise RequestError('NOT_FOUND', 404)

    def post(self):
        self.guard()
        program = self.server.session
        owner = self.headers.get('X-Mic-Session')
        dell = getattr(self.server, 'dell_mic', None)
        mic = dell or program.mic
        if self.path == '/v1/host/language':
            request = self.object(('language',))
            self.reply(200, program.select_language(request['language']))
        elif self.path == '/v1/host/mic/volume':
            request = self.object(('host_trim_db',))
            db = program.mic.set_host_trim_db(request['host_trim_db'])
            self.reply(200, {'host_trim_db': db})
        elif self.path == '/v1/host/bgm/volume':
            request = self.object(('volume_db',))
            if program.bgm is None:
                raise RequestError('BGM_NOT_CONFIGURED', 409)
            self.reply(200, {'volume_db': program.bgm.set_volume(request['volume_db'])})
        elif self.path in ('/v1/host/mic/open', '/v1/host/mic/open-auto'):
            self.object(())
            self.reply(200, {'session': mic.open(automatic=self.path.endswith('open-auto'))})
        elif self.path == '/v1/host/mic/mute':
            self.object(())
            mic.mute(owner)
            self.reply(200, {})
        elif dell and self.path == '/v1/host/mic/heartbeat':
            self.object(())
            dell.heartbeat(owner)
            self.reply(200, {})
        elif dell and self.path == '/v1/host/mic/question/start':
            self.object(())
            with program.lock:
                dell.start_question(owner, language=program.handsfree.language)
            self.reply(200, {})
        elif dell and self.path == '/v1/host/mic/question/finish':
            self.object(())
            data, language = dell.finish_question(owner)
            self.reply(202, program.submit_audio(self.headers.get('X-Input-ID'), data, already_aired=True,
                                                 language=language))
        elif self.path == '/v1/host/mic/pcm':
            if dell:
                raise RequestError('MIC_SOURCE_IS_DELL', 409)
            sequence = self.headers.get('X-Mic-Sequence', '')
            if not re.fullmatch(r'[0-9]{1,10}', sequence):
                raise RequestError('INVALID_MIC_SEQUENCE')
            data = self.body('application/octet-stream', PACKET_BYTES)
            program.mic.push(owner, int(sequence), data)
            self.reply(200, {})
        elif self.path in ('/v1/host/audio', '/v1/host/question'):
            data = self.body('audio/wav', program.pipeline.s.stt_max_seconds * 32000 + 4096)
            self.reply(202, program.submit_audio(self.headers.get('X-Input-ID'), data,
                                                 already_aired=self.path.endswith('/question'),
                                                 language=self.headers.get('X-Host-Language')))
        else:
            raise RequestError('NOT_FOUND', 404)
