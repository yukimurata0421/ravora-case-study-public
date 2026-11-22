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

    def test_host_only_cannot_start_a_broadcast(self):
        import subprocess
        for flag in ('--publish','--auto-broadcast'):
            result = subprocess.run([sys.executable, '-m', 'ravora.youtube_live', '--host-only', flag, '--check'],
                                    capture_output=True, text=True, timeout=15)
            self.assertNotEqual(result.returncode,0)
            self.assertIn('HOST_ONLY_CANNOT_PUBLISH',result.stderr)

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
                deadline = time.monotonic() + 5
                while program.completed < 2 and time.monotonic() < deadline:
                    stage, pcm = program.next_frame()
                    program.encoded(stage)
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

    def test_two_line_pages_and_word_emphasis_follow_pcm_positions(self):
        clip = Clip('Ray', 'early late', microphone_wav(), word_starts=[.1, .3])
        active = []
        while not clip.done:
            caption, _ = clip.take()
            active.append(caption[slice(*clip.highlight)])
        self.assertEqual(active, [''] * 3 + ['early'] * 6 + ['late'] * 6)
        long = Clip('Ray','今日は音声を確認しています。'*30,microphone_wav())
        self.assertGreater(len(long.pages),1)
        self.assertEqual(''.join(long.pages).replace('\n',''),'今日は音声を確認しています。'*30)
        self.assertTrue(all(len(page.splitlines())<=2 for page in long.pages))

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
                mic = LiveMicrophone(clock=lambda:now[0], handsfree=stt)
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
                    self.assertAlmostEqual(level(output), host_db if host_db == -55 else ray_db, delta=1.)

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
