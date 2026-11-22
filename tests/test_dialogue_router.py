import asyncio
from contextlib import ExitStack
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from ravora.conversation import MemoryStore, SpeechController
from ravora.database import MIGRATIONS
from ravora.dialogue_router import DialogueRoute, HOOKS
from ravora.fast_voice import FastVoiceSession
from ravora.fast_voice_server import MockVoiceClients
from ravora.providers import GeminiText
from ravora.ray_character import CharacterRuntime, public_fixture
from ravora.ray_memory import claim_hook
from ravora.settings import ProviderSettings
from support import Reaction, Speech
from test_persona import HTTP


class RouterTests(unittest.TestCase):
    def test_repeated_music_shifts_but_followup_and_new_subject_stay_on_track(self):
        ray = CharacterRuntime()
        def route(text, history):
            return ray.route(ray.begin(uuid4().hex, text), text, history)
        history = [('user','I played the piano.'), ('model','I like that rhythm.'),
                   ('user','ギターを練習したよ。'), ('model','最後まで弾けたんだね。')]
        self.assertEqual(route('I like jazz.', []).topic_saturation, 0)
        self.assertEqual(route('That rhythm is neat.', history[:2]).topic_saturation, .35)
        for text in ('そのリズムが好き。', 'うん。'):
            result = route(text, history)
            self.assertEqual((result.topic_saturation, result.dialogue_policy, result.need_knowledge_rag),
                             (.7, 'SHIFT_TO_HOOK', False))
        self.assertEqual(route('もっと詳しく教えて。', history).dialogue_policy, 'DEEP_DIVE')
        self.assertEqual(route('What is jazz?', history).rag_skip_reason, 'saturation')
        self.assertEqual(route('I planted tomatoes.', history).topic_saturation, 0)
        self.assertEqual(route('うん。', [('user','ピアノの話だよ'),('model','続きを聞く？')]).dialogue_policy, 'EMPATHY')
        self.assertEqual(route('へえ。', [('user','I like jazz.'),('model','How about footsteps on gravel.')]).dialogue_policy, 'EMPATHY')
        self.assertEqual(route('その話はつらい。', []).dialogue_policy, 'EMPATHY')
        self.assertEqual(route('別の話題にしよう。', []).dialogue_policy, 'SHIFT_TO_HOOK')
        self.assertEqual(route('雑談しよう。', []).dialogue_policy, 'SELF_DISCLOSURE')
        self.assertEqual(route('What interests you?', []).dialogue_policy, 'SELF_DISCLOSURE')
        for statements in [('I cleaned the telescope.', 'The telescope lens looks clear.', 'My telescope arrived.'),
                           ('望遠鏡を磨いた。','望遠鏡で星を見た。','望遠鏡を組み立てたよ。')]:
            earlier = [('user',statements[0]),('model','Nice.'),('user',statements[1]),('model','Interesting.')]
            self.assertEqual(route(statements[2],earlier).topic_saturation,.7)

    def test_incomplete_malformed_and_untrusted_private_fields_fall_back_independently(self):
        for data in (None, [], '{', '{"emotion":"curious"}',
                     {'topic_saturation':float('nan'), 'need_knowledge_rag':'false',
                      'dialogue_policy':{}, 'unspoken_hooks':[{'speech':'private'}, 'private text']}):
            self.assertEqual(DialogueRoute.from_payload(data), DialogueRoute())
        valid = next(iter(HOOKS))
        route = DialogueRoute.from_payload(json.dumps({'topic_saturation':2,'need_knowledge_rag':False,
                                                       'unspoken_hooks':[valid,valid,'ignored']}))
        self.assertEqual((route.topic_saturation,route.need_knowledge_rag,route.unspoken_hooks), (0,False,(valid,)))
        ray = CharacterRuntime()
        epoch = ray.begin('turn', 'Thank you!')
        with patch('ravora.ray_character.route_turn', return_value='{"dialogue_policy":"EMPATHY"}'):
            ray.route(epoch,'Thank you!',[])
        ray.react(epoch,'joy')
        self.assertEqual((ray.state.emotion,ray.state.dialogue_policy), ('pleased','EMPATHY'))

    def test_upgrade_from_v15_keeps_existing_data_and_reopens(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = root/'migrations'
            legacy.mkdir()
            for path in MIGRATIONS.glob('*.sql'):
                if int(path.name[:3]) <= 15:
                    (legacy/path.name).symlink_to(path)
            db_path = root/'old.db'
            MemoryStore(db_path,migrations=legacy).start('old','viewer','existing')
            for _ in range(2):
                with MemoryStore(db_path).connect() as db:
                    self.assertEqual(db.execute('SELECT comment FROM conversation_turns').fetchone(), ('existing',))
                    self.assertEqual(db.execute('SELECT count(*) FROM dialogue_hooks').fetchone(), (0,))


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.store = MemoryStore(self.root/'state.db')
        self.settings = replace(ProviderSettings(), semantic_memory_mode='jev', memory_extractor='local_rules')
        self.events, self.requests = [], []
        self.reply = 'That rhythm is catchy.'
        self.open_session()

    def open_session(self):
        clients = MockVoiceClients()
        async def generate(text, **kwargs):
            self.requests.append(kwargs)
            yield public_fixture(self.reply)
        generate.ray_public_output = True
        clients.generate = generate
        async def send(event): self.events.append(event)
        async def pcm(*args): pass
        self.fast = FastVoiceSession(self.settings,self.store,self.root,clients,send,pcm,mock=True)
        self.addAsyncCleanup(self.fast.close)

    async def speak(self,text):
        await self.fast.start_text(uuid4().hex,text)
        await asyncio.wait_for(self.fast.task,5)
        self.assertEqual(self.fast.phase,'generated',self.events[-1].get('code'))

    def play(self):
        self.assertTrue(self.fast.played(self.fast.token,self.fast.expected_frames))

    def hooks(self):
        with self.store.connect() as db:
            return dict(db.execute('SELECT hook,status FROM dialogue_hooks'))

    async def test_rest_and_grpc_skip_both_search_and_injection_at_each_gate(self):
        http = HTTP()
        adapter = GeminiText(self.settings,http)
        controller = SpeechController(self.settings,self.store,self.root,providers=(adapter,Reaction(),Speech()))
        self.addCleanup(controller.memory_worker.close)
        for data,reason in (({'need_knowledge_rag':False},'dialogue_policy'),
                            ({'topic_saturation':.7, 'need_knowledge_rag':True},'saturation')):
            with self.subTest(reason=reason), ExitStack() as stack:
                stack.enter_context(patch('ravora.ray_character.route_turn',return_value=json.dumps(data)))
                for name in ('episode_candidates','recall_context','conversation_memories','developer_banter_memory'):
                    stack.enter_context(patch.object(self.store,name,side_effect=AssertionError(name)))
                logs=stack.enter_context(self.assertLogs('ravora.dialogue_router',level='INFO'))
                adapter.set_memories([{'summary':'STALE_PRIVATE_REPORT'}])
                adapter.set_context([{'text':'STALE_PRIVATE_CONTEXT'}])
                events=[]
                metrics=controller.run('What is jazz?', 'viewer', uuid4().hex, events.append,threading.Event())
                self.assertEqual(events[-1]['kind'],'complete',events[-1])
                self.assertNotIn('STALE_PRIVATE',json.dumps(http.requests[-1]))
                self.assertEqual((metrics['memory_ids'],metrics['context_items'],metrics['knowledge_rag']),
                                 ([],0,'skipped_'+reason))
                await self.speak('What is jazz?')
                self.assertEqual((self.requests[-1]['memories'],self.requests[-1]['context']), ([],[]))
                self.play()
                self.assertEqual(sum('reason='+reason in record for record in logs.output),2)

    async def test_incomplete_private_json_allows_normal_retrieval_and_speech(self):
        with patch('ravora.ray_character.route_turn',return_value='{"unspoken_hooks":false}'), \
                patch.object(self.store,'conversation_memories',wraps=self.store.conversation_memories) as lookup:
            await self.speak('What did we talk about last time?')
            self.assertTrue(lookup.called)
            self.assertEqual(self.fast.character.state.need_knowledge_rag,True)
            self.play()

    async def test_played_music_history_selects_an_association_but_questions_keep_their_subject(self):
        for text in ('I tried the guitar.', '昨日はジャズを聴いた。'):
            await self.speak(text)
            self.play()
        with patch.object(self.store,'episode_candidates',side_effect=AssertionError('knowledge lookup')):
            await self.speak('The melody in this song is interesting.')
            request = self.requests[-1]
            self.assertIn('patterns in woven cloth', request['prompt'])
            self.assertNotIn('footsteps on different surfaces', request['prompt'])
            self.assertTrue(request['history'])
            await self.fast.cancel()
            await self.speak('この曲のリズムを教えて。')
            self.assertEqual(self.fast.character.state.dialogue_policy,'DEEP_DIVE')
            self.assertEqual(self.fast.character.state.topic_saturation,.7)
            self.assertNotIn('patterns in woven cloth',self.requests[-1]['prompt'])
            self.play()

    async def test_hook_survives_restart_is_only_consumed_by_its_acknowledged_spoken_turn(self):
        await self.speak('I like jazz.')
        self.assertEqual(self.hooks(),{})
        self.play()
        first, second = self.fast.character.state.unspoken_hooks
        self.assertEqual(self.hooks(),{first:'unspoken',second:'unspoken'})
        await self.fast.close()
        self.store=MemoryStore(self.store.path)
        self.open_session()
        self.reply='I wonder how footsteps change outdoors.'
        await self.speak('なるほど。')
        self.assertIn(first,self.requests[-1]['prompt'])
        self.assertNotIn(second,self.requests[-1]['prompt'])
        self.assertEqual(self.hooks()[first],'unspoken')
        await self.fast.cancel()
        self.reply='Good point.'
        await self.speak('Something else.')
        self.play()
        self.assertEqual(self.hooks()[first],'unspoken')
        self.reply='雨の日の足音って面白いね。'
        await self.speak('へえ。')
        self.assertIn(first,self.requests[-1]['prompt'])
        self.play()
        self.assertEqual(self.hooks(),{first:'used',second:'unspoken'})
        with self.store.connect() as db:
            event,=db.execute('SELECT source_id FROM dialogue_hooks LIMIT 1').fetchone()
            self.store.remove_source(db,event,time.time())
        self.assertEqual(self.hooks(),{})

    async def test_hook_reservation_is_owner_scoped_and_exclusive(self):
        await self.speak('I listen to jazz.')
        self.play()
        owner=self.fast.viewer
        def start(who):
            turn=uuid4().hex
            self.store.start(turn,'offline:'+who,'Okay.')
            return turn
        self.assertEqual(claim_hook(self.store,'offline','stranger',start('stranger'),'Okay.'),'')
        turns = [start(owner) for _ in range(3)]
        picked = await asyncio.gather(*(asyncio.to_thread(claim_hook,self.store,'offline',owner,t,'Okay.') for t in turns))
        self.assertLessEqual(sum(bool(h) for h in picked),2)
        self.assertEqual(len({h for h in picked if h}),sum(bool(h) for h in picked))
        self.assertTrue(any(picked))
        used = picked.index(next(h for h in picked if h))
        self.store.cancel_turn(turns[used])
        self.assertTrue(claim_hook(self.store,'offline',owner,start(owner),'Okay.'))

    async def test_busy_notebook_skips_reservation_then_allows_the_next_turn(self):
        await self.speak('I like jazz.')
        self.play()
        owner = self.fast.viewer
        turn = uuid4().hex
        self.store.start(turn,'offline:'+owner,'Okay.')
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            started = time.monotonic()
            self.assertEqual(claim_hook(self.store,'offline',owner,turn,'Okay.'),'')
            self.assertLess(time.monotonic()-started,.5)
        self.assertTrue(claim_hook(self.store,'offline',owner,turn,'Okay.'))

    async def test_private_or_opted_out_subject_does_not_create_hooks(self):
        await self.speak('Keep this off-stream, I play the piano.')
        self.play()
        self.assertEqual(self.hooks(),{})
