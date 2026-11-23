from legacy_turn import insert_legacy_turn
import asyncio
import json
from pathlib import Path
import shutil
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid

import test_early_reaction as fixtures
from ravora.comments import Comment,CommentInbox
from ravora.conversation import MemoryStore
from ravora.database import Database,MIGRATIONS
from ravora.input_records import InputRecords
from ravora.providers import ProviderError


class SingleReactionTests(unittest.IsolatedAsyncioTestCase):
    async def session(self,**kwargs):
        return await fixtures.StreamingPathTests.setup_session(self,**kwargs)

    def counts(self,store):
        with store.connect() as db:
            return (db.execute('SELECT COUNT(*) FROM early_reaction_calls').fetchone()[0],
                    db.execute("SELECT COUNT(*) FROM provider_calls WHERE provider='jev'").fetchone()[0],
                    db.execute('SELECT COUNT(*) FROM input_reaction_claims').fetchone()[0])

    async def pending(self,*,ignore_cancel=False):
        session,store,events,inputs=await self.session();started=asyncio.Event();release=asyncio.Event()
        identifier=uuid.uuid4().hex;await session.start_audio(identifier,'en-US')
        async def slow(text):
            started.set()
            try:await release.wait()
            except asyncio.CancelledError:
                if not ignore_cancel:raise
                await release.wait()
            return {'reaction':'joy','source':'test'},{}
        session.early.classify=slow
        await session.audio_chunk(identifier,b'\x00\x10'*3200);await asyncio.wait_for(started.wait(),1)
        await session.end_audio(identifier);await asyncio.wait_for(session.task,1)
        self.assertEqual(session.phase,'generated',events);self.assertEqual(inputs,['I like coffee and jazz.'])
        self.assertEqual(self.counts(store),(1,0,1))
        return session,store,events,release

    async def test_pending_partial_survives_stt_completion_without_delaying_audio(self):
        session,store,events,release=await self.pending()
        with store.connect() as db:state=db.execute('SELECT state FROM conversation_turns').fetchone()[0]
        self.assertFalse(any(e['kind']=='reaction' for e in events))
        release.set();await fixtures.until(lambda:any(e['kind']=='reaction' for e in events))
        reaction=next(e for e in events if e['kind']=='reaction')
        self.assertEqual(reaction['phase'],'partial');self.assertEqual(reaction['token'],session.token)
        self.assertEqual(self.counts(store),(1,0,1))
        with store.connect() as db:
            self.assertEqual(db.execute('SELECT state FROM conversation_turns').fetchone()[0],state)
            self.assertEqual(json.loads(db.execute('SELECT reaction FROM conversation_turns').fetchone()[0])['reaction'],'joy')

    async def test_failure_consumes_slot_and_final_text_does_not_retry(self):
        session,store,events,inputs=await self.session();identifier=uuid.uuid4().hex
        await session.start_audio(identifier,'en-US')
        async def fail(text):raise ProviderError('PROVIDER_CONNECTION_FAILED')
        session.early.classify=fail;await session.audio_chunk(identifier,b'\x00\x10'*3200)
        await fixtures.until(lambda:any(e['kind']=='reaction_unavailable' for e in events))
        await session.end_audio(identifier);await asyncio.wait_for(session.task,2)
        self.assertEqual(session.phase,'generated');self.assertEqual(self.counts(store),(1,0,1))
        with store.connect() as db:self.assertEqual(db.execute('SELECT state FROM early_reaction_calls').fetchone()[0],'outcome_unknown')

    async def test_no_eligible_partial_uses_final_fallback_once(self):
        session,store,events,inputs=await self.session();identifier=uuid.uuid4().hex
        async def stt(frames,emit,**kwargs):
            async for _ in frames:
                await emit({'kind':'stt_partial','text':'I like coffee','stability':.1,'segment_final':False})
            return 'I like coffee and jazz.',{}
        session.clients.transcribe_frames=stt
        await session.start_audio(identifier,'en-US');await session.audio_chunk(identifier,b'\x00\x10'*3200)
        await session.end_audio(identifier);await asyncio.wait_for(session.task,2)
        self.assertEqual(inputs,['I like coffee and jazz.']);self.assertEqual(self.counts(store),(0,1,1))
        self.assertEqual([e['phase'] for e in events if e['kind']=='reaction'],['final'])

    async def test_late_result_after_playback_is_discarded(self):
        session,store,events,release=await self.pending(ignore_cancel=True)
        self.assertTrue(session.played(session.token,session.expected_frames))
        self.assertFalse(session.early.active)
        release.set();await asyncio.wait_for(session.early.task,1)
        self.assertFalse(any(e['kind']=='reaction' for e in events));self.assertEqual(self.counts(store),(1,0,1))

    async def test_deleted_source_discards_pending_result(self):
        session,store,events,release=await self.pending()
        with store.connect() as db:store.remove_source(db,session.receipt,time.time())
        release.set();await asyncio.wait_for(session.early.task,1)
        self.assertFalse(any(e['kind']=='reaction' for e in events))
        self.assertFalse(session.played(session.token,session.expected_frames))

    async def test_slow_final_fallback_does_not_delay_text_audio_or_completion(self):
        session,store,events,inputs=await self.session();release=asyncio.Event();started=asyncio.Event()
        async def slow(controller,text):
            started.set();await release.wait()
            return {'reaction':'joy','source':'test'},{}
        with patch('ravora.early_reaction.EarlyReactions._classify',slow):
            await session.start_text(uuid.uuid4().hex,'Hello.')
            await asyncio.wait_for(started.wait(),1);await asyncio.wait_for(session.task,1)
            self.assertEqual(session.phase,'generated');self.assertEqual(self.counts(store),(0,1,1))
            release.set();await asyncio.wait_for(session.early.task,1)
        self.assertEqual([e['phase'] for e in events if e['kind']=='reaction'],['final'])

    def bound_input(self,store,*,legacy=False):
        records=InputRecords(store,'session','mock');key,_=records.create(uuid.uuid4().hex,'text',b'Hello.')
        comment=Comment('developer-mock','chat',uuid.uuid4().hex,'local-developer','Developer','Hello.',time.time())
        inbox=CommentInbox(store);self.assertEqual(inbox.put(comment),'queued');inbox.take();inbox.claimed(comment.receipt)
        turn=uuid.uuid4().hex;self.assertTrue(inbox.bind(comment.receipt,turn))
        if legacy:insert_legacy_turn(store,comment,turn,None,played=False)
        else:store.start(turn,'offline:'+comment.viewer,comment.text)
        records.state(key,'accepted',source=comment.receipt)
        return key,turn

    async def test_competing_final_preparations_share_atomic_claim(self):
        with tempfile.TemporaryDirectory() as directory:
            store=MemoryStore(Path(directory)/'db.sqlite3');key,turn=self.bound_input(store)
            def prepare():return store.prepare_call(turn,'jev','reaction',{},mode='offline',reaction_input_id=key)
            calls=await asyncio.gather(asyncio.to_thread(prepare),asyncio.to_thread(prepare))
            self.assertEqual(sum(value is not None for value in calls),1);self.assertEqual(self.counts(store),(0,1,1))

    async def test_v8_backfills_historical_unknowns_without_refunding(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);migrations=root/'v7';migrations.mkdir()
            for path in MIGRATIONS.glob('*.sql'):
                if int(path.name[:3])<=7:shutil.copy2(path,migrations/path.name)
            path=root/'db.sqlite3';Database(path,migrations=migrations);old=MemoryStore.existing(path,readonly=False)
            records=InputRecords(old,'session','mock');key,_=records.create(uuid.uuid4().hex,'audio',b'')
            with old.connect() as db:
                for sequence in (1,2):
                    db.execute('''INSERT INTO early_reaction_calls
                        (id,input_id,sequence,model,mode,text_sha256,characters,state,attempted,created_at,updated_at)
                        VALUES(?,?,?,'jev-latest','mock','fixture',10,'outcome_unknown',1,1,1)''',(uuid.uuid4().hex,key,sequence))
            other,turn=self.bound_input(old,legacy=True);old.prepare_call(turn,'jev','reaction',{},mode='offline')
            with old.connect() as db:before=db.execute('SELECT * FROM early_reaction_calls ORDER BY sequence').fetchall()
            upgraded=MemoryStore(path)
            with upgraded.connect() as db:
                self.assertEqual(db.execute('SELECT * FROM early_reaction_calls ORDER BY sequence').fetchall(),before)
                self.assertEqual(db.execute('SELECT COUNT(*) FROM input_reaction_claims').fetchone()[0],2)
                self.assertEqual(db.execute('SELECT DISTINCT stage FROM input_reaction_claims').fetchall(),[('historical',)])
            self.assertIsNone(upgraded.prepare_call(turn,'jev','reaction',{},mode='offline',reaction_input_id=other))


if __name__=='__main__':unittest.main()
