import asyncio
from dataclasses import replace
from pathlib import Path
import shutil
import tempfile
import threading
from types import SimpleNamespace as NS
import unittest
import uuid

from ravora.conversation import MemoryStore
from ravora.database import Database, MIGRATIONS, SCHEMA_VERSION
from ravora.input_records import InputRecords
from ravora.early_reaction import EarlyReactions
from ravora.edge_grpc import EdgeClients, EdgeError
from ravora.fast_voice import FastVoiceSession
from ravora.fast_voice_server import MockVoiceClients
from ravora.providers import ProviderError
from ravora.settings import ProviderSettings, SettingsError, load_settings


async def until(predicate, seconds=2):
    async with asyncio.timeout(seconds):
        while not predicate():
            await asyncio.sleep(.005)


class EarlyReactionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.store=MemoryStore(self.root/'memory.sqlite3');self.records=InputRecords(self.store,'session','mock')
        self.input_id=uuid.uuid4().hex;self.key,_=self.records.create(self.input_id,'audio',b'')
        self.records.state(self.key,'simulated');self.events=[];self.calls=[];self.active=True
        self.s=replace(ProviderSettings(),early_reaction_debounce_ms=100,early_reaction_cooldown_ms=300)
        async def emit(value):self.events.append(value)
        async def classify(text):
            self.calls.append(text)
            return {'reaction':'joy','source':'test'},{}
        self.early=EarlyReactions(self.s,self.store,self.key,self.input_id,lambda:self.active,emit,mock=True,classify=classify)
        self.addAsyncCleanup(self.early.close)

    def offer(self,text='I like coffee',stability=.9):
        self.early.offer({'text':text,'stability':stability,'segment_final':False})

    def rows(self):
        with self.store.connect() as db:
            return db.execute('SELECT state,attempted,emitted_at,turn_id FROM early_reaction_calls ORDER BY sequence').fetchall()

    async def test_interim_is_classified_before_any_final_turn_or_memory_exists(self):
        self.offer();await until(lambda:self.events)
        self.assertEqual(self.calls,['I like coffee'])
        self.assertEqual((self.events[0]['phase'],self.events[0]['provisional']),('partial',True))
        self.assertEqual(self.events[0]['input_id'],self.input_id)
        self.assertEqual(self.rows()[0][:2],('accepted',0));self.assertIsNone(self.rows()[0][3])
        with self.store.connect() as db:
            for table in ('source_events','conversation_turns','memory_jobs'):
                self.assertEqual(db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0],0)
            columns=[row[1] for row in db.execute('PRAGMA table_info(early_reaction_calls)')]
            self.assertNotIn('text',columns)

    async def test_low_stability_is_ignored_and_missing_stability_uses_debounce(self):
        self.offer(stability=.2);await asyncio.sleep(.12);self.assertFalse(self.calls)
        self.offer('That sounds really exciting',None);await asyncio.sleep(.05);self.assertFalse(self.calls)
        self.offer('A completely different thought',None)
        await asyncio.sleep(.06);self.assertFalse(self.calls)
        await until(lambda:self.calls)
        self.assertEqual(self.calls,['A completely different thought'])

    async def test_repeated_low_stability_prefix_can_trigger_while_hypothesis_grows(self):
        self.offer('I am really',.01);await asyncio.sleep(.06);self.assertFalse(self.calls)
        self.offer('I am really happy',.01)
        await until(lambda:self.events)
        self.assertEqual(self.calls,['I am really happy'])
        for text in ['I am really happy about','I am really happy about today']:
            self.offer(text,.01)
        self.assertEqual(len(self.rows()),1);self.assertEqual(len(self.calls),1)

    async def test_revised_low_stability_prefix_must_rebuild_agreement(self):
        self.offer('I am really',.01);await asyncio.sleep(.06)
        self.offer('We are very surprised',.01);await asyncio.sleep(.06)
        self.assertFalse(self.calls)
        self.offer('We are very surprised today',.01);await until(lambda:self.events)
        self.assertEqual(self.calls,['We are very surprised today'])

    async def test_punctuation_update_does_not_discard_inflight_reaction(self):
        started=asyncio.Event();release=asyncio.Event()
        async def classify(text):
            self.calls.append(text);started.set();await release.wait()
            return {'reaction':'joy','source':'test'},{}
        self.early.classify=classify
        self.offer('I am really happy');await started.wait()
        self.offer('I am really happy, today!');release.set()
        await until(lambda:self.events)
        self.assertEqual(self.rows()[0][0],'accepted');self.assertEqual(len(self.calls),1)

    async def test_duplicates_and_updates_share_one_durable_slot(self):
        self.offer();await until(lambda:self.events)
        for _ in range(10):self.offer('  I   LIKE coffee  ')
        await asyncio.sleep(.04);self.assertEqual(len(self.calls),1)
        for i in range(30):self.offer('I like coffee number '+str(i))
        self.assertEqual(len(self.calls),1)
        self.offer('Something else entirely');await asyncio.sleep(.35)
        self.assertEqual(len(self.calls),1);self.assertEqual(len(self.rows()),1)
        # Recreating the worker on this input cannot reset the database bound.
        await self.early.close()
        self.early=EarlyReactions(self.s,self.store,self.key,self.input_id,lambda:True,
            self.early.emit,mock=True,classify=self.early.classify)
        self.offer('A new hypothesis');await asyncio.sleep(.05)
        self.assertEqual(len(self.calls),1)

    async def test_corrected_hypothesis_drops_older_inflight_result(self):
        started=asyncio.Event();release=asyncio.Event()
        async def classify(text):
            self.calls.append(text)
            if len(self.calls)==1:started.set();await release.wait()
            return {'reaction':'surprise','source':'test'},{}
        self.early.classify=classify
        self.offer('I love this game');await started.wait()
        self.offer('I hate this game');release.set()
        await until(lambda:self.rows() and self.rows()[0][0]=='superseded')
        self.assertEqual(self.rows()[0][0],'superseded')
        self.assertEqual(len(self.events),0);self.assertEqual(len(self.calls),1)

    async def test_cancelled_thread_cannot_apply_late_result_and_attempt_stays_consumed(self):
        await self.early.close();self.records.state(self.key,'dispatched')
        started=threading.Event();release=threading.Event();returned=threading.Event()
        self.addCleanup(release.set)
        def blocking():
            started.set();release.wait(2);returned.set()
            return {'reaction':'joy','source':'test'},{}
        async def classify(text):return await asyncio.to_thread(blocking)
        self.early=EarlyReactions(replace(self.s,api_enabled=True,stt_api_enabled=True),self.store,self.key,self.input_id,
            lambda:self.active,self.early.emit,mock=False,classify=classify)
        self.offer();self.assertTrue(await asyncio.to_thread(started.wait,1))
        self.active=False;await self.early.close()
        self.assertEqual(self.rows()[0][:2],('outcome_unknown',1))
        release.set();self.assertTrue(await asyncio.to_thread(returned.wait,1));await asyncio.sleep(.01)
        self.assertFalse(self.events);self.assertEqual(self.rows()[0][:2],('outcome_unknown',1))

    async def test_prediction_error_does_not_poison_subsequent_updates(self):
        async def fail(text):raise ProviderError('PROVIDER_CONNECTION_FAILED')
        self.early.classify=fail;self.offer();await until(lambda:self.rows())
        await until(lambda:self.rows()[0][0]=='outcome_unknown')
        self.assertEqual(self.events[0]['kind'],'reaction_unavailable')
        self.assertTrue(self.early.active)


