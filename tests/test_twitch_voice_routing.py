from dataclasses import replace
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock
import wave

from ravora.aivis_voice import KanonSpeech, KANON_STYLE
from ravora.conversation import MemoryStore, SpeechController
from ravora.live_voice import live_reply_profiles
from ravora.providers import GeminiText, ProviderError
from ravora.ray_character import CharacterError, LANGUAGE_OUTPUT_SCHEMA, PublicDecoder
from ravora.settings import ProviderSettings, SettingsError
from support import HTTP, Reaction, Speech


def envelope(language, score, speech):
    return dict(input_language=language, language_confidence=score,
                public_output=dict(speech=speech, expression='joy', motion='none'))


class RouteTests(unittest.TestCase):
    def test_threshold_and_ledger_through_structured_turn(self):
        cases = [
            ('ja', .80, 'en', '今日は楽しみだね。', 'ja', 'model'),
            ('en', .99, 'ja', 'Good to see you.', 'en', 'model'),
            ('en', .79, 'ja', 'Rayと遊ぼう。', 'ja', 'fallback'),
            ('und', .98, 'en', 'Hello again!', 'en', 'fallback'),
            ('ja', 1, 'en', 'Rayはパズルが好き。', 'ja', 'model'),
        ]
        for detected, score, initial, speech, expected, reason in cases:
            with self.subTest(detected=detected, score=score), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                settings = replace(ProviderSettings(), mode='cloud', api_enabled=True, memory_enabled=False,
                                   character_enabled=True, language=initial + ('-JP' if initial=='ja' else '-US'))
                store = MemoryStore(root/'db')
                voices = {key: Speech() for key in ('ja','en')}
                for key, adapter in voices.items():
                    adapter.voice = key+'-test'
                    adapter.local = key=='ja'
                entry = json.dumps(envelope(detected, score, speech))
                reply = {'candidates':[{'content':{'parts':[{'text':entry}]}, 'finishReason':'STOP'}]}
                http = HTTP(stream=('data: '+json.dumps(reply)+'\n\n').encode())
                controller = SpeechController(settings,store,root,providers=(GeminiText(settings,http),Reaction(),voices[initial]))
                controller.language_voices = voices
                events = []
                try:
                    metrics = controller.run('Qwen3?', 'viewer', 'route', events.append, threading.Event())
                finally:
                    controller.memory_worker.close()
                self.assertEqual(events[-1]['kind'],'complete', [e.get('code') for e in events if 'code' in e])
                self.assertEqual(http.calls[0][2]['generationConfig']['responseJsonSchema'],LANGUAGE_OUTPUT_SCHEMA)
                self.assertEqual(metrics['tts_route']['language'],expected)
                self.assertEqual(metrics['tts_route']['decision'],reason)
                self.assertEqual(voices[expected].calls,[speech])
                self.assertEqual(voices['ja' if expected=='en' else 'en'].calls,[])
                with store.connect() as db:
                    self.assertEqual(db.execute("SELECT mode,model,status,attempted FROM api_usage WHERE provider='tts'").fetchone(),
                                     ('local' if expected=='ja' else 'cloud',expected+'-test','accepted',1))
                    self.assertEqual(db.execute('SELECT count(*) FROM speech_usage').fetchone()[0],int(expected=='en'))

    def test_invalid_classification_remains_private(self):
        for language, score in [('other',.9),('ja',True),('en',1.1),('en',-.1)]:
            decoder=PublicDecoder(structured=True,language_routing=True)
            self.assertEqual(decoder.feed(json.dumps(envelope(language,score,'Hello.'))),[])
            with self.assertRaises(CharacterError):
                decoder.feed('',final=True)
            self.assertIsNone(decoder.language_result)

    def test_twitch_profile_and_rollback(self):
        settings = ProviderSettings()
        self.assertIsInstance(live_reply_profiles(settings,twitch=True)['ja'][2],KanonSpeech)
        self.assertFalse(isinstance(live_reply_profiles(settings)['ja'][2],KanonSpeech))
        self.assertEqual(live_reply_profiles(settings,twitch=True)['en'][2].voice,'en-US-Kokoro-Ray-Kanon-v1')
        self.assertEqual(live_reply_profiles(replace(settings,twitch_ja_tts='google'),twitch=True)['ja'][2].voice,'ja-JP-Chirp3-HD-Zephyr')
        self.assertEqual(live_reply_profiles(replace(settings,voice_profile='legacy',twitch_en_tts='google'),twitch=True)['en'][2].voice,settings.voice)
        for score in [float('nan'),0,1.1]:
            with self.assertRaises(SettingsError):replace(settings,tts_language_confidence=score).validate()

    def test_local_pcm_released_only_after_valid_wave(self):
        stream=io.BytesIO()
        with wave.open(stream,'wb') as f:
            f.setnchannels(1);f.setsampwidth(2);f.setframerate(24000);f.writeframes(b'\x11\0'*240)
        speech=KanonSpeech(ProviderSettings())
        with tempfile.TemporaryDirectory() as temp:
            target=Path(temp)/'reply.wav'
            chunks=[]
            speech._post=Mock(side_effect=[b'{}',stream.getvalue()[:-2]])
            with self.assertRaises(ProviderError):speech.create('こんにちは。',target,on_pcm=chunks.append)
            self.assertFalse(target.exists())
            self.assertEqual(chunks,[])
            speech._post=Mock(side_effect=[b'{}',stream.getvalue()])
            meta=speech.create('こんにちは。',target,on_pcm=chunks.append)
            self.assertEqual(meta['duration'],.01)
            self.assertEqual(speech._post.call_args_list[0].args[1]['speaker'],KANON_STYLE)
            self.assertEqual(chunks,[b'\x11\0'*240])
