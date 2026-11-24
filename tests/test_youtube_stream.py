"""Exercise the production streamList wire client against a local TLS gRPC server."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import datetime as dt
from importlib.util import find_spec
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from ravora.comments import CommentInbox
from ravora.conversation import MemoryStore
from ravora.settings import ProviderSettings
from ravora.youtube import YouTubeReader
from ravora.youtube_stream import METHOD

try:
    import grpc
    from ravora.youtube_proto import stream_list_pb2 as wire
except ImportError:
    grpc = None


@unittest.skipUnless(grpc and shutil.which('openssl'), 'requires the youtube-live extra and openssl')
class StreamListTests(unittest.TestCase):
    @unittest.skipUnless(find_spec('google.rpc') if grpc else False, 'requires Google RPC details')
    def test_structured_resource_exhaustion_honors_retry_delay_and_daily_limit(self):
        from google.protobuf.any_pb2 import Any
        from google.rpc import error_details_pb2, status_pb2
        from unittest.mock import Mock
        from ravora.youtube_stream import _rpc_error

        status = status_pb2.Status(code=8,message='sensitive detail')
        retry = error_details_pb2.RetryInfo()
        retry.retry_delay.seconds = 145
        packed = Any()
        packed.Pack(retry)
        status.details.append(packed)
        error = Mock()
        error.code.return_value = grpc.StatusCode.RESOURCE_EXHAUSTED
        error.trailing_metadata.return_value = [('grpc-status-details-bin',status.SerializeToString())]
        result = _rpc_error(error)
        self.assertEqual((str(result),result.retry_after),('YOUTUBE_GRPC_RESOURCE_EXHAUSTED',145))
        packed.Pack(error_details_pb2.ErrorInfo(reason='DAILY_LIMIT_EXCEEDED'))
        status.details.append(packed)
        error.trailing_metadata.return_value = [('grpc-status-details-bin',status.SerializeToString())]
        self.assertEqual(str(_rpc_error(error)),'YOUTUBE_DAILY_LIMIT_EXCEEDED')
        error.trailing_metadata.return_value = [('grpc-status-details-bin',b'bad metadata')]
        self.assertEqual(str(_rpc_error(error)),'YOUTUBE_GRPC_RESOURCE_EXHAUSTED')

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        cert, key = root/'cert.pem', root/'key.pem'
        subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1',
                        '-subj','/CN=localhost','-addext','subjectAltName=IP:127.0.0.1',
                        '-keyout',str(key),'-out',str(cert)],capture_output=True,check=True)
        self.trusted = cert.read_bytes()
        self.creds = grpc.ssl_server_credentials([(key.read_bytes(), self.trusted)])
        self.root = root
        self.store = MemoryStore(root/'isolated.db')
        self.inbox = CommentInbox(self.store)
        self.settings = replace(ProviderSettings(),youtube_read_enabled=True,youtube_key='unit-test-key',
                                youtube_chat_id='test-chat',youtube_poll_seconds=1.)
        self.no_http = Mock()
        self.no_http.get.side_effect = AssertionError('Unexpected HTTP request')

    def serve(self, handler):
        pool = ThreadPoolExecutor(2)
        self.addCleanup(pool.shutdown)
        server = grpc.server(pool)
        name,method = METHOD.lstrip('/').split('/')
        rpc = grpc.unary_stream_rpc_method_handler(handler,request_deserializer=wire.LiveChatMessageListRequest.FromString,
                                                    response_serializer=wire.LiveChatMessageListResponse.SerializeToString)
        server.add_generic_rpc_handlers((grpc.method_handlers_generic_handler(name,{method:rpc}),))
        port = server.add_secure_port('127.0.0.1:0',self.creds)
        server.start()
        self.addCleanup(lambda:server.stop(0).wait(2))
        for sub in (patch('ravora.youtube_stream.ENDPOINT',f'127.0.0.1:{port}'),
                    patch('grpc.ssl_channel_credentials',return_value=grpc.ssl_channel_credentials(self.trusted))):
            sub.start()
            self.addCleanup(sub.stop)

    def batch(self, mid, token, age=0):
        result = wire.LiveChatMessageListResponse(next_page_token=token)
        msg = result.items.add(id=mid)
        msg.snippet.type = wire.LiveChatMessageSnippet.TEXT_MESSAGE_EVENT
        msg.snippet.live_chat_id = 'test-chat'
        msg.snippet.published_at = (dt.datetime.now(dt.timezone.utc)-dt.timedelta(seconds=age)).isoformat()
        msg.snippet.text_message_details.message_text = 'hello'
        msg.author_details.channel_id = 'viewer-'+mid
        msg.author_details.display_name = 'Viewer'
        return result

    def test_push_disconnect_durable_resume_dedup_and_end_without_terminal_token(self):
        requests = []
        def handler(request, context):
            requests.append((request.live_chat_id,request.page_token,list(request.part),
                             dict(context.invocation_metadata()).get('x-goog-api-key')))
            if len(requests)==1:
                yield self.batch('old','p0',age=20)
                yield self.batch('old2','p00',age=15)
                yield self.batch('1','p1')
                context.abort(grpc.StatusCode.UNAVAILABLE,'synthetic break')
            yield self.batch('1','p1')
            yield self.batch('2','p2')
            yield wire.LiveChatMessageListResponse(offline_at='ended')
        self.serve(handler)
        self.inbox.accept_page('test-chat',{'nextPageToken':'legacy','pollingIntervalMillis':5000})
        reader=YouTubeReader(self.settings,self.inbox,transport=self.no_http)
        reader.run(threading.Event(),seconds=10)
        self.assertEqual(requests,[('test-chat',cursor,['id','snippet','authorDetails'],'unit-test-key') for cursor in ('','p1')])
        self.assertEqual((reader.status,reader.stream_opens,reader.batches),('ended',2,6))
        self.assertEqual(reader.counts,{'queued':2,'duplicate':1,'dropped':2})
        self.assertEqual(self.inbox.cursor('test-chat',streaming=True),'p2')
        self.assertEqual(self.inbox.cursor('test-chat'),'legacy')
        self.no_http.get.assert_not_called()

    @unittest.skipUnless(find_spec('av') and find_spec('PIL') and find_spec('numpy'), 'media extras required')
    def test_pushed_comment_reaches_encoded_voice_and_captions(self):
        from ravora.conversation import ConversationBridge
        from ravora.input_pipeline import InputPipeline
        from ravora.live_media import ProgramWriter, Scene, av
        from ravora.live_program import LiveProgram
        from ravora.safety import TurnFence
        import numpy as np

        def handler(request, context):
            yield self.batch('on-air', 'p1')
            yield wire.LiveChatMessageListResponse(offline_at='ended')

        self.serve(handler)
        settings = replace(self.settings, mode='offline', comment_source='youtube',
                           memory_enabled=False, character_initiative_enabled=False)
        bridge = ConversationBridge(settings,self.store,self.root,TurnFence(),threading.RLock())
        pipeline = InputPipeline(settings,self.store,bridge,[])
        program = LiveProgram(pipeline,self.root)
        output = self.root/'test.flv'
        writer = ProgramWriter(output,Scene())
        captions = []
        try:
            pipeline.start(10)
            deadline = time.monotonic()+10
            while not program.completed and time.monotonic()<deadline:
                stage, sound = program.next_frame()
                writer.write(stage,sound)
                program.encoded(stage)
                if stage.speaker=='Ray':
                    captions.append(stage.caption)
            self.assertTrue(any(captions))
            self.assertEqual((pipeline.reader.status,pipeline.reader.stream_opens,program.completed),('ended',1,1))
        finally:
            writer.close()
            program.close()
        with av.open(str(output)) as container:
            self.assertEqual(container.streams.video[0].codec_context.name,'hevc')
            signal = max(float(np.max(np.abs(f.to_ndarray()))) for f in container.decode(audio=0))
            self.assertGreater(signal,.03)

    @unittest.skipUnless(find_spec('av') and find_spec('PIL'), 'media extras required')
    def test_managed_launcher_encodes_before_transition_and_uses_oauth_chat(self):
        self._managed_launcher('timer')

    @unittest.skipUnless(find_spec('av') and find_spec('PIL'), 'media extras required')
    def test_continuous_launcher_completes_only_its_own_event_on_operator_stop(self):
        self._managed_launcher('operator')

    @unittest.skipUnless(find_spec('av') and find_spec('PIL'), 'media extras required')
    def test_continuous_launcher_preserves_live_event_on_encoder_failure(self):
        self._managed_launcher('failure')

    @unittest.skipUnless(find_spec('av') and find_spec('PIL'), 'media extras required')
    def test_pinned_launcher_resumes_the_live_event_and_receives_chat(self):
        self._managed_launcher('resume')

    def _managed_launcher(self, ending):
        import contextlib
        import importlib.util
        import io
        import json
        import sys
        from PIL import Image
        from ravora.live_media import ProgramWriter, SIZE, av
        from ravora.youtube_broadcast import BROADCAST, Broadcast
        from test_youtube_broadcast import FakeChannel

        api = FakeChannel()
        studio_event = {'id':'manual', 'status':{'lifeCycleStatus':'live'},
                        'contentDetails':{'boundStreamId':'studio-input'}}
        api.rows[BROADCAST]['manual'] = studio_event.copy()
        signals = {}
        duration = ['--seconds','2'] if ending == 'timer' else ['--until-stopped']
        console_link = self.root/'operator.url'
        received = []
        def handler(request, context):
            received.append((request.live_chat_id, dict(context.invocation_metadata())))
            yield wire.LiveChatMessageListResponse(offline_at='ended')
        self.serve(handler)

        from ravora import youtube_live as launcher
        env = self.root/'test.env'
        env.write_text('RAVORA_API_ENABLED=true\nYOUTUBE_READ_ENABLED=true\nGEMINI_API_KEY=a\nJEV_API_KEY=b\n'
                       'GOOGLE_TTS_API_KEY=c\nRAVORA_CHARACTER_INITIATIVE_ENABLED=false\n')
        env.chmod(0o600)
        media = self.root/'program.flv'
        scene_dir = self.root/'scene'
        scene_dir.mkdir()
        Image.new('RGB', SIZE).save(scene_dir/'neutral.png')
        (self.root/'config.toml').write_text('[youtube_live]\nvideo_encoder = "hevc_nvenc"\nmade_for_kids = false\n'
            '[youtube_live.metadata]\ntitle = "ライブ｜{date}"\ndescription = "Rayと話そう"\n'
            'tags = ["Ray", "AI VTuber"]\ncategory_id = "24"\n')
        if ending == 'resume':
            import tomllib
            from ravora.youtube_broadcast import video_metadata
            metadata = video_metadata(tomllib.loads((self.root/'config.toml').read_text())['youtube_live']['metadata'])
            (self.root/'state/youtube').mkdir(parents=True)
            existing = Broadcast(api, self.root/'state/youtube', 'owner', None, 'unlisted', threading.Event(),
                                 metadata=metadata, made_for_kids=False)
            existing.prepare()
            row = api.rows[BROADCAST][existing.state['broadcast']]
            row['status'].update(lifeCycleStatus='live', privacyStatus='public')
            row['snippet']['liveChatId'] = 'test-chat'
            duration += ['--resume-live', row['id'], '--privacy', 'public']
        class LocalOutput(ProgramWriter):
            def __init__(writer, target, scene, *, publish, video_encoder):
                self.assertTrue(publish)
                self.assertIsNone(target)
                self.assertEqual(video_encoder, 'hevc_nvenc')
                super().__init__(media, scene)
            def start_publish(writer, target):
                self.assertEqual(target, 'rtmps://a.rtmps.youtube.com/live2/fake-test-key')
                video, = api.rows['videos'].values()
                self.assertEqual(api.rows[BROADCAST][video['id']]['contentDetails']['latencyPreference'], 'ultraLow')
                self.assertIs(api.rows[BROADCAST][video['id']]['status']['selfDeclaredMadeForKids'], False)
                self.assertNotIn('selfDeclaredMadeForKids', api.rows[BROADCAST]['manual']['status'])
                self.assertRegex(video['snippet']['title'], r'^ライブ｜\d{4}-\d{2}-\d{2}$')
                self.assertEqual(video['snippet']['description'], 'Rayと話そう')
                self.assertCountEqual(video['snippet']['tags'], ['Ray', 'AI VTuber'])
                self.assertEqual(video['snippet']['categoryId'], '24')
            def write(writer, stage, pcm):
                super().write(stage, pcm)
                api.receiving = True
                if ending != 'timer':
                    self.assertLess(writer.frames, 180, 'continuous startup timed out')
                    if received:
                        self.assertTrue(console_link.is_file())
                        if ending == 'failure':
                            raise RuntimeError('simulated encoder failure')
                        signals[launcher.signal.SIGTERM]()
        wait, finish = Broadcast.wait_live, Broadcast.finish
        stdout = io.StringIO()
        with (patch.object(launcher, 'ROOT', self.root), patch.object(launcher.signal, 'signal', side_effect=signals.__setitem__),
              patch.object(launcher.ChannelAPI, 'connect', return_value=(api, 'owner')),
              patch.object(sys, 'argv', ['ravora-youtube-live','--config',str(self.root/'config.toml'),'--env',str(env),'--mode','cloud','--auto-broadcast',
                                         *duration,'--scene-dir',str(scene_dir),'--port','0','--operator-url-link',str(console_link)]),
              patch('ravora.live_media.ProgramWriter', LocalOutput),
              patch.object(Broadcast, 'wait_live', lambda b, **kwargs: wait(b, interval=.02, **kwargs)),
              patch.object(Broadcast, 'finish', lambda b: finish(b, interval=.02)),
              contextlib.redirect_stdout(stdout)):
            with (patch('ravora.live_media.ProgramWriter', side_effect=RuntimeError('unavailable')),
                  patch.object(Broadcast, 'prepare') as prepare):
                with self.assertRaisesRegex(SystemExit, 'PROGRAM_ENCODER_OPEN_FAILED .*hevc_nvenc'):
                    launcher.main()
                prepare.assert_not_called()
            if ending == 'failure':
                with self.assertRaisesRegex(SystemExit,'PROGRAM_FAILED'):
                    launcher.main()
            else:
                launcher.main()
        self.assertFalse(console_link.is_symlink())
        self.assertEqual(api.rows[BROADCAST]['manual']['status']['lifeCycleStatus'], 'live')
        if ending == 'failure':
            owned, = [row for key,row in api.rows[BROADCAST].items() if key != 'manual']
            self.assertEqual(owned['status']['lifeCycleStatus'], 'live')
            return
        result = json.loads(next((self.root/'.runtime').glob('youtube-live-*/summary.json')).read_text())
        self.assertTrue(result['live_confirmed'])
        self.assertEqual(result['avatar_renderer'], 'custom_scene')
        self.assertEqual(result['video_encoder'], 'libx265')  # The local test writer actually opened this codec.
        self.assertEqual(result['live_chat_id'], 'test-chat')
        self.assertEqual(api.rows[BROADCAST][result['video_id']]['status']['lifeCycleStatus'], 'complete')
        self.assertEqual([(chat, md.get('authorization'), md.get('x-goog-api-key')) for chat, md in received],
                         [('test-chat', 'Bearer mock-channel-access', None)])
        self.assertNotIn('fake-test-key', stdout.getvalue())
        self.assertNotIn('mock-channel-access', stdout.getvalue())
        with av.open(str(media)) as decoded:
            video = decoded.streams.video[0]
            self.assertEqual((video.codec_context.name, video.width, video.height, video.average_rate),
                             ('hevc', 1920, 1080, 30))
            self.assertEqual(decoded.streams.audio[0].codec_context.name, 'aac')
            self.assertEqual(sum(1 for _ in decoded.decode(video=0)), result['frame'])

    def test_oauth_stream_renews_a_rejected_token_once(self):
        authorizations = []
        refreshed = []
        def token(*, force_refresh=False):
            if force_refresh:
                refreshed.append(True)
            return 'new-token' if refreshed else 'old-token'
        def handler(request, context):
            auth = dict(context.invocation_metadata()).get('authorization')
            authorizations.append(auth)
            if auth == 'Bearer old-token':
                context.abort(grpc.StatusCode.UNAUTHENTICATED, 'expired')
            yield wire.LiveChatMessageListResponse(offline_at='end')
        self.serve(handler)
        reader = YouTubeReader(replace(self.settings, youtube_key=''), self.inbox, access_token=token)
        reader.run(threading.Event(), seconds=8)
        self.assertEqual((reader.status, refreshed), ('ended', [True]))
        self.assertEqual(authorizations, ['Bearer old-token', 'Bearer new-token'])

    def test_shutdown_cancels_an_idle_rpc(self):
        opened, cancelled, stop = threading.Event(), threading.Event(), threading.Event()
        def handler(request, context):
            context.add_callback(cancelled.set)
            opened.set()
            cancelled.wait(4)
            return iter(())
        self.serve(handler)
        reader=YouTubeReader(self.settings,self.inbox,transport=self.no_http)
        thread=threading.Thread(target=reader.run,args=(stop,),kwargs={'seconds':None})
        thread.start()
        try:
            self.assertTrue(opened.wait(3))
        finally:
            stop.set()
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertTrue(cancelled.wait(1))
        self.assertEqual((reader.status,reader.requests),('stopped',1))
