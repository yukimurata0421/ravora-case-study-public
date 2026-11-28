from importlib.util import find_spec
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
import wave

from ravora.aivis_voice import KanonSpeech
from ravora.kokoro_voice import CONTINUATION_LIMIT, KokoroSpeech, continuation_parts
from ravora.providers import ProviderError
from ravora.spoken_language import japanese_reply_parts
from ravora.settings import ProviderSettings


class ViewerVoiceLatencyTests(unittest.TestCase):
    def test_only_viewer_opening_shortens_existing_engine_padding(self):
        audio = io.BytesIO()
        with wave.open(audio, 'wb') as wav:
            wav.setparams((1, 2, 24000, 0, 'NONE', ''))
            wav.writeframes(b'\x11\x00'*240)
        with tempfile.TemporaryDirectory() as tmp:
            voice = KanonSpeech(ProviderSettings())
            for viewer, sink, initial, expected in [(True, Mock(), .1, .01), (True, None, .1, .1),
                                                   (False, Mock(), .1, .1), (True, Mock(), .003, .003)]:
                with self.subTest(viewer=viewer, sink=bool(sink), initial=initial):
                    voice.viewer_reply = viewer
                    voice._post = Mock(side_effect=[json.dumps({'prePhonemeLength': initial, 'postPhonemeLength': .1}).encode(), audio.getvalue()])
                    voice.create('朝は落ち着いた音楽が好き。',Path(tmp)/'reply.wav',on_pcm=sink)
                    body = json.loads(voice._post.call_args_list[1].args[2])
                    self.assertEqual(body['prePhonemeLength'],expected)
                    self.assertEqual(body['postPhonemeLength'],.1)

    @unittest.skipUnless(find_spec("numpy") and find_spec("PIL"), "studio caption layout required")
    def test_long_head_and_tail_fit_separate_gpu_requests_with_ordered_complete_wav(self):
        samples = (
            'Hot green tea and warm rice crackers make a steady pair. The dry crunch of the cracker cuts right through the bitter steam of the tea.',
            'Everybody enjoys meeting different people sharing wonderful experiences together.',
        )
        with tempfile.TemporaryDirectory() as tmp:
            voice = KokoroSpeech(ProviderSettings())
            voice.viewer_reply = True
            for text in samples:
                with self.subTest(text=text):
                    parts,preview=[],[]
                    def synth(part):
                        parts.append(part)
                        return bytes((len(parts),0))*20
                    voice._pcm=synth
                    output=Path(tmp)/'reply.wav'
                    voice.create(text,output,on_pcm=preview.append)
                    self.assertEqual(' '.join(parts),text)
                    self.assertLessEqual(max(map(len,parts)),CONTINUATION_LIMIT)
                    with wave.open(str(output)) as wav:
                        self.assertEqual(wav.readframes(wav.getnframes()),b''.join(bytes((i+1,0))*20 for i in range(len(parts))))
                    self.assertEqual(preview,[bytes((i+1,0))*20 for i in range(len(parts)-1)])
        self.assertEqual(list(continuation_parts('x'*70+' short words')),['x'*70,'short words'])

    def test_503_splits_only_the_failed_part_once_and_preserves_complete_audio(self):
        text = 'That sounds like a plan! We can bring everyone together for a game later tonight.'
        for right_ok in (True, False):
            voice = KokoroSpeech(ProviderSettings())
            voice.viewer_reply = True
            error = ProviderError('LOCAL_TTS_HTTP_503')
            voice._pcm = Mock(side_effect=[b'A0'*10, error, b'B0'*20, b'C0'*30 if right_ok else error])
            packets = []
            with self.subTest(right_ok=right_ok), tempfile.TemporaryDirectory() as tmp:
                output = Path(tmp)/'reply.wav'
                if right_ok:
                    voice.create(text,output,on_pcm=packets.append)
                    with wave.open(str(output)) as wav:
                        self.assertEqual(wav.readframes(wav.getnframes()),b'A0'*10 + b'B0'*20 + b'C0'*30)
                else:
                    with self.assertRaisesRegex(ProviderError,'LOCAL_TTS_HTTP_503'):
                        voice.create(text,output,on_pcm=packets.append)
                    self.assertFalse(output.exists())
                calls = [c.args[0] for c in voice._pcm.call_args_list]
                self.assertEqual(len(calls),4)
                self.assertEqual(' '.join(calls[:2]),text)
                self.assertEqual(' '.join(calls[2:]),calls[1])
                self.assertEqual(packets,[b'A0'*10])

    def test_unsplittable_503_and_unknown_status_are_terminal(self):
        for text,code in [('Blue.','LOCAL_TTS_HTTP_503'),('Two words','LOCAL_TTS_HTTP_500'),
                          ('Two words','LOCAL_TTS_CONNECTION_FAILED')]:
            voice = KokoroSpeech(ProviderSettings())
            voice._pcm = Mock(side_effect=ProviderError(code))
            with self.assertRaisesRegex(ProviderError,code):voice._render_part(text)
            voice._pcm.assert_called_once_with(text)

    def test_japanese_coordinated_facts_keep_order_and_quotations(self):
        self.assertEqual(japanese_reply_parts('幅は八十ミリで、重さは一キロだよ。'),
                         ['幅は八十ミリで、','重さは一キロだよ。'])
        for text in ('朝の配信なら、静かな曲がいいよ。','「幅は八十ミリで、重さは一キロ」と言ったよ。',
                     '色は緑で、落ち着いた気持ちになる。'):
            self.assertEqual(japanese_reply_parts(text),[text])
