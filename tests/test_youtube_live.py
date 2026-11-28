from contextlib import ExitStack
from dataclasses import replace
import io
import sys
import tempfile
import threading
import time
import unittest
import uuid
import wave
from pathlib import Path
from unittest.mock import patch

from ravora.comments import Comment
from ravora.conversation import ConversationBridge, MemoryStore
from ravora.input_pipeline import InputPipeline
from ravora.safety import TurnFence
from ravora.settings import ProviderSettings
from support import Reaction, Speech, Text

try:
    import av
    import numpy as np
    from ravora.live_media import Clip, FPS, ProgramWriter, Scene, SILENCE, Stage, publish_target
    from ravora.live_program import LiveProgram
    from ravora.live_mic import LiveMicrophone, PACKET_BYTES
    from ravora.developer_http import RequestError
except ModuleNotFoundError:
    av = None


def microphone_wav():
    buffer = io.BytesIO()
    with wave.open(buffer, 'wb') as wav:
        wav.setparams((1, 2, 16000, 0, 'NONE', ''))
        pcm = np.sin(np.arange(8000) * (2 * np.pi * 440 / 16000)) * 9000
        wav.writeframes(pcm.astype('<i2').tobytes())
    return buffer.getvalue()


@unittest.skipUnless(av, 'youtube-live extra required')
class LiveTests(unittest.TestCase):
    def test_program_voice_uses_conversation_stream_and_playable_pcm(self):
        from ravora.edge_grpc import EdgeClients
        from ravora.live_voice import LiveChirpSpeech
        stream = []
        samples = b'\x00\x01' * 2400
        async def synthesize(client, texts):
            stream.append((client.language, client.voice, client.speaking_rate, client.stream_pitch,
                           [text async for text in texts]))
            yield samples
        settings = replace(ProviderSettings(), language='ja-JP', voice_tts_voice='en-US-Chirp3-HD-Zephyr',
                           speaking_rate=.96, pitch=2)
        with tempfile.TemporaryDirectory() as directory, patch.object(EdgeClients,'synthesize_stream',synthesize):
            path = Path(directory)/'voice.wav'
            result = LiveChirpSpeech(settings).create('聞こえるよ。', path)
            with wave.open(str(path)) as audio:
                self.assertEqual(audio.getframerate(), 24000)
                self.assertEqual(audio.readframes(audio.getnframes()), samples)
            self.assertAlmostEqual(result['duration'], .1)
        self.assertEqual(stream,[('ja-JP','ja-JP-Chirp3-HD-Zephyr',.96,2,['聞こえるよ。'])])

    def test_validated_studio_pcm_can_air_before_eof_and_cannot_ack_partial_speech(self):
        from ravora.edge_grpc import EdgeClients, pcm_wave
        from ravora.live_media import decode_wav, SpeechPrefix
        from ravora.live_reverb import DEFAULT_RAY_REVERB_MIX, RayReverb
        from ravora.live_voice import LiveChirpSpeech
        from ravora.ray_character import public_fixture
        import json

        class Reply:
            def stream(self, *args):
                yield public_fixture('I remember the blue bridge.')
            stream.ray_public_output = True

        sound = np.random.default_rng(19).integers(-3000,3000,24000*2,dtype='<i2').tobytes()
        dry = decode_wav(pcm_wave(sound, 24000))
        for outcome in ('success', 'bypass', 'failed', 'cancelled'):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
                root = Path(directory)
                first, finish = threading.Event(), threading.Event()
                requests = []
                async def synthesize(client, texts):
                    requests.extend([t async for t in texts])
                    for offset in range(0, 48000, 4800):
                        yield sound[offset:offset+4800]
                    first.set()
                    if not finish.wait(4):
                        raise AssertionError('prefix never consumed')
                    if outcome == 'failed':
                        raise RuntimeError('fixture interruption')
                    yield sound[48000:]
                stack.enter_context(patch.object(EdgeClients,'synthesize_stream',synthesize))
                settings = replace(ProviderSettings(), memory_enabled=False, character_initiative_enabled=False)
                store = MemoryStore(root/'live.db')
                bridge = ConversationBridge(settings,store,root,TurnFence(),threading.RLock(),
                                            providers=(Reply(),Reaction(),LiveChirpSpeech(settings)))
                mix = 0.0 if outcome == 'bypass' else DEFAULT_RAY_REVERB_MIX
                program = LiveProgram(InputPipeline(settings,store,bridge,[]),root,ray_reverb_mix=mix)
                stack.callback(program.close)
                stack.callback(finish.set)
                with patch.object(program.stt,'transcribe',return_value={'speech': True, 'text': 'Do you remember?'}):
                    program.submit_audio(uuid.uuid4().hex, microphone_wav(), already_aired=True)
                monitor_clips = {}
                def tick():
                    stage, pcm = program.next_frame()
                    if program.monitor:
                        name, wav = program.monitor
                        if name not in monitor_clips:
                            with wave.open(io.BytesIO(wav)) as recording:
                                self.assertEqual(recording.getframerate(),48000)
                                monitor_clips[name] = recording.readframes(recording.getnframes())
                    program.encoded(stage)
                    return (stage.speaker,pcm)
                tick()
                token = program.token
                self.assertTrue(first.wait(2))
                monitor_start = program.monitor_packet(None)[0]
                self.assertFalse((root/(token['turn_id']+'-0.wav')).exists())
                self.assertFalse(bridge.claim(token,0))
                heard = []
                for _ in range(2 * SpeechPrefix.window // len(SILENCE)):
                    speaker, pcm = tick()
                    self.assertEqual(speaker,'Ray')
                    heard.append(pcm)
                self.assertIn('-p',program.monitor[0])
                self.assertEqual(program.completed,0)
                if outcome == 'cancelled':
                    program.pipeline.cancel()
                finish.set()
                bridge.worker.join(3)
                self.assertFalse(bridge.worker.is_alive())
                self.assertEqual(requests,['I remember the blue bridge.'])
                if outcome in ('success','bypass'):
                    for _ in range(70):
                        speaker, pcm = tick()
                        if speaker == 'Ray':
                            heard.append(pcm)
                        if program.completed:
                            break
                    self.assertEqual(program.completed,1)
                    expected = RayReverb(mix).render(dry, final=True) if mix else dry
                    # Both sinks contain each sample once across both prefixes, suffix and final decay.
                    self.assertEqual(b''.join(monitor_clips.values()), expected)
                    # A delayed client can still drain both prefixes and the WAV suffix after ACK.
                    cursor = monitor_start
                    recovered = bytearray()
                    while True:
                        seq, _, _, offset, join, wav = program.monitor_packet(cursor)
                        if not wav:
                            break
                        self.assertEqual((seq, offset, join), (cursor + 1, 0, False))
                        self.assertEqual(program.monitor_packet(cursor)[-1], wav)
                        with wave.open(io.BytesIO(wav)) as item:
                            recovered.extend(item.readframes(item.getnframes()))
                        cursor = seq
                    self.assertEqual(recovered, expected)
                    self.assertFalse(program.monitor_packet(None)[-1])
                    self.assertEqual(b''.join(heard), expected + bytes(-len(expected) % len(SILENCE)))
                else:
                    self.assertEqual(tick(),('',SILENCE))
                    self.assertIsNone(program.monitor)
                    self.assertFalse(program.monitor_packet(monitor_start)[-1])
                    self.assertIsNone(program.reverb.history)
                with store.connect() as db:
                    played, raw = db.execute('SELECT played,metrics FROM conversation_turns WHERE turn_id=?',
                                             (token['turn_id'],)).fetchone()
                self.assertEqual(bool(played),outcome in ('success','bypass'))
                if played:
                    metrics = json.loads(raw)
                    self.assertLess(metrics['first_pcm_ms'],metrics['first_audio_ready_ms'])
                    self.assertGreaterEqual(metrics['live_program']['stt_final_to_program_signal_ms'],
                                            metrics['live_program']['turn_to_program_signal_ms'])
                    self.assertGreaterEqual(metrics['live_program']['stt_ms'],0)

    def test_ray_room_impulse_is_quiet_short_and_resets_between_utterances(self):
        from ravora.live_media import RATE
        from ravora.live_reverb import RayReverb
        room = RayReverb()
        impulse = np.array([30000],dtype='<i2').tobytes()
        wave = np.frombuffer(room.render(impulse,final=True),dtype='<i2')
        self.assertEqual(wave[0],28200)
        self.assertFalse(wave[1:round(.008 * RATE)].any())
        self.assertLessEqual(len(wave)-1,round(.120 * RATE))
        reflected = wave[1:].astype(float)
        self.assertAlmostEqual(np.linalg.norm(reflected)/30000, .06, places=3)
        self.assertGreater(np.linalg.norm(reflected[:2400]), 8*np.linalg.norm(reflected[2400:]))
        self.assertEqual(room.render(SILENCE,final=True),SILENCE)
        self.assertEqual(RayReverb(0).render(impulse,final=True),impulse)

    def test_monitor_http_retries_join_expiry_and_revocation(self):
        from urllib.error import HTTPError
        from urllib.request import Request, urlopen
        from ravora.developer_http import DeveloperHTTP
        from ravora.live_http import LiveHandler

        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root = Path(directory)
            s = replace(ProviderSettings(), memory_enabled=False, character_initiative_enabled=False)
            store = MemoryStore(root/'live.db')
            bridge = ConversationBridge(s,store,root,TurnFence(),threading.RLock(),providers=(Text(),Reaction(),Speech()))
            program = LiveProgram(InputPipeline(s,store,bridge,[]),root,ray_reverb_mix=0)
            stack.callback(program.close)
            server = stack.enter_context(DeveloperHTTP(program,'test',0,handler=LiveHandler))
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            stack.callback(thread.join)
            stack.callback(server.shutdown)
            url = f'http://127.0.0.1:{server.server_port}/v1/monitor/next'
            def read(after=None):
                headers = {'Authorization':'Bearer test'}
                if after is not None:
                    headers['X-Ray-After'] = str(after)
                with urlopen(Request(url, headers=headers)) as response:
                    return response.headers, response.read()
            with self.assertRaises(HTTPError) as denied:
                urlopen(url)
            self.assertEqual(denied.exception.code,401)
            denied.exception.close()
            with self.assertRaises(HTTPError) as invalid:
                read(-1)
            self.assertEqual(invalid.exception.code,400)
            invalid.exception.close()
            header, empty = read()
            self.assertEqual((header['X-Ray-Sequence'], empty), ('0', b''))
            program.token = bridge.fence.begin('a'*32)
            for i in (1,2):
                program.clip = Clip('Ray','Hello',pcm=bytes([i,0]) * (12*1600))
                program._prepare_ray_audio(str(i),final=False)
                for _ in range(12):
                    program.clip.take()
                    program.encoded(Stage('Ray','Hello'))
            for cursor in (0,0,1):
                header, data = read(cursor)
                self.assertEqual((header['X-Ray-Sequence'],header['X-Ray-Offset-Samples']), (str(cursor+1),'0'))
                with wave.open(io.BytesIO(data)) as wav:
                    self.assertEqual(wav.readframes(wav.getnframes()),bytes([cursor+1,0])*19200)
            header, _ = read()
            self.assertEqual((header['X-Ray-Join'],header['X-Ray-Offset-Samples']),('1','19200'))
            for _ in range(4*FPS):
                program.encoded(Stage())
            self.assertFalse(program.monitor_history)
            self.assertEqual(read(0)[1], b'')
            program.clip = Clip('Ray','Again',pcm=SILENCE*12)
            program._prepare_ray_audio('fresh',final=True)
            bridge.fence.cancel()
            header, data = read(2)
            self.assertEqual((header['X-Ray-Generation'], data), ('1',b''))
            inbox = program.pipeline.inbox
            viewer = Comment('youtube','chat','message','channel','Viewer','Hello!',time.time())
            inbox.put(viewer)
            program.selected = inbox.take()['receipt']
            program.token = bridge.fence.begin('b'*32)
            inbox.bind(program.selected, program.token['turn_id'])
            cursor = program.monitor_sequence
            program._prepare_ray_audio('viewer',final=True)
            inbox.finish(program.selected, 'played')
            program._end(played=True)
            self.assertTrue(read(cursor)[1])
            deleted = {'id':'message','snippet':{'type':'tombstone','liveChatId':'chat'}}
            inbox.accept_page('chat', {'items':[deleted]}, streaming=True)
            self.assertEqual(read(cursor)[1], b'')

    def test_long_captions_keep_whole_clip_timing_and_source_filtered_latency_report(self):
        import importlib.util
        from ravora.live_media import SpeechPrefix
        from test_semantic_memory import seed
        prefix = SpeechPrefix('日本語の長い字幕を段落ごとに読み上げます。'*20)
        prefix.feed(b'\x01\x10'*24000)
        self.assertIsNone(prefix.take())
        self.assertEqual(len(prefix.pending),0)

        path = Path(__file__).resolve().parents[1]/'scripts/report_voice_latency.py'
        spec = importlib.util.spec_from_file_location('report_voice_latency',path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory:
            store = MemoryStore(Path(directory)/'report.db')
            seed(store, platform='live-host')
            seed(store, platform='live-host', played=False)
            seed(store, platform='developer-mock')  # Typed turn.
            result = module.report(store.path,60,'offline')
            self.assertEqual(result['studio']['played_audio_turns'],1)
            self.assertEqual(result['studio']['milliseconds']['stt_final_to_program_signal_ms'],
                             {'n':0,'missing':1,'p50':None,'p95':None})
            self.assertEqual(result['developer']['played_audio_turns'],0)

    def test_cloud_host_audio_uses_selected_language_and_accounts_once(self):
        from ravora.providers import ProviderError
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root = Path(directory)
            store = MemoryStore(root/'live.db')
            s = replace(ProviderSettings(), mode='cloud', api_enabled=True, stt_api_enabled=True,
                        cloud_project='test-project', gcp_access_token='fixture', stt_session_requests=2)
            bridge = ConversationBridge(s, store, root, TurnFence(), threading.RLock(),
                                        providers=(Text(), Reaction(), Speech()))
            program = LiveProgram(InputPipeline(s, store, bridge, []), root, host_language='ja')
            stack.callback(program.close)
            audio = microphone_wav()
            def submit(key, cancel=None):
                return program.submit_audio(key, audio, already_aired=True, cancel=cancel)
            response = {'results': [{'alternatives': [{'transcript': '聞こえます。'}]}],
                        'metadata': {'totalBilledDuration': '1s'}}
            http = program.cloud_stt.http
            with patch.object(http, 'json', return_value=response) as request:
                key = uuid.uuid4().hex
                self.assertEqual(submit(key)['status'], 'queued')
                self.assertEqual(submit(key)['status'], 'queued')
                request.assert_called_once()
                url, _, payload = request.call_args.args
                self.assertTrue(url.startswith('https://asia-northeast1-speech.googleapis.com/v2/'))
                self.assertEqual(payload['config']['languageCodes'], ['ja-JP'])
                self.assertEqual(program.host_caption, '聞こえます。')
                records = program.cloud_stt.records
                info = records.info(records.from_client(key))
                self.assertEqual((info['input_state'], info['stt_provider']), ('accepted', 'cloud_speech_v2'))
                self.assertEqual(info['stt_diagnostics']['provider_billed_seconds'], 1)
                self.assertGreater(info['stt_latency_ms'], 0)
                cancel = threading.Event()
                def muting(*_):
                    cancel.set()
                    return response
                request.side_effect = muting
                muted = uuid.uuid4().hex
                with self.assertRaisesRegex(RequestError, 'HOST_STT_CANCELLED'):
                    submit(muted, cancel)
                with self.assertRaisesRegex(RequestError, 'HOST_STT_CANCELLED'):
                    submit(muted)
                with self.assertRaisesRegex(RequestError, 'STT_SESSION_LIMIT'):
                    submit(uuid.uuid4().hex)
                self.assertEqual(request.call_count, 2)
                self.assertEqual(records.info(records.from_client(muted))['input_state'], 'cancelled')
            with self.assertRaisesRegex(ProviderError, 'UNSUPPORTED_PROVIDER_ORIGIN'):
                http.json('https://example.invalid/recognize', {}, {})

    def test_host_speech_api_failure_has_no_second_dispatch(self):
        from ravora.providers import ProviderError
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root = Path(directory)
            store = MemoryStore(root/'live.db')
            s = replace(ProviderSettings(), mode='cloud', api_enabled=True, stt_api_enabled=True,
                        cloud_project='test-project', gcp_access_token='fixture')
            bridge = ConversationBridge(s, store, root, TurnFence(), threading.RLock(),
                                        providers=(Text(), Reaction(), Speech()))
            program = LiveProgram(InputPipeline(s, store, bridge, []), root, host_language='en')
            stack.callback(program.close)
            key = uuid.uuid4().hex
            with patch.object(program.cloud_stt.http, 'json', side_effect=ProviderError('PROVIDER_CONNECTION_FAILED')) as rpc:
                for _ in range(2):
                    with self.assertRaisesRegex(RequestError, 'PROVIDER_CONNECTION_FAILED'):
                        program.submit_audio(key, microphone_wav())
                records = program.cloud_stt.records
                self.assertEqual(records.info(records.from_client(key))['input_state'], 'outcome_unknown')
                rpc.assert_called_once()
                self.assertEqual(rpc.call_args.args[2]['config']['languageCodes'], ['en-US'])
                self.assertFalse(program.host_caption)

    def test_host_only_cannot_start_a_broadcast(self):
        import subprocess
        for flag in ('--publish','--auto-broadcast','--publish-twitch'):
            result = subprocess.run([sys.executable, '-m', 'ravora.youtube_live', '--host-only', flag, '--check'],
                                    capture_output=True, text=True, timeout=15)
            self.assertNotEqual(result.returncode,0)
            self.assertIn('HOST_ONLY_CANNOT_PUBLISH',result.stderr)

    def test_encoder_configuration_is_checked_without_opening_a_device(self):
        import contextlib
        import importlib.util
        import json
        from ravora import youtube_live as launcher
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / 'config.toml'
            with (patch.object(launcher, 'ROOT', root),
                  patch.object(sys, 'argv', ['ravora-youtube-live', '--config', str(config), '--check']),
                  patch('ravora.live_media.ProgramWriter') as writer):
                for contents, expected, trim, cleanup in [('', 'libx265', 0.0, True),
                        ('[youtube_live]\nvideo_encoder="hevc_nvenc"\nhost_trim_db=-3.0\nhost_mic_cleanup=false',
                         'hevc_nvenc', -3.0, False)]:
                    config.write_text(contents)
                    with contextlib.redirect_stdout(io.StringIO()) as output:
                        launcher.main()
                    settings = json.loads(output.getvalue())
                    self.assertEqual(settings['video_encoder'], expected)
                    self.assertEqual(settings['host_trim_db'], trim)
                    self.assertIs(settings['host_mic_cleanup'], cleanup)
                    self.assertEqual(settings['ray_reverb_mix'],.06)
                    self.assertEqual(settings['latency_preference'], 'ultraLow')
                for latency in ('normal', 'low', 'ultraLow'):
                    config.write_text(f'[youtube_live]\nlatency_preference="{latency}"')
                    with contextlib.redirect_stdout(io.StringIO()) as output:
                        launcher.main()
                    self.assertEqual(json.loads(output.getvalue())['latency_preference'], latency)
                for value in ('true', '"ultralow"'):
                    config.write_text(f'[youtube_live]\nlatency_preference={value}')
                    with self.assertRaisesRegex(SystemExit, 'INVALID_YOUTUBE_LATENCY_PREFERENCE'):
                        launcher.main()
                config.write_text('[youtube_live]\nray_reverb_mix=0')
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    launcher.main()
                self.assertEqual(json.loads(output.getvalue())['ray_reverb_mix'],0.)
                for value in ('nan', '-0.1', '1.1', 'false', '"0.06"'):
                    config.write_text(f'[youtube_live]\nray_reverb_mix={value}')
                    with self.assertRaisesRegex(SystemExit, 'INVALID_RAY_REVERB_MIX'):
                        launcher.main()
                for invalid in ('youtube_live=false', '[youtube_live]\nvideo_encoder="h264_nvenc"'):
                    config.write_text(invalid)
                    with self.assertRaisesRegex(SystemExit, 'INVALID_PROGRAM_VIDEO_ENCODER'):
                        launcher.main()
                for value in ('nan', '1.0', '-25', 'false', '"-3"'):
                    config.write_text(f'[youtube_live]\nhost_trim_db={value}')
                    with self.assertRaisesRegex(SystemExit, 'INVALID_HOST_TRIM_DB'):
                        launcher.main()
                config.write_text('[youtube_live]\nhost_mic_cleanup="false"')
                with self.assertRaisesRegex(SystemExit, 'INVALID_HOST_MIC_CLEANUP'):
                    launcher.main()
                writer.assert_not_called()

    def test_twitch_configuration_selects_h264_without_opening_a_device_or_sending(self):
        import contextlib
        import importlib.util
        import json
        import os
        from ravora import youtube_live as launcher
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / 'config.toml'
            env = root / '.env'
            secret = 'dummy-twitch-key-for-test'
            env.write_text(f'TWITCH_STREAM_KEY={secret}\nYOUTUBE_READ_ENABLED=true\n')
            env.chmod(0o600)
            argv = ['ravora-youtube-live', '--config', str(config), '--env', str(env), '--check', '--publish-twitch']
            with (patch.object(launcher, 'ROOT', root), patch.object(sys, 'argv', argv),
                  patch.dict(os.environ, {}, clear=True),
                  patch.object(launcher, 'ChannelAPI') as youtube,
                  patch('ravora.live_media.ProgramWriter') as writer):
                for configuration, encoder in (('', 'libx264'),
                        ('[twitch_live]\nvideo_encoder="h264_nvenc"', 'h264_nvenc')):
                    config.write_text('[youtube_live]\nvideo_encoder="hevc_nvenc"\n' + configuration)
                    with contextlib.redirect_stdout(io.StringIO()) as output:
                        launcher.main()
                    self.assertNotIn(secret, output.getvalue())
                    report = json.loads(output.getvalue())
                    self.assertEqual(report['video_encoder'], encoder)
                    self.assertEqual(report['publish_destination'], 'twitch')
                    self.assertEqual(report['chat_input'], 'twitch')
                    self.assertTrue(report['stream_key_set'])
                    self.assertNotIn('oauth_token_file', report)
                config.write_text('[twitch_live]\nvideo_encoder="hevc_nvenc"')
                with self.assertRaisesRegex(SystemExit, 'INVALID_PROGRAM_VIDEO_ENCODER'):
                    launcher.main()
                with patch.object(sys, 'argv', argv + ['--auto-broadcast']), contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit):
                        launcher.main()
                youtube.connect.assert_not_called()
                writer.assert_not_called()

    def test_twitch_launch_reuses_cloud_db_and_bypasses_youtube(self):
        from contextlib import contextmanager, redirect_stdout
        import importlib.util
        import json
        import os
        from types import SimpleNamespace
        from ravora.live_media import TWITCH_INGEST
        from ravora.live_transport import rtmps_output
        from ravora.youtube_auth import publisher_lock

        from ravora import youtube_live as launcher
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'config.toml').write_text('')
            db = root / 'state/conversation.sqlite3'
            MemoryStore(db)
            frame = av.VideoFrame.from_ndarray(np.full((96, 160, 3), 128, dtype='uint8'), format='rgb24')
            scene = SimpleNamespace(render=lambda stage: frame.reformat(format='yuv420p'), avatar=None, clip=None)
            target = root / 'twitch.flv'
            secret = 'dummy-publish-key-for-test'
            destination = []
            @contextmanager
            def redirect_publish(url, streams):
                destination.append(url)
                with rtmps_output(str(target), streams) as output:
                    yield output

            argv = ['ravora-youtube-live', '--env', str(root / '.env'), '--mode', 'cloud', '--publish-twitch',
                    '--scene-dir', str(root), '--port', '0', '--seconds', '1']
            env = {'RAVORA_API_ENABLED': 'true', 'RAVORA_CHARACTER_INITIATIVE_ENABLED': 'false',
                   'YOUTUBE_READ_ENABLED': 'true', 'GEMINI_API_KEY': 'a', 'JEV_API_KEY': 'b',
                   'GOOGLE_TTS_API_KEY': 'c', 'TWITCH_STREAM_KEY': secret}
            with (patch.object(launcher, 'ROOT', root), patch.object(sys, 'argv', argv),
                  patch.dict(os.environ, env, clear=True), patch.object(launcher.signal, 'signal'),
                  patch.object(launcher, 'MemoryStore', wraps=MemoryStore) as store,
                  patch.object(launcher.ChannelAPI, 'connect') as youtube,
                  patch('ravora.input_pipeline.YouTubeReader') as reader,
                  patch('ravora.twitch_chat.TwitchReader.run', autospec=True) as twitch_reader,
                  patch('ravora.twitch_voice.warm_twitch_voices', autospec=True) as warm_voices,
                  patch('ravora.live_media.Scene', return_value=scene),
                  patch('ravora.live_media.SIZE', (160, 96)),
                  patch('ravora.live_transport.rtmps_output', redirect_publish),
                  redirect_stdout(io.StringIO()) as stdout):
                with publisher_lock(root / 'state/youtube'):
                    with self.assertRaisesRegex(SystemExit, 'RAY_PUBLISHER_ALREADY_RUNNING'):
                        launcher.main()
                    store.assert_not_called()
                launcher.main()
                store.assert_called_once_with(db)
                youtube.assert_not_called()
                reader.assert_not_called()
                twitch_reader.assert_called_once()
                warm_voices.assert_called_once()
                self.assertEqual(twitch_reader.call_args.args[0].inbox.store.path, db)
            self.assertEqual(destination, [f'{TWITCH_INGEST}/{secret}'])
            summary_path, = (root / '.runtime').glob('twitch-live-*/summary.json')
            summary_json = summary_path.read_text()
            self.assertNotIn(secret, stdout.getvalue() + summary_json)
            summary = json.loads(summary_json)
            self.assertTrue(summary['publish'])
            self.assertEqual(summary['publish_destination'], 'twitch')
            self.assertEqual(summary['chat']['transport'], 'twitch_eventsub_websocket')
            with av.open(str(target)) as output:
                self.assertEqual(output.streams.video[0].codec_context.name, 'h264')
                self.assertEqual(output.streams.audio[0].codec_context.name, 'aac')
                self.assertTrue(next(output.decode(video=0)).key_frame)

    def test_conversation_profiles_bind_the_prompt_and_voice_to_the_same_language(self):
        from ravora.live_voice import live_reply_profiles
        for profile in ('fast_streaming', 'legacy'):
            profiles = live_reply_profiles(replace(ProviderSettings(), voice_profile=profile))
            for language, locale, prompt in [('ja', 'ja-JP', '自然な日本語'), ('en', 'en-US', 'conversational English')]:
                with self.subTest(profile=profile, language=language):
                    settings, text, speech = profiles[language]
                    self.assertEqual(settings.language, locale)
                    self.assertIn(prompt, text.prompt)
                    self.assertTrue(getattr(speech, 'voice', speech.s.voice).startswith(locale + '-'))

    def test_youtube_reply_language_uses_the_body_and_falls_back_to_the_host_tab(self):
        from ravora.live_program import youtube_reply_language
        for text, language in (
            ('初見です！', 'ja'), ('最高', 'ja'), ('RayのYouTube Live、楽しい！', 'ja'),
            ('AIとBGM', 'ja'), ('ﾔｯﾀｰ', 'ja'), ('“fantastic”ってどういう意味？', 'ja'),
            ('Hi!', 'en'), ('Ｆａｎｔａｓｔｉｃ！', 'en'), ('I LOVE 東京!', 'en'),
            ('@星空 What does 「流れ星」 mean?', 'en'), ('「ありがとう」', 'ja'),
        ):
            with self.subTest(text=text):
                self.assertEqual(youtube_reply_language(text, 'en' if language=='ja' else 'ja'), language)
        for tab in ('ja','en'):
            self.assertEqual(youtube_reply_language('🎶 888 @Ray :_clap: https://example.org/星', tab), tab)

    def test_viewer_and_host_turns_route_prompt_captions_and_voice_independently(self):
        import json
        from ravora.edge_grpc import EdgeClients
        from ravora.live_voice import live_reply_profiles
        from ravora.ray_character import public_fixture

        replies = {'en-US':'I like the stars.', 'ja-JP':'星が好きだよ。'}
        prompts, voices = [], []
        def generate(url, headers, payload):
            prompt = payload['systemInstruction']['parts'][0]['text']
            locale = 'ja-JP' if '自然な日本語' in prompt else 'en-US'
            if locale == 'en-US':
                self.assertIn('conversational English', prompt)
            prompts.append((locale, payload['contents'][-1]['parts'][-1]['text']))
            part = {'text':public_fixture(replies[locale])}
            event = {'candidates':[{'finishReason':'STOP','content':{'parts':[part]}}]}
            return io.BytesIO(('data: ' + json.dumps(event) + '\n\n').encode())
        async def synthesize(client, texts):
            voices.append((client.language, client.voice, ''.join([text async for text in texts])))
            yield b'\x00\x08' * 2400

        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root = Path(directory)
            s = replace(ProviderSettings(), voice_profile='fast_streaming', comment_source='youtube',
                        memory_enabled=False, character_idle_seconds=60, character_max_initiatives=-1)
            profiles = live_reply_profiles(s)
            for _, text, _ in profiles.values():
                stack.enter_context(patch.object(text.http, 'open', side_effect=generate))
            stack.enter_context(patch.object(EdgeClients, 'synthesize_stream', synthesize))
            store = MemoryStore(root/'live.db')
            bridge = ConversationBridge(s,store,root,TurnFence(),threading.RLock(),
                                        providers=(profiles['ja'][1],Reaction(),profiles['ja'][2]))
            program = LiveProgram(InputPipeline(s,store,bridge,[]),root,reply_profiles=profiles,host_language='ja')
            stack.callback(program.close)
            now = [0.]
            ray = bridge.controller.character
            ray.clock = lambda:now[0]
            program.next_frame()
            now[0] = 60
            with program.ingress:
                program.next_check = 0
                program.next_frame()
            now[0] += 59
            program.next_check = 0
            program.next_frame()
            self.assertIsNone(program.token)
            now[0] += 1
            turns = [('ja','ray-initiative',None,'en-US'),
                     ('ja','youtube','Which stars do you like?', 'en-US'),
                     ('ja','live-host','Fantastic!', 'ja-JP'),
                     ('en','twitch','好きな星座は？', 'ja-JP'),
                     ('en','live-host','そうだね。', 'en-US'),
                     ('en','youtube','🎶', 'en-US'),
                     ('ja','ray-initiative',None,'en-US')]
            for count, (tab, source, message, locale) in enumerate(turns, 1):
                program.select_language(tab)
                if count > 1:
                    now[0] += 60  # A waiting human takes precedence even when an initiative is due.
                program.next_check = 0
                if source in ('youtube', 'twitch'):
                    viewer = Comment(source,'chat',str(count),'channel','Viewer',message,time.time())
                    self.assertEqual(program.pipeline.inbox.put(viewer), 'queued')
                elif source == 'live-host':
                    with patch.object(program.stt,'transcribe',return_value={'speech':True,'text':message}) as stt:
                        program.submit_audio(uuid.uuid4().hex,microphone_wav(),already_aired=True)
                        self.assertEqual(stt.call_args.kwargs['language'], tab)
                captions = set()
                deadline = time.monotonic()+5
                while program.completed < count and time.monotonic()<deadline:
                    stage, _ = program.next_frame()
                    if stage.speaker == 'Ray':
                        captions.add(stage.caption)
                    program.encoded(stage)
                    time.sleep(.003)
                self.assertEqual(program.completed, count, program.last_error)
                self.assertEqual(program.conversation_language, tab)
                if source == 'ray-initiative':
                    self.assertEqual(prompts[count-1][0],locale)
                    self.assertIn('There is no new host or viewer message',prompts[count-1][1])
                else:
                    self.assertEqual(prompts[count-1], (locale, message))
                voice_locale, name, spoken = voices[count-1]
                self.assertEqual(voice_locale, locale)
                self.assertTrue(name.startswith(locale+'-'))
                self.assertEqual(spoken, replies[locale])
                self.assertEqual(captions, {spoken})

    def test_tab_change_during_recognition_preserves_queued_reply_and_next_turn_switches(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            s = replace(ProviderSettings(), memory_enabled=False, character_initiative_enabled=False)
            profiles = {code: (replace(s, language=locale), Text(), Speech())
                        for code, locale in [('ja', 'ja-JP'), ('en', 'en-US')]}
            store = MemoryStore(root/'live.db')
            bridge = ConversationBridge(s, store, root, TurnFence(), threading.RLock(),
                                        providers=(profiles['ja'][1], Reaction(), profiles['ja'][2]))
            program = LiveProgram(InputPipeline(s, store, bridge, []), root, reply_profiles=profiles, host_language='ja')
            def recognize(packet, *, cancel, language):
                self.assertEqual(language, 'ja')
                program.select_language('en')
                return {'speech': True, 'text': 'こんにちは。'}
            def play(count):
                deadline = time.monotonic() + 5
                while program.completed < count and time.monotonic() < deadline:
                    stage, _ = program.next_frame()
                    program.encoded(stage)
                    time.sleep(.003)
                self.assertEqual(program.completed, count)
            try:
                with self.assertRaisesRegex(RequestError, 'INVALID_CONVERSATION_LANGUAGE'):
                    program.select_language(['en'])
                with patch.object(program.stt, 'transcribe', side_effect=recognize):
                    program.submit_audio(uuid.uuid4().hex, microphone_wav(), already_aired=True)
                self.assertEqual(program.status()['conversation_language'], 'en')
                play(1)
                self.assertTrue(profiles['ja'][2].calls)
                self.assertFalse(profiles['en'][2].calls)
                viewer = Comment('youtube', 'chat', 'message', 'channel', 'Viewer', 'Hello!', time.time())
                self.assertEqual(program.pipeline.inbox.put(viewer), 'queued')
                play(2)
                self.assertTrue(profiles['en'][2].calls)
            finally:
                program.close()

    def test_handsfree_rearms_after_rejection_and_ignores_playback_and_muted_audio(self):
        from ravora.live_handsfree import LiveHandsfree
        audio, languages = [], []
        seen = threading.Event()
        def submit(key, wav, *, already_aired, cancel, language):
            languages.append(language)
            self.assertTrue(already_aired)
            self.assertFalse(cancel.is_set())
            with wave.open(io.BytesIO(wav)) as stream:
                self.assertEqual(stream.getframerate(), 16000)
                audio.append(stream.getnframes())
            seen.set()
            if len(audio) == 1:
                raise RequestError('HOST_SPEECH_UNCLEAR', 422)

        auto = LiveHandsfree(submit, 3, -38, 800, language='ja')
        self.addCleanup(auto.close)
        quiet = bytes(PACKET_BYTES)
        loud = (np.sin(np.arange(PACKET_BYTES // 2) * .21) * 2000).astype('<i2').tobytes()
        phrase = [quiet] * 3 + [loud] * 4 + [quiet] * 8
        auto.session(True)
        for expected in ('HOST_SPEECH_UNCLEAR', 'received'):
            seen.clear()
            for packet in phrase[:7]:
                auto.feed(packet)
            self.assertTrue(auto.speaking())
            with auto.condition:
                auto.language = 'en'
            for packet in phrase[7:]:
                auto.feed(packet)
            self.assertTrue(seen.wait(2))
            with auto.condition:
                self.assertTrue(auto.condition.wait_for(lambda: auto.result == expected, 2))
            self.assertEqual(auto.status()['result'], expected)
        self.assertEqual(len(audio), 2)
        self.assertEqual(languages, ['ja', 'en'])
        auto.playback()
        for packet in phrase:
            auto.feed(packet)
        auto.session(False)
        for packet in phrase:
            auto.feed(packet)
        self.assertIsNone(auto.pending)
        self.assertEqual(len(audio), 2)

    def test_handsfree_mute_invalidates_inflight_recognition_and_drops_queued_audio(self):
        from ravora.live_handsfree import LiveHandsfree
        started, finished = threading.Event(), threading.Event()
        calls = []
        def submit(key, wav, *, already_aired, cancel, language):
            calls.append(key)
            started.set()
            self.assertTrue(cancel.wait(2))
            finished.set()
        auto = LiveHandsfree(submit, 2, -38, 300)
        self.addCleanup(auto.close)
        auto.session(True)
        phrase = [b'\x10\x10' * (PACKET_BYTES // 2)] * 4 + [bytes(PACKET_BYTES)] * 4
        for pcm in phrase:
            auto.feed(pcm)
        self.assertTrue(started.wait(2))
        for pcm in phrase * 2:
            auto.feed(pcm)
        self.assertEqual(auto.result, 'HOST_MIC_BUSY')
        auto.session(False)
        self.assertTrue(finished.wait(2))
        self.assertEqual(len(calls), 1)
        self.assertEqual(auto.status()['state'], 'off')

    def test_dell_capture_fences_other_tabs_and_releases_device_on_lost_lease(self):
        from ravora.live_dell_mic import DellCapture
        # Feed 100 ms packets at the same rate as the capture device.
        source = [sys.executable, '-I', '-c',
                  'import os, time\nwhile True:\n time.sleep(.1)\n os.write(1, bytes(9600))']
        capture = DellCapture('Device,0', LiveMicrophone(), 2)
        self.addCleanup(capture.close)
        with patch('ravora.live_dell_mic.capture_command', return_value=source):
            owner = capture.open()
            with self.assertRaisesRegex(RequestError, 'MIC_ALREADY_OPEN'):
                capture.open()
            with self.assertRaisesRegex(RequestError, 'MIC_SESSION_EXPIRED'):
                capture.heartbeat('other')
            capture.mute('other')
            capture.start_question(owner, language='en')
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                with capture.lock:
                    if len(capture.question) >= 2 * PACKET_BYTES:
                        break
                time.sleep(.02)
            wav, language = capture.finish_question(owner)
            self.assertEqual(language, 'en')
            with wave.open(io.BytesIO(wav)) as recorded:
                self.assertGreaterEqual(recorded.getnframes(), 3200)
            capture.start_question(owner, language='ja')
            with capture.lock:
                first = capture.child
                capture.expires = time.monotonic() - 1
            first.wait(timeout=3)
            second = capture.open()
            self.assertNotEqual(owner, second)
            capture.mute(owner)
            capture.heartbeat(second)
            self.assertIsNone(capture.question)
            child = capture.child
            capture.close()
            self.assertIsNotNone(child.poll())
            with self.assertRaisesRegex(RequestError, 'PROGRAM_STOPPED'):
                capture.open()

    def test_destination_credentials_are_private_and_only_used_for_studio(self):
        from ravora.live_media import TWITCH_INGEST, twitch_publish_target
        secret = 'dummy-key-for-test'
        base_url = 'rtmps://a.rtmps.youtube.com:443/live2'
        self.assertEqual(publish_target(base_url, secret), 'rtmps://a.rtmps.youtube.com:443/live2/' + secret)
        self.assertNotIn('youtube_stream_key', vars(ProviderSettings()))
        for base in ('rtmp://a.rtmps.youtube.com/live2', 'rtmps://x.invalid/live2',
                     'rtmps://a.rtmps.youtube.com:443/live2/another', 'rtmps://a.rtmps.youtube.com@x.invalid/live2'):
            with self.assertRaisesRegex(ValueError, 'INVALID_YOUTUBE_RTMPS_URL'):
                publish_target(base, secret)
        with self.assertRaisesRegex(ValueError, 'STREAM_KEY'):
            publish_target(base_url, '')
        self.assertEqual(twitch_publish_target(TWITCH_INGEST, secret), f'{TWITCH_INGEST}/{secret}')
        regional = 'rtmps://sfo.contribute.live-video.net:443/app'
        self.assertEqual(twitch_publish_target(regional + '/', secret), f'{regional}/{secret}')
        self.assertNotIn('twitch_stream_key', vars(ProviderSettings()))
        for base in (base_url, TWITCH_INGEST.replace('rtmps:', 'rtmp:'),
                     TWITCH_INGEST + '?query=1', TWITCH_INGEST + '/embedded-key',
                     'rtmps://ingest.global-contribute.live-video.net.example.org/app'):
            with self.assertRaisesRegex(ValueError, 'INVALID_TWITCH_RTMPS_URL'):
                twitch_publish_target(base, secret)
        with self.assertRaisesRegex(ValueError, 'MISSING_OR_INVALID_TWITCH_STREAM_KEY'):
            twitch_publish_target(TWITCH_INGEST, secret + '/extra')

    def test_encoded_audio_and_caption_change_on_same_timeline(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'program.flv'
            clip = Clip('Host', '聞こえますか？', microphone_wav())
            writer = ProgramWriter(path, Scene())
            try:
                writer.write(Stage(), SILENCE)
                while not clip.done:
                    caption, pcm = clip.take()
                    writer.write(Stage('Host', caption), pcm)
                writer.write(Stage(), SILENCE)
            finally:
                writer.close()
            with av.open(str(path)) as source:
                video = source.streams.video[0]
                self.assertEqual((video.codec_context.name, video.width, video.height, video.average_rate),
                                 ('hevc', 1920, 1080, FPS))
                patch_means = []
                for frame in source.decode(video=0):
                    pixels = frame.to_ndarray(format='gray')
                    patch_means.append((pixels[884:965, 105:570] > 180).sum())
                self.assertEqual(len(patch_means), 17)
                self.assertEqual(patch_means[0], 0)
                self.assertGreater(max(patch_means[1:-1]), 400)
                self.assertEqual(patch_means[-1], 0)
            with av.open(str(path)) as source:
                audio = source.streams.audio[0]
                self.assertEqual((audio.codec_context.name, audio.rate, audio.channels), ('aac', 48000, 2))
                signal = np.concatenate([f.to_ndarray()[0] for f in source.decode(audio=0)])
                self.assertGreater(np.max(np.abs(signal[4000:10000])), .1)

    def test_studio_starts_its_own_topic_and_chat_can_pick_up_only_a_heard_opening(self):
        from ravora.ray_character import public_fixture
        opening = 'A puzzle with the answer first gives the clue a second meaning.'
        class Reply:
            def __init__(self): self.calls = []
            def stream(self,text,history):
                self.calls.append((text,history))
                yield public_fixture(opening if len(self.calls)==1 else 'The next clue could overturn that answer.')
            stream.ray_public_output = True
        for source,revoked in [('youtube',False),('developer',False),('youtube',True)]:
            with self.subTest(source=source,revoked=revoked), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                s = replace(ProviderSettings(),comment_source=source,memory_enabled=False)
                store,reply = MemoryStore(root/'test.db'),Reply()
                bridge = ConversationBridge(s,store,root,TurnFence(),threading.RLock(),providers=(reply,Reaction(),Speech()))
                pipeline = InputPipeline(s,store,bridge,[])
                program = LiveProgram(pipeline,root)
                def play(count):
                    deadline = time.monotonic()+5
                    while program.completed<count and time.monotonic()<deadline:
                        stage,_ = program.next_frame()
                        program.encoded(stage)
                        time.sleep(.002)
                    self.assertEqual(program.completed,count,program.last_error)
                try:
                    ray = bridge.controller.character
                    # Auth, the encoder and YouTube confirming live can all take time.
                    ray.last_activity -= 300
                    program.next_frame()
                    self.assertIsNone(program.token)
                    ray.last_activity -= 46
                    mic = program.mic.open(automatic=False)
                    program.next_check=0
                    program.next_frame()
                    self.assertEqual(ray.initiatives,0)
                    program.mic.mute(mic)
                    program.next_check=0
                    play(1)
                    self.assertIn('introducing a topic',reply.calls[0][0])
                    self.assertEqual(reply.calls[0][1],[])
                    with store.connect() as db:
                        turn,receipt = db.execute("SELECT turn_id,receipt FROM comment_inbox WHERE source='ray-initiative' AND status='played'").fetchone()
                        self.assertEqual(db.execute('SELECT count(*) FROM memory_jobs WHERE turn_id=?',(turn,)).fetchone(),(0,))
                        if revoked: store.remove_source(db,receipt,time.time())
                    from ravora.ray_character import CharacterOpening
                    next_opening = CharacterOpening('games',previous=tuple(ray.aired_openings))
                    excerpts = store.followup_history(next_opening,s.mode)
                    self.assertEqual([text for role,text in excerpts if role=='model'],[] if revoked else [opening])
                    self.assertEqual(store.followup_history(next_opening,'another-scope'),[])
                    ray.last_activity -= 46
                    program.next_check=0
                    program.next_frame()
                    self.assertIsNone(program.token)
                    platform = 'youtube' if source=='youtube' else 'live-host'
                    pipeline.inbox.put(Comment(platform,pipeline.inbox.session,'one','someone','Guest','Go on.',time.time()))
                    play(2)
                    expected=[] if revoked else [('user','Ray opened this conversation.'),('model',opening)]
                    self.assertEqual(reply.calls[1][1],expected)
                finally:
                    program.close()

    def test_studio_resumes_only_rays_public_speech_in_preview_and_broadcast(self):
        from ravora.ray_character import public_fixture

        class Reply:
            def __init__(self):
                self.calls = []
            def stream(self,text,history):
                self.calls.append((text,history))
                yield public_fixture('I wonder how two stars can look close together.')
            stream.ray_public_output = True

        for source in ('developer','youtube'):
            with self.subTest(source=source), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                s = replace(ProviderSettings(),comment_source=source,memory_enabled=False)
                store = MemoryStore(root/'state.db')
                reply = Reply()
                bridge = ConversationBridge(s,store,root,TurnFence(),threading.RLock(),providers=(reply,Reaction(),Speech()))
                pipeline = InputPipeline(s,store,bridge,[])
                program = LiveProgram(pipeline,root)
                def play(count):
                    until = time.monotonic()+5
                    while program.completed < count and time.monotonic()<until:
                        stage,_ = program.next_frame()
                        program.encoded(stage)
                        time.sleep(.002)
                    self.assertEqual(program.completed,count,program.last_error)
                try:
                    ray = bridge.controller.character
                    source_event = Comment('live-host',pipeline.inbox.session,'test','host','Host',
                                           'My sticker says COLOR42. Let us talk about constellations.',time.time())
                    pipeline.inbox.put(source_event)
                    play(1)
                    self.assertIsNotNone(ray.thread)
                    source_turn = ray.thread.turn
                    ray.last_activity -= 46
                    mic = program.mic.open(automatic=False)
                    program.next_check=0
                    program.next_frame()
                    self.assertIsNone(program.token)
                    self.assertIsNotNone(ray.thread)
                    program.mic.mute(mic)
                    with program.ingress:
                        program.next_check=0
                        program.next_frame()
                        self.assertIsNone(program.token)
                    program.next_check=0
                    play(2)
                    self.assertEqual(len(reply.calls),2)
                    opening,history = reply.calls[1]
                    self.assertIn('following up',opening)
                    self.assertEqual(history[-1],('model','I wonder how two stars can look close together.'))
                    self.assertNotIn('COLOR42',str(history))
                    ray.last_activity -= 46
                    program.next_check=0
                    program.next_frame()
                    self.assertIsNone(program.token)
                    self.assertIsNone(ray.thread)
                    with store.connect() as db:
                        calls=db.execute("""SELECT p.provider FROM provider_calls p JOIN comment_inbox i ON i.turn_id=p.turn_id
                            WHERE i.source='ray-initiative'""").fetchall()
                        self.assertCountEqual(calls,[('gemini',),('tts',)])
                        store.remove_source(db,source_event.receipt,time.time())
                    self.assertIsNone(store.followup_exchange(source_turn,source_event.viewer,s.mode))
                finally:
                    program.close()

    def test_human_arrivals_and_source_removal_take_precedence_over_an_unheard_followup(self):
        from ravora.ray_character import public_fixture
        for arrival in ('already_queued','while_generating','host_microphone','deleted_source','deleted_while_generating'):
            with self.subTest(arrival=arrival),tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                s = replace(ProviderSettings(),comment_source='youtube',memory_enabled=False)
                store=MemoryStore(root/'state.db')
                waiting,release = threading.Event(),threading.Event()
                class Reply:
                    def stream(self,text,history):
                        if 'Ray is following up' in text:
                            waiting.set()
                            release.wait(3)
                        yield public_fixture('I wonder what makes two notes fit together.')
                    stream.ray_public_output=True
                bridge=ConversationBridge(s,store,root,TurnFence(),threading.RLock(),providers=(Reply(),Reaction(),Speech()))
                pipeline=InputPipeline(s,store,bridge,[])
                program=LiveProgram(pipeline,root)
                original=Comment('youtube','stream','one','one','A','I like jazz.',time.time())
                next_comment=Comment('youtube','stream','two','two','B','My turn.',time.time())
                try:
                    pipeline.inbox.put(original)
                    deadline=time.monotonic()+5
                    while program.completed<1 and time.monotonic()<deadline:
                        program.next_frame();time.sleep(.002)
                    ray=bridge.controller.character
                    self.assertIsNotNone(ray.thread)
                    ray.last_activity-=46
                    if arrival=='already_queued':
                        pipeline.inbox.put(next_comment)
                        program.next_check=0
                        program.next_frame()
                        self.assertEqual(bridge.viewer,next_comment.viewer)
                        self.assertEqual(ray.initiatives,0)
                        continue
                    if arrival=='deleted_source':
                        with store.connect() as db:
                            store.remove_source(db,original.receipt,time.time())
                        program.next_check=0
                        program.next_frame()
                        self.assertIsNone(program.token)
                        self.assertFalse(waiting.is_set())
                        continue
                    program.next_check=0
                    program.next_frame()
                    self.assertTrue(waiting.wait(2))
                    if arrival == 'host_microphone':
                        program.mic.open(automatic=False)
                    elif arrival == 'deleted_while_generating':
                        with store.connect() as db:
                            store.remove_source(db,original.receipt,time.time())
                        pipeline.check_removed()
                    else:
                        pipeline.inbox.put(next_comment)
                    stage,_=program.next_frame()
                    self.assertFalse(stage.speaker)
                    self.assertIsNone(program.token)
                    release.set()
                    bridge.worker.join(2)
                    if arrival == 'while_generating':
                        program.next_check=0
                        program.next_frame()
                        self.assertEqual(bridge.viewer,next_comment.viewer)
                    with store.connect() as db:
                        self.assertEqual(db.execute("SELECT status FROM comment_inbox WHERE source='ray-initiative'").fetchone(),('cancelled',))
                finally:
                    release.set()
                    program.close()

    def test_host_and_chat_share_one_playout_and_host_survives_reader_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            s = replace(ProviderSettings(), comment_source='youtube', memory_enabled=False,
                        character_initiative_enabled=False)
            bridge = ConversationBridge(s, MemoryStore(root/'live.db'), root, TurnFence(), threading.RLock(),
                                        providers=(Text(), Reaction(), Speech()))
            pipeline = InputPipeline(s, bridge.controller.store, bridge, [])
            program = LiveProgram(pipeline, root)
            try:
                inbox = pipeline.inbox
                viewer = Comment('youtube', 'stream', 'message', 'channel', 'Viewer', 'Hello!', time.time())
                inbox.put(viewer)
                key = uuid.uuid4().hex
                with patch.object(program.stt, 'transcribe', return_value={'text': 'Testing my microphone.', 'speech': True}) as stt:
                    self.assertEqual(program.submit_audio(key, microphone_wav())['status'], 'queued')
                    self.assertEqual(program.submit_audio(key, microphone_wav())['status'], 'queued')
                    stt.assert_called_once()
                from ravora.youtube import YouTubeReader
                pipeline.reader = YouTubeReader(s, inbox)
                pipeline.reader.status = 'failed'
                speakers = []
                dry_host = Clip('Host', 'Testing my microphone.', microphone_wav())
                host_mixer = LiveMicrophone()
                deadline = time.monotonic() + 5
                while program.completed < 2 and time.monotonic() < deadline:
                    stage, pcm = program.next_frame()
                    program.encoded(stage)
                    if stage.speaker == 'Host':
                        _, raw = dry_host.take()
                        self.assertEqual(pcm, host_mixer.mix(raw, host_playback=True))
                    if stage.speaker:
                        speakers.append(stage.speaker)
                    time.sleep(.003)
                self.assertEqual(speakers[0], 'Host')
                self.assertIn('Ray', speakers)
                self.assertEqual(program.completed, 2)
                self.assertEqual(inbox.summary(), {'played': 2})
                while program.frame < program.history_until:
                    stage, _ = program.next_frame()
                    self.assertTrue(stage.previous)
                    program.encoded(stage)
                self.assertEqual(program.next_frame()[0].previous, '')
            finally:
                program.close()

    def test_overlay_pins_only_selected_public_chat_and_releases_removed_messages(self):
        from ravora.comments import CommentInbox
        with tempfile.TemporaryDirectory() as directory:
            inbox = CommentInbox(MemoryStore(Path(directory)/'live.db'), capacity=3)
            now = time.time()
            messages = [Comment('youtube','stream',str(i),str(i),f'Viewer {i}',f'Message {i}',now)
                        for i in range(9)]
            for message in messages:
                inbox.put(message)
            inbox.put(Comment('live-host','stream','host','host','Host','private input',now))
            rows = inbox.display_comments(messages[0].receipt)
            self.assertEqual([r[2] for r in rows],['Message 0'] + [f'Message {i}' for i in range(4,9)])
            with inbox.store.connect() as db:
                inbox._remove(db,'youtube','stream','0')
            self.assertNotIn(messages[0].receipt,[r[0] for r in inbox.display_comments(messages[0].receipt)])

    def test_busy_chat_draws_reply_target_and_latest_below_topic(self):
        from itertools import groupby
        from ravora.live_overlay import ChatLine, Overlay

        overlay = Overlay()
        chat = tuple(ChatLine('Ray fan', text) for text in (
            '本棚を整理するときは、好きな色で並べたい？それとも新しく買った順番で並べたい？',
            'One', 'Two', 'Three', 'Four',
            'Would you spend a quiet afternoon building a model or painting a picture?',
        ))

        def tiles(stage):
            layer = overlay._chat(stage)
            # The middle column crosses every card, including its top and bottom edges.
            groups = groupby(range(layer.height), lambda y: bool(layer.getpixel((280, y))[3]))
            result = []
            for visible, rows in groups:
                if visible:
                    ys = list(rows)
                    result.append(layer.crop((0, ys[0], layer.width, ys[-1] + 1)).tobytes())
            return result

        for title in ('今日のお題', ''):
            for selected in (0, 3, 5, -1):
                with self.subTest(title=title, selected=selected):
                    stage = Stage(chat=chat, selected=selected, replying=True, prompt_title=title)
                    actual = tiles(stage)
                    newest = tiles(replace(stage, chat=chat[-1:], selected=0 if selected == 5 else -1))
                    self.assertTrue(actual[-1] == newest[-1], 'latest card is missing or clipped')
                    if title:
                        self.assertTrue(actual[0] == newest[0], 'topic banner missing')
                    if selected >= 0:
                        active = tiles(replace(stage, chat=(chat[selected],), selected=0))[-1]
                        self.assertTrue(active in actual, 'reply card or REPLYING badge missing')
                        if selected < 5:
                            self.assertLess(actual.index(active), len(actual)-1)

    def test_slot_topic_reaches_the_rendered_program_frame(self):
        from datetime import datetime
        from ravora.live_overlay import Overlay
        from ravora.twitch_schedule import JST, Slot

        start = int(datetime(2026, 11, 28, 23, tzinfo=JST).timestamp())
        slot = Slot(start, 'ja-23')
        self.assertEqual(slot.prompt_title, '日常の道具に1つだけ新機能を足すなら？')
        morning = int(datetime(2026, 11, 28, 7, tzinfo=JST).timestamp())
        self.assertEqual(Slot(morning, 'en-07').prompt_title, 'Upgrade one everyday object?')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = replace(ProviderSettings(), character_initiative_enabled=False)
            store = MemoryStore(root/'studio.db')
            bridge = ConversationBridge(settings, store, root, TurnFence(), threading.RLock(),
                                        providers=(Text(), Reaction(), Speech()))
            program = LiveProgram(InputPipeline(settings, store, bridge, []), root,
                                  initiative_language=slot.language, prompt_title=slot.prompt_title)
            try:
                stage, _ = program.next_frame()
                self.assertEqual((stage.prompt_title, stage.language), (slot.prompt_title, 'ja'))
                overlay = Overlay()
                def banner(view):
                    return overlay.compose(view).crop((64, 181, 625, 245)).tobytes()
                japanese_frame = banner(stage)
                self.assertNotEqual(banner(replace(stage, language='en')), japanese_frame)
                self.assertNotEqual(banner(replace(stage, prompt_title='')), japanese_frame)
            finally:
                program.close()

    def test_adaptive_pages_and_word_emphasis_follow_pcm_positions(self):
        clip = Clip('Ray', 'early late', microphone_wav(), word_starts=[.1, .3])
        active = []
        while not clip.done:
            caption, _ = clip.take()
            active.append(caption[slice(*clip.highlight)])
        self.assertEqual(active, [''] * 3 + ['early'] * 6 + ['late'] * 6)
        long = Clip('Ray','今日は音声を確認しています。'*30,pcm=SILENCE*90)
        self.assertGreater(len(long.pages),1)
        self.assertEqual(''.join(long.pages).replace('\n',''),'今日は音声を確認しています。'*30)
        self.assertTrue(all(len(page.splitlines())<=5 for page in long.pages))
        shown = []
        while not long.done:
            caption, _ = long.take()
            if not shown or caption != shown[-1]:
                shown.append(caption)
        self.assertEqual(tuple(shown), long.pages)
        from ravora.live_overlay import CAPTION_WIDTH, caption_layout, font
        mixed = 'Rayの「星図」を見てみよう。' + 'constellation'*40 + 'って呼ぶんだ。'
        (size, cjk), pages = caption_layout(mixed)
        rows = [row for page in pages for row in page.splitlines()]
        self.assertEqual(''.join(rows), mixed)
        self.assertTrue(all(font(size, True, cjk).getlength(row) <= CAPTION_WIDTH for row in rows))

    def test_long_reply_fits_one_readable_page_during_speech_and_after_playout(self):
        from PIL import ImageDraw
        from ravora.live_media import SpeechPrefix
        from ravora.live_overlay import CAPTION_WIDTH, Overlay, font

        jp = '私は星座を見ていると、同じ星の並びが船にも鳥にも見えるのが面白い。人によって見つける形が違うでしょう。線を一本引き直すだけで、新しい物語が始まりそうなところが好きなんだ。'
        en = 'I keep seeing a little ship between those stars. Turn the map sideways and the deck becomes a wing. I wonder whether two people could draw the same sky and tell completely different stories about it.'
        overlay = Overlay()
        for text in ('短い返事。', jp, en):
            with self.subTest(text=text[:8]):
                prefix = SpeechPrefix(text)
                self.assertTrue(prefix.single_page)
                style, (page,) = prefix.layout
                self.assertEqual(style[0] == 38, text == '短い返事。')
                face = font(style[0], True, style[1])
                rows = page.splitlines()
                self.assertTrue(all(face.getlength(line) <= CAPTION_WIDTH for line in rows))
                self.assertEqual(('' if style[1] else ' ').join(rows), text)
                for stage in (Stage('Ray', page, caption_font=style), Stage(previous=page, caption_font=style)):
                    with patch.object(ImageDraw.ImageDraw, 'text', autospec=True) as draw:
                        overlay._subtitles(stage)
                    self.assertEqual([call.args[2] for call in draw.call_args_list], ['RAY', *rows])

    def test_live_microphone_mute_discards_queue_and_fences_late_packets(self):
        now = [4.0]
        mic = LiveMicrophone(clock=lambda: now[0])
        old = mic.open()
        with self.assertRaisesRegex(RequestError, 'MIC_ALREADY_OPEN'):
            mic.open()
        packet = (b'\x01\x02' * (PACKET_BYTES // 2))
        now[0] += .1
        mic.push(old, 0, packet)
        mic.mute(old)
        with self.assertRaisesRegex(RequestError, 'MIC_SESSION_EXPIRED'):
            mic.push(old, 1, packet)
        new = mic.open()
        mic.mute(old)
        now[0] += .1
        self.assertEqual(mic.mix(SILENCE), SILENCE)
        mic.push(new, 0, packet)
        mic.push(new, 0, packet)
        now[0] += .04
        self.assertEqual(mic.mix(SILENCE), packet[:len(SILENCE)])
        now[0] += .26
        self.assertEqual(mic.mix(SILENCE), SILENCE)
        now[0] += 1
        with self.assertRaisesRegex(RequestError, 'MIC_SESSION_EXPIRED'):
            mic.push(new, 1, packet)

    def test_host_console_volume_is_authenticated_and_changes_only_host_mix(self):
        import json
        from urllib.error import HTTPError
        from urllib.request import Request, urlopen
        from ravora.developer_http import DeveloperHTTP
        from ravora.live_http import LiveHandler
        from ravora.live_bgm import LoopingMusic

        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root = Path(directory)
            settings = replace(ProviderSettings(), memory_enabled=False, character_initiative_enabled=False)
            store = MemoryStore(root/'live.db')
            bridge = ConversationBridge(settings, store, root, TurnFence(), threading.RLock(),
                                        providers=(Text(), Reaction(), Speech()))
            music = LoopingMusic([root/'test.mp3'])
            stack.callback(music.close)
            program = LiveProgram(InputPipeline(settings, store, bridge, []), root, host_trim_db=-3, bgm=music)
            stack.callback(program.close)
            server = stack.enter_context(DeveloperHTTP(program, 'test-host', 0, handler=LiveHandler))
            worker = threading.Thread(target=server.serve_forever)
            worker.start()
            stack.callback(worker.join)
            stack.callback(server.shutdown)
            origin = f'http://127.0.0.1:{server.server_port}'
            headers = {'Authorization':'Bearer test-host', 'Content-Type':'application/json'}
            def set_volume(db, request_headers, bus='mic'):
                key = 'host_trim_db' if bus=='mic' else 'volume_db'
                request = Request(origin+'/v1/host/'+bus+'/volume', data=json.dumps({key:db}).encode(),
                                  headers=request_headers)
                with urlopen(request, timeout=2) as response:
                    return json.load(response)

            pcm = (np.ones(len(SILENCE)//2, dtype='<i2') * 10000).tobytes()
            before = np.frombuffer(program.mic.mix(pcm, host_playback=True), dtype='<i2')
            for denied, code in [({}, 401), ({**headers, 'Origin':'https://example.invalid'}, 403)]:
                for bus in ('mic','bgm'):
                    with self.assertRaises(HTTPError) as error:
                        set_volume(-12, denied, bus)
                    self.assertEqual(error.exception.code, code)
                    error.exception.close()
            with self.assertRaises(HTTPError) as error:
                set_volume(-61, headers, 'bgm')
            self.assertEqual(error.exception.code,400)
            error.exception.close()
            self.assertEqual(set_volume(-60, headers, 'bgm'), {'volume_db':-60})
            self.assertEqual(set_volume(-9, headers), {'host_trim_db':-9})
            with self.assertRaises(HTTPError) as error:
                set_volume(9, headers)
            self.assertEqual(error.exception.code, 400)
            error.exception.close()
            with urlopen(Request(origin+'/v1/status', headers=headers), timeout=2) as response:
                status=json.load(response)
                self.assertEqual(status['host_trim_db'], -9)
                self.assertEqual(status['bgm']['volume_db'], -60)
            fade = np.frombuffer(program.mic.mix(pcm, host_playback=True), dtype='<i2')
            after = np.frombuffer(program.mic.mix(pcm, host_playback=True), dtype='<i2')
            self.assertAlmostEqual(20*np.log10(float(after[-1])/before[-1]), -6, delta=.01)
            self.assertGreater(fade[0], fade[-1])
            self.assertEqual(fade[-1], after[0])
            self.assertEqual(program.mic.mix(pcm, ray_speaking=True), pcm)
            owner = program.mic.open()
            program.mic.mute(owner)
            self.assertEqual(program.status()['host_trim_db'], -9)

    def test_aac_contains_host_and_ray_together_then_only_ray_after_mute(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'program.flv'
            writer = ProgramWriter(path, Scene())
            now = [0.]
            mic = LiveMicrophone(clock=lambda: now[0])
            owner = mic.open()
            def tone(hz, count):
                return (10000 * np.sin(np.arange(count) * (2*np.pi*hz/48000))).astype('<i2').tobytes()
            ray = tone(600, len(SILENCE)//2)
            host = tone(1800, PACKET_BYTES//2)
            try:
                for i in range(45):
                    now[0] = i / FPS
                    if 3 <= i < 21 and i % 3 == 0:
                        mic.push(owner, i//3-1, host)
                    if i == 21:
                        mic.mute(owner)
                    writer.write(Stage('Ray', 'Hello!'), mic.mix(ray))
            finally:
                writer.close()
            with av.open(str(path)) as source:
                audio = np.concatenate([f.to_ndarray()[0] for f in source.decode(audio=0)])
            def amplitude(segment, hz):
                return abs(np.mean(segment * np.exp(-2j*np.pi*hz*np.arange(len(segment))/48000))) * 2
            overlap, muted = audio[18000:27600], audio[42000:51600]
            for segment in (overlap, muted):
                self.assertGreater(amplitude(segment, 600), .10)
            self.assertGreater(amplitude(overlap, 1800), .10)
            self.assertLess(amplitude(muted, 1800), .002)

    def test_program_gain_tracks_ray_for_quiet_and_loud_host_without_changing_stt(self):
        from unittest.mock import Mock
        frames = len(SILENCE) // 2
        phase = np.sin(np.arange(frames) * (2*np.pi*600/48000))
        def tone(db):
            return np.rint(32768 * np.sqrt(2) * 10**(db/20) * phase).astype('<i2').tobytes()
        def level(pcm):
            samples = np.frombuffer(pcm, dtype='<i2').astype(float) / 32768
            return 10*np.log10(np.mean(samples*samples))

        for ray_db in (-21, -14):
            with self.subTest(ray_db=ray_db):
                now = [0.]
                stt = Mock()
                mic = LiveMicrophone(clock=lambda:now[0], handsfree=stt, host_trim_db=-3)
                ray = tone(ray_db)
                for _ in range(120):
                    self.assertEqual(mic.mix(ray, ray_speaking=True), ray)
                owner = mic.open(automatic=True)
                seq = 0
                for host_db in (-31, -6, -55):
                    raw = tone(host_db)
                    for _ in range(24):
                        now[0] = (seq + 1) / 10
                        mic.push(owner, seq, raw*3)
                        stt.feed.assert_called_with(raw*3, gap=False)
                        for i in range(3):
                            now[0] = (seq + 1)/10 + (i+1)/FPS + 1e-8
                            output = mic.mix(SILENCE)
                            self.assertLessEqual(max(abs(np.frombuffer(output,dtype='<i2').astype(int))), 27571)
                        seq += 1
                    reference = host_db if host_db == -55 else ray_db
                    self.assertAlmostEqual(level(output), reference - 3, delta=1.)

    def test_broadcast_breath_reduction_keeps_raw_stt_quiet_voice_and_ray_overlap(self):
        from unittest.mock import Mock
        size = len(SILENCE) // 2
        rng = np.random.default_rng(734)
        # Soft, band-limited noise, followed by a quiet voiced interjection and its final consonant.
        noise = np.convolve(rng.normal(size=size*3 + 4), np.ones(5)/5, mode='valid')
        breath = np.rint(noise * 340 / np.std(noise)).astype('<i2').tobytes()
        phase = np.arange(3*size) * (2*np.pi*180/48000)
        voiced = np.rint(590*np.sin(phase) + 140*np.sin(phase*2)).astype('<i2').tobytes()
        ray = np.rint(32768*np.sqrt(2)*10**(-16/20)*np.sin(np.arange(size)*2*np.pi*600/48000)).astype('<i2')

        def capture(enabled, overlap):
            now, stt = [0.], Mock()
            mic = LiveMicrophone(clock=lambda:now[0], handsfree=stt, host_mic_cleanup=enabled)
            owner = mic.open(automatic=True)
            heard = []
            for seq, packet in enumerate([breath]*5 + [voiced, breath]):
                now[0] = (seq+1) / 10
                mic.push(owner, seq, packet)
                stt.feed.assert_called_with(packet, gap=False)
                for frame in range(3):
                    now[0] = (seq+1)/10 + (frame+1)/FPS + 1e-8
                    mix = mic.mix(ray.tobytes() if overlap else SILENCE, ray_speaking=overlap)
                    heard.append(np.frombuffer(mix, '<i2').astype(float))
            self.assertEqual(stt.feed.call_count, 7)
            return np.array(heard)

        clean, bypass = capture(True, True) - ray, capture(False, True) - ray
        def rms(x):
            return np.sqrt(np.mean(x*x))
        # AGC must not boost the steady breath back to the old level.
        self.assertLess(rms(clean[6:15]), .28*rms(bypass[6:15]))
        # During Ray playback the same short interjection remains immediately audible.
        self.assertGreater(rms(clean[15, :240]), 300.)
        self.assertGreater(rms(clean[15:18]), rms(np.frombuffer(voiced, '<i2').astype(float))*.95)
        # The two unvoiced frames immediately following speech retain their consonant energy.
        self.assertGreater(rms(clean[18:20]), 330.)
        # Ray alone contributes exactly its original samples (allow one PCM rounding unit).
        np.testing.assert_allclose(clean, capture(True, False), atol=1)

    def test_broadcast_suppresses_short_click_across_frame_edge_and_resets_on_gap(self):
        size = len(SILENCE) // 2
        samples = np.zeros(3 * size, dtype='<i2')
        pulse = np.rint(16000*np.hanning(80)*np.cos(np.arange(80)*.6)).astype('<i2')
        samples[size-40:size+40] = pulse
        now = [0.]
        mic = LiveMicrophone(clock=lambda:now[0])
        owner = mic.open()
        now[0] = .1
        mic.push(owner, 0, samples.tobytes())
        now[0] += .04
        aired = np.frombuffer(b''.join(mic.mix(SILENCE) for _ in range(3)), '<i2')
        self.assertGreater(np.max(np.abs(aired)), 0)
        self.assertLess(np.max(np.abs(aired)), .1*np.max(np.abs(pulse)))

        quiet = np.random.default_rng(93).integers(-500, 501, 3*size, dtype='<i2').tobytes()
        now[0] = .2
        mic.push(owner, 1, quiet)
        now[0] += .04
        first = mic.mix(SILENCE)
        for _ in range(2):
            mic.mix(SILENCE)
        now[0] = .4
        mic.push(owner, 3, quiet)
        now[0] += .04
        fresh = LiveMicrophone()
        reference = fresh.mix(quiet[:len(SILENCE)], host_playback=True)
        self.assertEqual(mic.mix(SILENCE), reference)
        self.assertEqual(first, reference)

    def test_recorded_host_clip_uses_same_gain_and_resets_between_mic_sessions(self):
        mic = LiveMicrophone()
        owner = mic.open()
        samples = np.rint(1200*np.sin(np.arange(len(SILENCE)//2)*.1)).astype('<i2')
        pcm = samples.tobytes()
        first = mic.mix(pcm, host_playback=True)
        for _ in range(60):
            output = mic.mix(pcm, host_playback=True)
        self.assertGreater(np.max(np.frombuffer(output, dtype='<i2')), 3*np.max(samples))
        mic.mute(owner)
        mic.open()
        self.assertEqual(mic.mix(pcm, host_playback=True), first)

    def test_on_air_question_is_transcribed_once_without_delayed_host_playback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            s = replace(ProviderSettings(), memory_enabled=False, character_initiative_enabled=False)
            bridge = ConversationBridge(s, MemoryStore(root/'live.db'), root, TurnFence(), threading.RLock(),
                                        providers=(Text(), Reaction(), Speech()))
            program = LiveProgram(InputPipeline(s, bridge.controller.store, bridge, []), root)
            try:
                key = uuid.uuid4().hex
                with patch.object(program.stt, 'transcribe', return_value={'text':'Hello there.', 'speech':True}) as stt:
                    for _ in range(2):
                        program.submit_audio(key, microphone_wav(), already_aired=True)
                    stt.assert_called_once()
                speakers = set()
                deadline = time.monotonic() + 5
                while not program.completed and time.monotonic() < deadline:
                    stage, _ = program.next_frame()
                    self.assertEqual(stage.host_caption, 'Hello there.')
                    speakers.add(stage.speaker)
                    program.encoded(stage)
                    time.sleep(.003)
                self.assertEqual(program.completed, 1)
                self.assertIn('Ray', speakers)
                self.assertNotIn('Host', speakers)
                text = '自分で星座を描くなら、どの明るい星を目印にするか、一緒に考えてみたい。' * 8
                with patch.object(program.stt, 'transcribe', return_value={'text':text, 'speech':True}):
                    program.submit_audio(uuid.uuid4().hex, microphone_wav(), already_aired=True)
                with patch.object(program, '_turn_frame', return_value=(Stage('Ray', 'いいね。'), SILENCE)):
                    pages = []
                    while page := program.next_frame()[0].host_caption:
                        self.assertLessEqual(len(page.splitlines()), 3)
                        pages.append(page)
                        program.frame = program.host_caption_until
                    self.assertGreater(len(pages), 1)
                    self.assertEqual(''.join(pages).replace('\n',''), text)
            finally:
                program.close()

    def test_removed_turn_drops_caption_and_unplayed_chunks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            s = replace(ProviderSettings(), memory_enabled=False, character_initiative_enabled=False)
            bridge = ConversationBridge(s, MemoryStore(root/'live.db'), root, TurnFence(), threading.RLock(),
                                        providers=(Text(), Reaction(), Speech()))
            pipeline = InputPipeline(s, bridge.controller.store, bridge, [])
            program = LiveProgram(pipeline, root)
            try:
                inbox = pipeline.inbox
                inbox.put(Comment('youtube', 'chat', 'message', 'channel', 'Viewer', 'Hello!', time.time()))
                program.next_frame()
                bridge.worker.join(3)
                self.assertEqual(program.next_frame()[0].speaker, 'Ray')
                with inbox.store.connect() as db:
                    inbox._remove(db, 'youtube', 'chat', 'message')
                pipeline.check_removed()
                self.assertEqual(program.next_frame(), (Stage(), SILENCE))
                self.assertIsNone(program.monitor)
                self.assertEqual(program.completed, 0)
            finally:
                program.close()

    def test_twitch_viewer_language_controls_speech_and_caption_without_switching_host_tab(self):
        import json

        class Reply:
            def __init__(self, speech):
                self.speech = speech

            def stream(self, *_args, **_kwargs):
                yield json.dumps({'public_output': {
                    'speech': self.speech, 'expression': 'neutral', 'motion': 'none'}})
            stream.ray_public_output = True

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = replace(ProviderSettings(), comment_source='twitch', memory_enabled=False,
                               character_enabled=True, character_initiative_enabled=False)
            lines = {'en': 'Good morning.', 'ja': '\u304a\u306f\u3088\u3046\u3002'}
            voices = {locale: Speech() for locale in lines}
            profiles = {locale: (replace(settings, language=locale + ('-US' if locale == 'en' else '-JP')),
                                 Reply(line), voices[locale]) for locale, line in lines.items()}
            s, text, speech = profiles['ja']
            bridge = ConversationBridge(s, MemoryStore(root/'live.db'), root, TurnFence(), threading.RLock(),
                                        providers=(text, Reaction(), speech))
            pipeline = InputPipeline(s, bridge.controller.store, bridge, [])
            program = LiveProgram(pipeline, root, host_language='ja', reply_profiles=profiles)
            try:
                for locale, comment_text in [('en', 'Hello, Ray!'), ('ja', '\u3053\u3093\u306b\u3061\u306f')]:
                    message = Comment('twitch', 'test-channel', locale, locale, 'Viewer', comment_text, time.time())
                    self.assertEqual(pipeline.inbox.put(message), 'queued')
                    caption = None
                    completed = program.completed
                    deadline = time.monotonic() + 3
                    while program.completed == completed and time.monotonic() < deadline:
                        stage, _ = program.next_frame()
                        program.encoded(stage)
                        if stage.speaker == 'Ray':
                            caption = stage.caption
                            self.assertEqual(stage.chat[stage.selected].text, comment_text)
                        time.sleep(.003)
                    self.assertEqual(program.completed, completed + 1, program.last_error)
                    self.assertEqual(voices[locale].calls, [lines[locale]])
                    self.assertEqual(caption, lines[locale])
                    self.assertEqual(program.conversation_language, 'ja')
                self.assertEqual(pipeline.inbox.summary(), {'played': 2})
            finally:
                program.close()
