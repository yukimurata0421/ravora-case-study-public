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
        for text,policy in [('そのリズムが好き。','SHIFT_TO_HOOK'), ('うん。','CONTINUE_TOPIC'),
                            ('うんうん。','CONTINUE_TOPIC'),('Mm-hmm.','CONTINUE_TOPIC'),
                            ('ジャズの裏拍に合わせると旋律の聞こえ方が変わる。','DEEP_DIVE')]:
            result = route(text, history)
            self.assertEqual((result.topic_saturation, result.dialogue_policy, result.need_knowledge_rag),
                             (.7, policy, False))
        self.assertEqual(route('もっと詳しく教えて。', history).dialogue_policy, 'DEEP_DIVE')
        self.assertEqual(route('What is jazz?', history).rag_skip_reason, 'saturation')
        for question in ('前回のジャズのアルバム、何だっけ？', 'Which jazz album did I recommend last time?'):
            metrics = {}
            result = route(question, history)
            self.assertEqual((result.topic_saturation,result.dialogue_policy), (.7,'DEEP_DIVE'))
            self.assertTrue(result.allow_retrieval(metrics,question))
            self.assertEqual(metrics['knowledge_rag'],'explicit_recall')
            self.assertFalse(DialogueRoute(topic_saturation=.7,need_knowledge_rag=False).allow_retrieval({},question))
        for comment in ('昨日のジャズは何だか懐かしかった。', 'ジャズの曲は今でも覚えてるよ。', 'I listened to jazz yesterday.'):
            self.assertFalse(route(comment,history).allow_retrieval({},comment))
        self.assertEqual(route('I planted tomatoes.', history).topic_saturation, 0)
        self.assertEqual(route('うん。', [('user','ピアノの話だよ'),('model','続きを聞く？')]).dialogue_policy, 'CONTINUE_TOPIC')
        self.assertEqual(route('へえ。', [('user','I like jazz.'),('model','How about footsteps on gravel.')]).dialogue_policy, 'CONTINUE_TOPIC')
        for line in ('話題を変えないで。', "Don't change the subject."):
            epoch = ray.begin(line,line)
            ray.route(epoch,line,history)
            self.assertEqual(ray.plan(line,epoch=epoch).intent,'answer')
        for text,prior in [('うん。',[]), ('そうだね',[('user','この話は内緒。'),('model','わかった。')]), ('Thanks!',history), ('haha',history),
                           ('Yep.',[('user','I need to rest.'),('model','Rest well.')])]:
            self.assertEqual(route(text,prior).dialogue_policy,'EMPATHY')
        self.assertEqual(route('その話はつらい。', []).dialogue_policy, 'EMPATHY')
        self.assertEqual(route('別の話題にしよう。', []).dialogue_policy, 'SHIFT_TO_HOOK')
        self.assertEqual(route('雑談しよう。', []).dialogue_policy, 'SELF_DISCLOSURE')
        self.assertEqual(route('What interests you?', []).dialogue_policy, 'SELF_DISCLOSURE')
        for question in ('音楽以外の趣味は？', 'What are your hobbies besides music?'):
            ray = CharacterRuntime()
            epoch = ray.begin('hobbies', question)
            self.assertEqual(ray.route(epoch,question,[]).dialogue_policy, 'SELF_DISCLOSURE')
            memories = [{'kind':'interest','topic':topic,'strength':.6} for topic in ('music','puzzles')]
            plan = ray.plan(question,memories,epoch=epoch)
            self.assertEqual(plan.preferences,(('puzzles',.6),))
            self.assertNotEqual(plan.topic,'music')
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
            # Seed the historical schema without calling a writer that requires newer columns.
            with MemoryStore(db_path,migrations=legacy).connect() as db:
                db.execute("""INSERT INTO conversation_turns(id,turn_id,viewer,comment,status,started)
                              VALUES('old','old','viewer','existing','running','2026-11-01')""")
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
        controller.memory_worker.character.set_preference('stargazing',.6,kind='interest')
        for worker in (controller.memory_worker,self.fast.memory):
            worker._character_reload()
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
                self.assertIn('constellations', self.requests[-1]['prompt'])
                self.assertIn('constellations', http.requests[-1]['systemInstruction']['parts'][0]['text'])
                self.play()
                self.assertEqual(sum('reason='+reason in record for record in logs.output),2)

    async def test_incomplete_private_json_allows_normal_retrieval_and_speech(self):
        with patch('ravora.ray_character.route_turn',return_value='{"unspoken_hooks":false}'), \
                patch.object(self.store,'conversation_memories',wraps=self.store.conversation_memories) as lookup:
            await self.speak('What did we talk about last time?')
            self.assertTrue(lookup.called)
            self.assertEqual(self.fast.character.state.need_knowledge_rag,True)
            self.play()

    async def test_saturated_bilingual_recall_uses_eligible_source_on_both_transports(self):
        from test_semantic_memory import seed
        earlier = 'ジャズで最初に勧めたのは青い橋です。'
        seed(self.store,text=earlier,owner=self.settings.developer_id,platform='developer-mock')
        await self.speak('I played the piano.')
        self.play()
        await self.speak('I practiced jazz.')
        self.play()
        await self.speak('前回のジャズ、何を勧めたか覚えてる？')
        self.assertEqual(self.fast.character.state.topic_saturation,.7)
        self.assertTrue(any(earlier in entry['text'] for entry in self.requests[-1]['context']))
        self.play()

        _, owner = seed(self.store,text='My jazz recommendation was Blue Bridge.')
        seed(self.store,text='My jazz recommendation was Other Album.',owner='other')
        adapter = GeminiText(self.settings,HTTP())
        controller = SpeechController(self.settings,self.store,self.root,providers=(adapter,Reaction(),Speech()))
        self.addCleanup(controller.memory_worker.close)
        repeated = [('user','I like jazz.'),('model','Nice rhythm.')] * 2
        question = 'Which jazz album did I recommend last time?'
        with patch.object(self.store,'history',return_value=repeated):
            def ask():
                return controller.run(question,owner.viewer,uuid4().hex,lambda event:None,threading.Event())
            metrics = ask()
            self.assertEqual(metrics['knowledge_rag'],'explicit_recall')
            content = json.dumps(adapter.http.requests[-1])
            self.assertIn('Blue Bridge',content)
            self.assertNotIn('Other Album',content)
            with self.store.connect() as db:
                self.store.remove_source(db,owner.receipt,time.time())
            ask()
            self.assertNotIn('Blue Bridge',json.dumps(adapter.http.requests[-1]))

    async def test_live_response_does_not_wait_for_remote_memory_extraction(self):
        settings = replace(self.settings, comment_source='youtube', memory_extractor='gemini',
                           semantic_memory_mode='off', memory_api_enabled=True)
        controller = SpeechController(settings,self.store,self.root,
                                      providers=(GeminiText(settings,HTTP()),Reaction(),Speech()))
        self.addCleanup(controller.memory_worker.close)
        with patch.object(controller.memory_worker,'wait_for',side_effect=AssertionError('blocked on extraction')):
            events = []
            metrics = controller.run('What is a telescope?', 'viewer', uuid4().hex, events.append, threading.Event())
            self.assertEqual(metrics['knowledge_rag'],'allowed')
            self.assertEqual(events[-1]['kind'],'complete')
        await self.fast.close()
        self.settings = settings
        self.open_session()
        with patch.object(self.fast.memory,'wait_for',side_effect=AssertionError('blocked on extraction')):
            await self.speak('What is a telescope?')

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
        await self.speak('なるほど。')
        self.assertNotIn(first,self.requests[-1]['prompt'])
        self.assertTrue(self.requests[-1]['history'])
        self.assertEqual(self.fast.character.state.dialogue_policy,'CONTINUE_TOPIC')
        self.assertEqual(self.hooks(),{first:'unspoken',second:'unspoken'})
        await self.fast.cancel()
        await self.fast.close()
        self.store=MemoryStore(self.store.path)
        self.open_session()
        self.reply='I wonder how footsteps change outdoors.'
        await self.speak('話題を変えて。')
        self.assertIn(first,self.requests[-1]['prompt'])
        self.assertNotIn(second,self.requests[-1]['prompt'])
        self.assertEqual(self.hooks()[first],'unspoken')
        await self.fast.cancel()
        self.reply='Good point.'
        await self.speak('Something else.')
        self.play()
        self.assertEqual(self.hooks()[first],'unspoken')
        self.reply='雨の日の足音って面白いね。'
        await self.speak('別の話題にしよう。')
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