class StreamingPathTests(unittest.IsolatedAsyncioTestCase):
    async def test_stt_adapter_emits_interim_and_segment_finals_but_returns_only_final_text(self):
        make=lambda **kwargs:NS(**kwargs)
        def explicit(**kwargs):return make(**kwargs)
        explicit.AudioEncoding=NS(LINEAR16=1)
        types=NS(StreamingRecognitionConfig=make,RecognitionConfig=make,ExplicitDecodingConfig=explicit,
            RecognitionFeatures=make,StreamingRecognitionFeatures=make,StreamingRecognizeRequest=make)
        requests=[];events=[]
        def result(text,final=False,stability=0):return NS(alternatives=[NS(transcript=text)],is_final=final,stability=stability)
        class STT:
            async def streaming_recognize(self,**kwargs):
                async for request in kwargs['requests']:requests.append(request)
                async def replies():
                    for results in ([result('I lie',stability=.2)], [result('I like coffee',stability=.9)],
                                    [result('I like coffee.',True),result('And jazz')], [result('And jazz.',True)]):
                        yield NS(speech_event_type=None,results=results)
                return replies()
        async def frames():yield b'\x00\x10'*1600
        async def emit(value):events.append(value)
        client=EdgeClients({'access_token':'not-a-token'});client.stt=STT();client.speech_types=types
        text,diagnostics=await client.transcribe_frames(frames(),emit)
        partials=[e for e in events if e['kind']=='stt_partial']
        self.assertEqual(partials[0]['text'],'I lie');self.assertEqual(partials[1]['stability'],.9)
        self.assertEqual(partials[2]['text'],'I like coffee. And jazz');self.assertIsNone(partials[2]['stability'])
        self.assertTrue(partials[-1]['segment_final'])
        self.assertEqual(text,'I like coffee. And jazz.');self.assertNotIn('I lie',text)
        self.assertEqual(diagnostics['final_segments'],2)
        self.assertTrue(requests[0].streaming_config.streaming_features.interim_results)

    async def setup_session(self,*,enabled=True,max_requests=1):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);root=Path(tmp.name)
        store=MemoryStore(root/'memory.sqlite3');events=[];inputs=[]
        class Capture(MockVoiceClients):
            async def generate(self,text,**kwargs):
                inputs.append(text)
                async for delta in super().generate(text,**kwargs):yield delta
        async def emit(value):events.append(value)
        async def pcm(token,data):pass
        session=FastVoiceSession(replace(ProviderSettings(),early_reaction_enabled=enabled,early_reaction_max_requests=max_requests,memory_extractor='local_rules'),
            store,root,Capture(),emit,pcm,mock=True)
        self.addAsyncCleanup(session.close)
        return session,store,events,inputs

    async def test_early_expression_while_listening_then_full_transcript_to_llm(self):
        session,store,events,inputs=await self.setup_session()
        identifier=uuid.uuid4().hex;await session.start_audio(identifier,'en-US')
        await session.audio_chunk(identifier,b'\x00\x10'*3200)
        await until(lambda:any(e['kind']=='reaction' and e.get('phase')=='partial' for e in events))
        self.assertEqual(session.phase,'listening');self.assertFalse(inputs)
        with store.connect() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM source_events').fetchone()[0],0)
        await session.end_audio(identifier);await asyncio.wait_for(session.task,4)
        self.assertEqual(session.phase,'generated',events);self.assertEqual(inputs,['I like coffee and jazz.'])
        self.assertFalse(any(e['kind']=='reaction' and e.get('phase')=='final' for e in events))
        with store.connect() as db:
            self.assertEqual(db.execute('SELECT text FROM source_events').fetchall(),[('I like coffee and jazz.',)])
            self.assertEqual(db.execute('SELECT turn_id FROM early_reaction_calls').fetchone()[0],session.token['turn_id'])
            self.assertEqual(db.execute('SELECT SUM(attempted) FROM early_reaction_calls').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT SUM(attempted) FROM provider_calls').fetchone()[0],0)

    async def test_slow_early_jev_never_holds_full_transcript_away_from_llm(self):
        session,store,events,inputs=await self.setup_session();identifier=uuid.uuid4().hex
        await session.start_audio(identifier,'en-US');started=asyncio.Event()
        async def slow(text):started.set();await asyncio.sleep(5)
        session.early.classify=slow
        await session.audio_chunk(identifier,b'\x00\x10'*3200);await started.wait()
        await session.end_audio(identifier);await asyncio.wait_for(session.task,1)
        self.assertEqual(inputs,['I like coffee and jazz.']);self.assertEqual(session.phase,'generated',events)
        self.assertFalse(any(e['kind']=='reaction' and e.get('phase')=='partial' for e in events))

    async def test_disabled_feature_preserves_full_transcript_route(self):
        session,store,events,inputs=await self.setup_session(enabled=False);identifier=uuid.uuid4().hex
        await session.start_audio(identifier,'en-US');await session.audio_chunk(identifier,b'\x00\x10'*3200)
        await session.end_audio(identifier);await asyncio.wait_for(session.task,4)
        self.assertEqual(inputs,['I like coffee and jazz.']);self.assertEqual(session.early.request_phase,'final')
        with store.connect() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM early_reaction_calls').fetchone()[0],0)

    async def test_stt_failure_resets_early_face_and_creates_no_conversation(self):
        session,store,events,inputs=await self.setup_session();original=session.clients.transcribe_frames
        async def fail(*args,**kwargs):
            await original(*args,**kwargs)
            raise EdgeError('EMPTY_OR_OVERSIZED_STT')
        session.clients.transcribe_frames=fail;identifier=uuid.uuid4().hex
        await session.start_audio(identifier,'en-US');await session.audio_chunk(identifier,b'\x00\x10'*3200)
        await until(lambda:any(e['kind']=='reaction' and e.get('phase')=='partial' for e in events))
        await session.end_audio(identifier);await asyncio.wait_for(session.task,2)
        self.assertEqual(session.phase,'failed');self.assertFalse(inputs)
        self.assertEqual(events[-2]['kind'],'reaction_reset')
        with store.connect() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM conversation_turns').fetchone()[0],0)


class EarlySettingsTests(unittest.TestCase):
    def test_env_and_limits(self):
        s=load_settings(Path('/absent'),environ={'RAVORA_EARLY_REACTION_ENABLED':'false','RAVORA_EARLY_REACTION_MAX_REQUESTS':'1'})
        self.assertFalse(s.early_reaction_enabled);self.assertEqual(s.early_reaction_max_requests,1)
        for values in ({'early_reaction_enabled':1},{'early_reaction_stability':.1},{'early_reaction_stability':True},{'early_reaction_debounce_ms':0},
                       {'early_reaction_cooldown_ms':-1},{'early_reaction_max_requests':0},{'early_reaction_max_requests':True},{'early_reaction_max_requests':4}):
            with self.subTest(values=values),self.assertRaises(SettingsError):replace(ProviderSettings(),**values).validate()

    def test_v6_migration_keeps_existing_input_attempts(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);migrations=root/'v6';migrations.mkdir()
            for path in MIGRATIONS.glob('*.sql'):
                if int(path.name[:3])<=6:shutil.copy2(path,migrations/path.name)
            path=root/'db.sqlite3';Database(path,migrations=migrations);old=MemoryStore.existing(path,readonly=False)
            records=InputRecords(old,'old-session','mock');key,_=records.create(uuid.uuid4().hex,'audio',b'')
            records.state(key,'simulated')
            with old.connect() as db:before=db.execute('SELECT * FROM developer_inputs').fetchall()
            upgraded=MemoryStore(path)
            with upgraded.connect() as db:
                self.assertEqual([row[:-3] for row in db.execute('SELECT * FROM developer_inputs')],before)
                self.assertEqual(db.execute('SELECT reaction_limit,reaction_applied_sequence,reaction_result FROM developer_inputs').fetchall(),[(1,0,None)])
                self.assertEqual(db.execute('SELECT COUNT(*) FROM early_reaction_calls').fetchone()[0],0)
                self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],SCHEMA_VERSION)


if __name__=='__main__':unittest.main()
