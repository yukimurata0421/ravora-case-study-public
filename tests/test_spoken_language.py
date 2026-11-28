import json
from dataclasses import replace
from pathlib import Path
import tempfile
import threading
import unittest

from ravora.conversation import MemoryStore, SpeechController
from ravora.providers import GeminiText, ProviderError
from ravora.settings import ProviderSettings
from ravora.spoken_language import FIXED, readable
from support import HTTP, Reaction, Speech
from test_twitch_voice_routing import envelope


class Replies(HTTP):
    def __init__(self, values):
        super().__init__()
        self.values = iter(values)

    def open(self, url, headers, payload):
        value = next(self.values)
        if isinstance(value, Exception):
            raise value
        reply = {'candidates':[{'content':{'parts':[{'text':json.dumps(value)}]},'finishReason':'STOP'}]}
        self.data = ('data: '+json.dumps(reply)+'\n\n').encode()
        return super().open(url, headers, payload)


class SpokenLanguageTests(unittest.TestCase):
    def run_turn(self, comment, replies):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            s = replace(ProviderSettings(), mode='cloud', api_enabled=True, memory_enabled=False,
                        character_enabled=True, monthly_reply_llm_usd=20, gemini_model='gemini-3.5-flash-lite')
            http = Replies(replies)
            store = MemoryStore(root/'db')
            voices = {lang:Speech() for lang in ('en','ja')}
            for lang, voice in voices.items():
                voice.voice,voice.local = lang+'-fixture', True
            controller = SpeechController(s,store,root,providers=(GeminiText(s,http),Reaction(),voices['en']))
            controller.language_voices=voices
            events=[]
            try:
                m=controller.run(comment,'viewer','private',events.append,threading.Event())
            finally:
                controller.memory_worker.close()
            self.assertEqual(events[-1]['kind'],'complete',events[-1].get('code'))
            with store.connect() as db:
                ledger=db.execute("SELECT p.state,p.attempted,json_extract(p.summary,'$.purpose') FROM provider_calls p WHERE p.provider='gemini' ORDER BY p.prepared").fetchall()
            self.assertEqual([e['text'] for e in events if e['kind']=='sentence'],voices['en'].calls+voices['ja'].calls)
            return m, voices, ledger, http.calls

    def test_rejects_wrong_tail_before_prefix_and_repairs_once(self):
        original='Right, I can tell you about that! '+('The opening is safe. '*4)+'そうみたいだね。'
        correct='Right, my English voice uses Kokoro.'
        m,v,ledger,calls=self.run_turn('Ah, not Qwen3!', [envelope('en',.95,original),envelope('ja',1,correct)])
        self.assertEqual(v['en'].calls,[correct]);self.assertEqual(v['ja'].calls,[])
        self.assertEqual(ledger,[('accepted',1,None),('accepted',1,'speech_language_repair')])
        self.assertEqual(m['speech_language_guard']['outcome'],'repaired')
        self.assertEqual(calls[1][2]['contents'][-1]['parts'][-1]['text'],'Ah, not Qwen3!')

    def test_russian_and_other_requested_languages_use_available_phrase(self):
        for comment, flagged in [('Можешь говорить по русски я не понимаю на других языках',False),
                                 ('Is it possible for us to keep talking in Italian instead?',True)]:
            r=envelope('und',.3,'Конечно, давай перейдем на русский.')
            if flagged:r['unsupported_language_requested']=True
            m,v,ledger,_=self.run_turn(comment,[r])
            self.assertEqual(v['en'].calls,[FIXED['available']['en']]);self.assertEqual(len(ledger),int(flagged))
            self.assertEqual(m['speech_language_guard']['outcome'],'available')

    def test_repair_failure_uses_safe_phrase_without_third_call(self):
        for failure in (envelope('en',1,'また失敗。'),ProviderError('CONNECTION_FAILED')):
            m,v,ledger,_=self.run_turn('Ah, not Qwen3!',[envelope('en',.95,'そうみたい。'),failure])
            self.assertEqual(v['en'].calls,[FIXED['clarify']['en']])
            self.assertEqual(len(ledger),2);self.assertEqual(m['speech_language_guard']['outcome'],'clarify')
            self.assertIn(ledger[-1][0],('failed','outcome_unknown'))

    def test_clean_turns_keep_one_generation(self):
        for lang,comment,text in [('en','What engine?','My voice is Kokoro-82M.'),('ja','英語音声は？','英語はKokoro-82Mだよ。')]:
            m,v,ledger,_=self.run_turn(comment,[envelope(lang,1,text)])
            self.assertEqual(v[lang].calls,[text]);self.assertEqual(len(ledger),1)
            self.assertNotIn('speech_language_guard',m)
        self.assertFalse(readable('Привет!','en'));self.assertFalse(readable('Hello there.','ja'))

    def test_clear_input_pins_first_request_and_voice(self):
        for lang, other, comment, answer, target in (
            ('en','ja','Ah, not Qwen3!','My voice is Kokoro.','US English'),
            ('ja','en','今日はどうだった？','楽しかったよ。','natural Japanese'),
        ):
            m,v,ledger,calls = self.run_turn(comment,[envelope(other,.99,answer)])
            self.assertEqual(len(ledger),1)
            self.assertIn('studio selected '+target,calls[0][2]['systemInstruction']['parts'][0]['text'])
            self.assertEqual(m['tts_route']['decision'],'local_clear')
            self.assertEqual(v[lang].calls,[answer])

    def test_ambiguous_inputs_preserve_original_routing(self):
        from ravora.spoken_language import clear_reply_language
        for comment in ('Qwen3?', 'The Baltic Crusades', 'The Who?', 'ひなた', '明日の予定', 'What is 花音?',
                        '英語で答えて', 'Can you reply in Russian?', 'Who is “Ray”?', 'Hola amigo',
                        'Что это?', 'I see @viewer', 'https://example.org/what-is-it'):
            self.assertIsNone(clear_reply_language(comment),comment)

    def test_one_known_name_is_spoken_without_regeneration(self):
        m,v,ledger,_ = self.run_turn('Ah, not Qwen3!',[envelope('en',1,'My voice is 花音.')])
        self.assertEqual(v['en'].calls,['My voice is Kanon.'])
        self.assertTrue(m['tts_name_normalized'])
        self.assertEqual(len(ledger),1)
        from ravora.spoken_language import normalize_voice_name
        for text in ('My voice is 花音です。','My voice is 未知.', '花音', 'My voice is 花音 and 花音.'):
            self.assertEqual(normalize_voice_name(text,'en'),text)

    def test_quotes_and_single_word_wrong_language(self):
        from ravora.spoken_language import explicit_unsupported_request
        for text in ('Я не хочу говорить по-русски.', 'Can you speak Russian or English?', 'Can you speak English?',
                     'Можешь говорить по русски или по японски?', 'Is “Can you speak Russian?” grammatical?'):
            self.assertFalse(explicit_unsupported_request(text))
        for text in ('Can you speak Russian?', 'ロシア語で話せる？'):
            self.assertTrue(explicit_unsupported_request(text))
        self.assertFalse(readable('Hello!', 'ja'))
        self.assertTrue(readable('Qwen3.', 'ja'))
        self.assertTrue(readable('Kokoro-82Mだよ。','ja'))
