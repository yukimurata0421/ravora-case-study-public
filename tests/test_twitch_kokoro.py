from importlib.util import find_spec
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ravora.kokoro_voice import KokoroSpeech, reply_lead, request_parts
from ravora.live_voice import live_reply_profiles
from ravora.providers import ProviderError
from ravora.settings import ProviderSettings
from ravora.twitch_voice import profile_live_turn


LONG = 'That sounds like a plan! We can bring everyone together for a game later tonight.'


class KokoroTests(unittest.TestCase):
    def test_natural_boundaries_keep_words_and_titles(self):
        self.assertEqual(reply_lead(LONG), 'That sounds like a plan!')
        self.assertEqual(reply_lead('That sounds like a plan!'), 'That sounds like a plan!')
        plain = 'Everybody enjoys meeting different people sharing wonderful experiences together.'
        self.assertEqual(reply_lead(plain), plain)
        title = 'I visited my teacher Dr. Smith yesterday, and he showed me a new painting.'
        self.assertFalse(reply_lead(title).endswith('Dr.'))
        descriptive = 'I would start with a glowing window seen through thick fog and listen to the quiet rain outside.'
        self.assertEqual(reply_lead(descriptive), 'I would start with a glowing window')
        parts = list(request_parts('Alpha beta '*69+'finish.'))
        self.assertTrue(all(0 < len(p) <= 500 for p in parts))
        self.assertEqual(' '.join(parts), ('Alpha beta '*69+'finish.').strip())
        with self.assertRaises(ProviderError): list(request_parts('x'*501))

    @unittest.skipUnless(all(find_spec(pkg) for pkg in ("av", "numpy", "PIL")), "studio extra required")
    def test_turn_specific_routing_prefix_and_join(self):
        profiles = live_reply_profiles(ProviderSettings(), twitch=True)
        program = SimpleNamespace(reply_profiles=profiles, initiative_language='en', _receive_pcm=Mock())
        dst = SimpleNamespace(controller=SimpleNamespace())
        for source, expected_count in [('twitch',2),('ray-initiative',1),('live-host',1)]:
            profile_live_turn(program, dst, dict(source=source, comment='Love this game!'), 'en')
            ctrl = dst.controller
            if source == 'twitch':
                self.assertIs(ctrl.language_voices['en'], ctrl.speech)
            voice = ctrl.speech
            first = b'\x19\x04'*12000
            tail = b'\x01\x02'*24000
            voice._pcm = Mock(side_effect=[first,tail])
            program._receive_pcm.reset_mock()
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp)/'audio.wav'
                voice.create(LONG,path,on_pcm=lambda pcm:ctrl.program_pcm('turn',LONG,pcm))
                self.assertEqual(voice._pcm.call_count,expected_count)
                program._receive_pcm.assert_called_once_with('turn',reply_lead(LONG) if expected_count==2 else LONG,first)
                with wave.open(str(path)) as w:
                    self.assertEqual(w.readframes(w.getnframes()),first+(tail if expected_count==2 else b''))
        self.assertFalse(profiles['en'][2].viewer_reply)

    def test_unknown_tail_failure_is_not_retried_or_published_as_a_complete_wave(self):
        voice = KokoroSpeech(ProviderSettings())
        voice.viewer_reply = True
        voice._pcm = Mock(side_effect=[b'\x01\x02'*24000,ProviderError('LOCAL_TTS_CONNECTION_FAILED')])
        sink = Mock()
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'out.wav'
            with self.assertRaises(ProviderError): voice.create(LONG,path,on_pcm=sink)
            self.assertFalse(path.exists())
        self.assertEqual(voice._pcm.call_count,2)
        self.assertEqual(sink.call_count,1)
        self.assertIsNone(voice.preview_caption)

    @unittest.skipUnless(all(find_spec(pkg) for pkg in ("av", "numpy", "PIL")), "studio extra required")
    def test_reload_preserves_admitted_voice_and_applies_to_new_turns(self):
        from threading import RLock
        from ravora.live_program import LiveProgram

        program = LiveProgram.__new__(LiveProgram)
        program.reply_profiles = live_reply_profiles(ProviderSettings(), twitch=True)
        program.pipeline = SimpleNamespace(s=SimpleNamespace(comment_source='twitch'))
        program.lock = RLock()
        program.observations = None
        program.policy_generation = 0
        branch = SimpleNamespace(controller=SimpleNamespace())
        program._profile(branch, dict(source='twitch', comment='Hello everyone!'), 'en')
        with patch.dict('os.environ', {'RAVORA_TWITCH_EN_TTS': 'google'}):
            program.restart_speech_policy()
        self.assertNotIsInstance(program.reply_profiles['en'][2], KokoroSpeech)
        self.assertIsInstance(branch.controller.speech, KokoroSpeech)
        self.assertTrue(branch.controller.speech.viewer_reply)

    @unittest.skipUnless(all(find_spec(pkg) for pkg in ("av", "numpy", "PIL")), "studio extra required")
    def test_long_tail_releases_middle_pcm_before_final_file(self):
        text = LONG + ' ' + LONG + ' ' + LONG + ' ' + LONG
        voice = KokoroSpeech(ProviderSettings())
        voice.viewer_reply = True
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)/'reply.wav'
            calls, previews = [], []
            def synth(part):
                calls.append(part)
                return bytes([len(calls),0])*12000
            def receive(pcm):
                self.assertFalse(output.exists())
                previews.append(pcm[0])
            voice._pcm = synth
            voice.create(text,output,on_pcm=receive)
            self.assertEqual(' '.join(calls),text)
            self.assertGreaterEqual(len(calls),3)
            self.assertEqual(previews,list(range(1,len(calls))))
            with wave.open(str(output)) as wav:
                self.assertEqual(wav.getnframes(),12000*len(calls))

    def test_unix_socket_warmup_checks_audio_and_selected_voice(self):
        import io
        import json
        import socketserver
        import threading
        from http.server import BaseHTTPRequestHandler
        from ravora.twitch_voice import warm_twitch_voices

        with tempfile.TemporaryDirectory() as tmp:
            socket_path = Path(tmp) / 'tts.sock'
            speech = KokoroSpeech(ProviderSettings(), socket_path)
            body = io.BytesIO()
            with wave.open(body, 'wb') as wav:
                wav.setparams((1, 2, 24000, 0, 'NONE', ''))
                wav.writeframes(b'\x00\x04' * 480)
            requests = []

            class Handler(BaseHTTPRequestHandler):
                def do_POST(self):
                    requests.append((self.path, json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
                    self.send_response(200)
                    self.send_header('X-Kokoro-Voice', self.server.style)
                    self.end_headers()
                    self.wfile.write(body.getvalue())

                def log_message(self, *args):
                    pass

            with socketserver.UnixStreamServer(str(socket_path), Handler) as server:
                server.style = speech.style
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                cache = patch('ravora.spoken_language.ASSETS', Path(tmp)/'cache')
                cache.start()
                self.addCleanup(cache.stop)
                try:
                    warm_twitch_voices({'en': (None, None, speech), 'ja': (None, None, object())})
                    self.assertEqual(requests[0], ('/synthesis', {'text': 'Ray ready. English TTS.', 'voice': speech.style}))
                    self.assertGreater(len(requests), 1)
                    server.style = 'other'
                    with self.assertRaisesRegex(ProviderError, 'KOKORO_VOICE_MISMATCH'):
                        warm_twitch_voices({'en': (None, None, speech), 'ja': (None, None, object())})
                finally:
                    server.shutdown()
                    thread.join()
