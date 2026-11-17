import asyncio
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import time
import unittest
import uuid

import test_early_reaction as fixtures
from ravora.comments import Comment,CommentInbox
from ravora.conversation import MemoryStore
from ravora.database import Database,MIGRATIONS
from ravora.input_records import InputRecords
from ravora.early_reaction import EarlyReactions
from ravora.providers import ProviderError
from ravora.settings import ProviderSettings


class DualReactionTests(unittest.IsolatedAsyncioTestCase):
    async def setup_session(self,**kwargs):
        return await fixtures.StreamingPathTests.setup_session(self,max_requests=2,**kwargs)

    def counts(self,store):
        with store.connect() as db:
            return (db.execute('SELECT COUNT(*) FROM early_reaction_calls').fetchone()[0],
                db.execute("SELECT COUNT(*) FROM provider_calls WHERE provider='jev'").fetchone()[0],
                db.execute('SELECT COUNT(*) FROM reaction_stage_claims').fetchone()[0])

    async def start(self,*,first='joy',last='surprise',classify=None):
        session,store,events,inputs=await self.setup_session();identifier=uuid.uuid4().hex
        await session.start_audio(identifier,'en-US')
        async def immediate(text):
            return {'reaction':first if text=='I like coffee' else last,'source':'test'},{}
        session.early.classify=classify or immediate
        await session.audio_chunk(identifier,b'\x00\x10'*3200)
        return session,store,events,inputs,identifier

    async def completed_pair(self,last):
        session,store,events,inputs,identifier=await self.start(last=last)
        await fixtures.until(lambda:any(e['kind']=='reaction' for e in events))
        await session.end_audio(identifier);await asyncio.wait_for(session.task,2)
        await asyncio.wait_for(session.early.task,2)
        updates=[e for e in events if e['kind']=='reaction']
        self.assertEqual([e['reaction_sequence'] for e in updates],[1,2])
        self.assertEqual(inputs,['I like coffee and jazz.']);self.assertEqual(self.counts(store),(1,1,2))
        self.assertEqual(session.early.phase_requests,{'partial':1,'final':1})
        with store.connect() as db:
            self.assertEqual(db.execute('SELECT reaction_limit,reaction_applied_sequence FROM developer_inputs').fetchone(),(2,2))
            self.assertEqual(json.loads(db.execute('SELECT reaction FROM conversation_turns').fetchone()[0])['reaction'],last)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM input_reaction_claims').fetchone()[0],1)
        return session,store,events,updates

    async def test_same_expression_is_a_confirmation_not_another_transition(self):
        _,_,_,updates=await self.completed_pair('joy')
        self.assertEqual([e['transition'] for e in updates],['switch','continue'])
        self.assertEqual(updates[1]['previous_reaction'],'joy')

    async def test_changed_expression_supersedes_the_provisional_expression(self):
        _,_,_,updates=await self.completed_pair('surprise')
        self.assertEqual(updates[1]['transition'],'switch');self.assertEqual(updates[1]['reaction'],'surprise')

    async def test_neutral_final_returns_from_the_provisional_expression(self):
        _,_,_,updates=await self.completed_pair('neutral')
        self.assertEqual(updates[1]['transition'],'return_neutral')

    async def test_final_does_not_wait_for_slow_partial_and_old_result_cannot_revert_it(self):
        started=asyncio.Event();release=asyncio.Event()
        async def classify(text):
            if text=='I like coffee':started.set();await release.wait();return {'reaction':'joy'},{}
            return {'reaction':'surprise'},{}
        session,store,events,inputs,identifier=await self.start(classify=classify)
        await asyncio.wait_for(started.wait(),1)
        await session.end_audio(identifier);await asyncio.wait_for(session.task,1)
        await fixtures.until(lambda:any(e.get('phase')=='final' and e['kind']=='reaction' for e in events))
        self.assertEqual(session.phase,'generated');self.assertEqual(inputs,['I like coffee and jazz.'])
        release.set();await asyncio.wait_for(session.early.task,1)
        self.assertEqual([e['phase'] for e in events if e['kind']=='reaction'],['final'])
        with store.connect() as db:
            self.assertEqual(db.execute('SELECT state FROM early_reaction_calls').fetchone()[0],'superseded_or_closed')
            self.assertEqual(json.loads(db.execute('SELECT reaction FROM conversation_turns').fetchone()[0])['reaction'],'surprise')

    async def test_slow_final_does_not_delay_text_audio_or_generation_completion(self):
        final_started=asyncio.Event();release=asyncio.Event()
        async def classify(text):
            if text!='I like coffee':final_started.set();await release.wait()
            return {'reaction':'joy' if text=='I like coffee' else 'neutral'},{}
        session,store,events,_,identifier=await self.start(classify=classify)
        await fixtures.until(lambda:any(e['kind']=='reaction' for e in events))
        await session.end_audio(identifier);await asyncio.wait_for(final_started.wait(),1)
        await asyncio.wait_for(session.task,1);self.assertEqual(session.phase,'generated')
        self.assertTrue(any(e['kind']=='audio_end' for e in events));self.assertTrue(any(e['kind']=='complete' for e in events))
        self.assertEqual([e['reaction'] for e in events if e['kind']=='reaction'],['joy'])
        release.set();await asyncio.wait_for(session.early.task,1)
        self.assertEqual([e['reaction'] for e in events if e['kind']=='reaction'],['joy','neutral'])

    async def test_partial_failure_still_allows_one_independent_final(self):
        async def classify(text):
            if text=='I like coffee':raise ProviderError('PROVIDER_CONNECTION_FAILED')
            return {'reaction':'joy'},{}
        session,store,events,_,identifier=await self.start(classify=classify)
        await fixtures.until(lambda:any(e['kind']=='reaction_unavailable' for e in events))
        await session.end_audio(identifier);await session.task;await session.early.task
        self.assertEqual(self.counts(store),(1,1,2))
        self.assertEqual([e['phase'] for e in events if e['kind']=='reaction'],['final'])
        with store.connect() as db:self.assertEqual(db.execute('SELECT state FROM early_reaction_calls').fetchone()[0],'outcome_unknown')

    async def test_final_failure_keeps_provisional_and_recreation_cannot_retry(self):
        calls=[]
        async def classify(text):
            calls.append(text)
            if text!='I like coffee':raise ProviderError('PROVIDER_CONNECTION_FAILED')
            return {'reaction':'joy'},{}
        session,store,events,_,identifier=await self.start(classify=classify)
        await fixtures.until(lambda:any(e['kind']=='reaction' for e in events))
        await session.end_audio(identifier);await session.task;await session.early.task
        self.assertEqual([e['reaction'] for e in events if e['kind']=='reaction'],['joy'])
        old=session.early;other=EarlyReactions(session.s,store,session.key,identifier,lambda:True,old.emit,mock=True,classify=classify)
        self.addAsyncCleanup(other.close);other.bind(session.token['turn_id'])
        other.finalize('I like coffee and jazz.');other.finalize('A revised final cannot reserve another request.')
        await asyncio.wait_for(other.task,1)
        self.assertEqual(len(calls),2);self.assertEqual(self.counts(store),(1,1,2))
        with store.connect() as db:self.assertEqual(json.loads(db.execute('SELECT reaction_result FROM developer_inputs').fetchone()[0])['reaction'],'joy')

    async def test_no_eligible_partial_means_one_final_request_with_rank_two(self):
        session,store,events,_=await self.setup_session(enabled=False)
        await session.start_text(uuid.uuid4().hex,'Hello.');await session.task;await session.early.task
        self.assertEqual(self.counts(store),(0,1,1))
        updates=[e for e in events if e['kind']=='reaction']
        self.assertEqual([e['reaction_sequence'] for e in updates],[2]);self.assertEqual(session.early.sequence,1)

    async def test_both_neutral_results_are_not_visible_restarts(self):
        session,store,events,_,identifier=await self.start(first='neutral',last='neutral')
        await fixtures.until(lambda:any(e['kind']=='reaction' for e in events))
        await session.end_audio(identifier);await session.task;await session.early.task
        self.assertEqual([e['transition'] for e in events if e['kind']=='reaction'],['continue','continue'])

    async def test_cancel_consumes_both_claims_and_suppresses_pending_results(self):
        started=[]
        async def classify(text):started.append(text);await asyncio.sleep(5)
        session,store,events,_,identifier=await self.start(classify=classify)
        await fixtures.until(lambda:len(started)==1)
        await session.end_audio(identifier);await fixtures.until(lambda:len(started)==2)
        await session.cancel();self.assertFalse(session.early.active)
        self.assertEqual(self.counts(store),(1,1,2));self.assertFalse(any(e['kind']=='reaction' for e in events))

    async def test_playback_completion_rejects_a_late_final(self):
        release=asyncio.Event();started=asyncio.Event()
        async def classify(text):
            if text!='I like coffee':started.set();await release.wait()
            return {'reaction':'joy' if text=='I like coffee' else 'surprise'},{}
        session,store,events,_,identifier=await self.start(classify=classify)
        await fixtures.until(lambda:any(e['kind']=='reaction' for e in events))
        await session.end_audio(identifier);await session.task;await started.wait()
        self.assertTrue(session.played(session.token,session.expected_frames));release.set();await session.early.task
        self.assertEqual([e['reaction'] for e in events if e['kind']=='reaction'],['joy'])

    async def test_removed_source_cannot_accept_a_late_final(self):
        release=asyncio.Event();started=asyncio.Event()
        async def classify(text):
            if text!='I like coffee':started.set();await release.wait()
            return {'reaction':'joy' if text=='I like coffee' else 'surprise'},{}
        session,store,events,_,identifier=await self.start(classify=classify)
        await fixtures.until(lambda:any(e['kind']=='reaction' for e in events))
        await session.end_audio(identifier);await session.task;await started.wait()
        with store.connect() as db:store.remove_source(db,session.receipt,time.time())
        release.set();await session.early.task
        self.assertEqual([e['reaction'] for e in events if e['kind']=='reaction'],['joy'])
        self.assertFalse(session.played(session.token,session.expected_frames))
        with store.connect() as db:
            self.assertIsNone(db.execute('SELECT reaction_result FROM developer_inputs').fetchone()[0])
            self.assertEqual(db.execute('SELECT COUNT(*) FROM reaction_stage_claims').fetchone()[0],2)

    async def test_raw_expiry_clears_cached_reaction_but_keeps_consumed_stages(self):
        session,store,_,_=await self.completed_pair('surprise')
        store.expire(now=time.time()+86401)
        with store.connect() as db:
            self.assertEqual(db.execute('SELECT reaction_result,reaction_applied_sequence FROM developer_inputs').fetchone(),(None,2))
            self.assertEqual(db.execute('SELECT COUNT(*) FROM reaction_stage_claims').fetchone()[0],2)

    async def test_interim_text_is_not_copied_into_result_or_memories(self):
        session,store,events,_,identifier=await self.start()
        await fixtures.until(lambda:any(e['kind']=='reaction' for e in events))
        with store.connect() as db:
            payload=db.execute('SELECT reaction_result FROM developer_inputs').fetchone()[0]
            self.assertNotIn('coffee',payload)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM source_events').fetchone()[0],0)

    async def test_competing_workers_can_reserve_only_one_of_each_stage(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=MemoryStore(Path(tmp)/'state.sqlite3');records=InputRecords(store,'s','mock',reaction_limit=2)
            key,_=records.create(uuid.uuid4().hex,'audio',b'');records.state(key,'simulated')
            def reserve(stage):
                with store.connect() as db:
                    db.execute('BEGIN IMMEDIATE');return store.claim_input_reaction(db,key,stage,uuid.uuid4().hex,time.time())
            results=await asyncio.gather(*(asyncio.to_thread(reserve,stage) for stage in ('partial','partial','final','final')))
            self.assertEqual(sum(results),2)
            with store.connect() as db:
                self.assertEqual(db.execute('SELECT stage,COUNT(*) FROM reaction_stage_claims GROUP BY stage ORDER BY stage').fetchall(),[('final',1),('partial',1)])

    async def test_cancellation_between_reservation_and_child_dispatch_sends_nothing(self):
        session,store,events,_=await self.setup_session();identifier=uuid.uuid4().hex
        await session.start_audio(identifier,'en-US');calls=[]
        async def classify(text):calls.append(text);return {'reaction':'joy'},{}
        worker=session.early;worker.classify=classify
        snapshot={'phase':'partial','text':'I like coffee','key':'i like coffee'}
        key=worker.prepare(snapshot);self.assertIsNotNone(key)
        worker.current=lambda:False
        await worker.evaluate(key,snapshot)
        self.assertFalse(calls);self.assertFalse(any(e['kind']=='reaction' for e in events))
        with store.connect() as db:
            self.assertEqual(db.execute('SELECT state,attempted FROM early_reaction_calls').fetchone(),('cancelled_before_send',0))
            self.assertEqual(db.execute('SELECT COUNT(*) FROM reaction_stage_claims').fetchone()[0],1)


class PolicyMigrationTests(unittest.TestCase):
    def test_existing_claims_and_values_survive_without_an_extra_slot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);legacy=root/'v9';legacy.mkdir()
            for file in MIGRATIONS.glob('*.sql'):
                if int(file.name[:3])<=9:shutil.copy2(file,legacy/file.name)
            path=root/'state.sqlite3';Database(path,migrations=legacy);old=MemoryStore.existing(path,readonly=False)
            records=InputRecords(old,'old','mock');key,_=records.create(uuid.uuid4().hex,'text',b'hello')
            with old.connect() as db:
                db.execute("INSERT INTO input_reaction_claims VALUES(?,'historical',NULL,1)",(key,))
                before=db.execute('SELECT * FROM developer_inputs').fetchall()
            current=MemoryStore(path)
            with current.connect() as db:
                self.assertEqual([row[:-3] for row in db.execute('SELECT * FROM developer_inputs')],before)
                self.assertFalse(current.claim_input_reaction(db,key,'final','new',time.time()))
                self.assertEqual(db.execute('SELECT COUNT(*) FROM reaction_stage_claims').fetchone()[0],0)
                self.assertEqual(db.execute('SELECT reaction_limit,reaction_applied_sequence FROM developer_inputs').fetchone(),(1,0))
            with self.assertRaises(sqlite3.IntegrityError),current.connect() as db:
                db.execute('UPDATE developer_inputs SET reaction_limit=2 WHERE id=?',(key,))

    def test_reopening_input_with_different_setting_cannot_change_its_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=MemoryStore(Path(tmp)/'state.sqlite3');identifier=uuid.uuid4().hex
            one=InputRecords(store,'s','mock',reaction_limit=1);key,_=one.create(identifier,'text',b'hello')
            two=InputRecords(store,'s','mock',reaction_limit=2);self.assertEqual(two.create(identifier,'text',b'hello'),(key,False))
            with store.connect() as db:self.assertEqual(db.execute('SELECT reaction_limit FROM developer_inputs').fetchone()[0],1)

    def test_default_requests_are_two_but_one_remains_supported(self):
        self.assertEqual(ProviderSettings().validate().early_reaction_max_requests,2)


if __name__=='__main__':unittest.main()
