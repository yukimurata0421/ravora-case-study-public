import asyncio
from dataclasses import replace
import importlib.util
from pathlib import Path
import tempfile
import time
import unittest
import uuid

from ravora.conversation import MemoryStore
from ravora.edge_grpc import EdgeError
from ravora.fast_voice import FastVoiceSession
from ravora.fast_voice_server import MockVoiceClients
from ravora.settings import ProviderSettings,SettingsError,load_settings


class SettingsTests(unittest.TestCase):
    def test_cloud_default_remains_disabled_until_explicitly_enabled(self):
        s=ProviderSettings().validate()
        self.assertEqual(s.voice_profile,"fast_streaming")
        self.assertEqual((s.voice_stt_region,s.voice_stt_model),('asia-northeast1','long'))
        self.assertFalse(s.api_enabled);self.assertFalse(s.stt_api_enabled)
        with self.assertRaises(SettingsError):replace(s,voice_jitter_buffer_ms=0).validate()
    def test_env_profile_resolution(self):
        s=load_settings(Path('/nonexistent'),environ={'RAVORA_VOICE_PROFILE':'legacy','RAVORA_VOICE_INPUT_CHUNK_MS':'80'})
        self.assertEqual((s.voice_profile,s.voice_input_chunk_ms),('legacy',80))


class FastVoiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.store=MemoryStore(self.root/'memory.sqlite3');self.events=[];self.pcm=[];self.client=MockVoiceClients()
        async def send(value):self.events.append(value)
        async def pcm(token,data):self.pcm.append((token,data))
        self.session=FastVoiceSession(ProviderSettings(memory_extractor='local_rules'),self.store,self.root,self.client,send,pcm,mock=True)
        self.addAsyncCleanup(self.session.close)
    async def text(self,value):
        await self.session.start_text(uuid.uuid4().hex,value);await asyncio.wait_for(self.session.task,4)
        self.assertEqual(self.session.phase,'generated',self.events)
    async def test_playback_ack_then_memory_recall_and_close_preserves_completed(self):
        await self.text('I like coffee and jazz.');token=self.session.token
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM memory_jobs').fetchone()[0],0)
        self.assertFalse(self.session.played(token,self.session.expected_frames-1))
        self.assertTrue(self.session.played(token,self.session.expected_frames))
        self.assertFalse(self.session.played(token,self.session.expected_frames))
        self.assertTrue(await asyncio.to_thread(self.session.memory.wait_for,self.session.viewer))
        await self.text('Do you remember my favorite drink?')
        with self.store.connect() as db:
            self.assertIn('coffee',db.execute('SELECT reply FROM conversation_turns ORDER BY rowid DESC LIMIT 1').fetchone()[0])
            self.assertGreater(db.execute('SELECT COUNT(*) FROM memory_retrievals WHERE context_prepared=1').fetchone()[0],0)
        self.assertTrue(self.session.played(self.session.token,self.session.expected_frames))
        await self.session.cancel();await self.session.cancel()
        await self.session.close()
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM conversation_turns WHERE status='completed' AND played=1").fetchone()[0],2)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM developer_inputs WHERE input_state='cancelled'").fetchone()[0],0)
            self.assertEqual(db.execute('SELECT SUM(attempted) FROM provider_calls').fetchone()[0],0)
    async def test_live_audio_consumed_before_release_and_stale_input_rejected(self):
        first=asyncio.Event();original=self.client.transcribe_frames
        async def transcribe(frames,emit,**kw):
            async def checked():
                async for frame in frames:first.set();yield frame
            return await original(checked(),emit,**kw)
        self.client.transcribe_frames=transcribe;identifier=uuid.uuid4().hex
        await self.session.start_audio(identifier,'en-US');await self.session.audio_chunk(identifier,b'\x00\x10'*3200)
        await asyncio.wait_for(first.wait(),1);self.assertEqual(self.session.phase,'listening')
        with self.assertRaises(EdgeError):await self.session.audio_chunk('f'*32,b'\x00\x10'*10)
        await self.session.end_audio(identifier);await asyncio.wait_for(self.session.task,4)
        self.assertEqual(self.session.phase,'generated',self.events)
        await self.session.cancel();self.assertFalse(self.session.played(self.session.token,self.session.expected_frames))
    async def test_cancel_stops_pcm_and_never_saves_memory_or_replays_input(self):
        gate=asyncio.Event();identifier=uuid.uuid4().hex
        async def slow(texts):
            async for _ in texts:
                yield b'\x00\x10'*2400;gate.set();await asyncio.sleep(3)
        self.client.synthesize_stream=slow
        await self.session.start_text(identifier,'Hello.');await asyncio.wait_for(gate.wait(),2)
        token=self.session.token;await self.session.cancel();count=len(self.pcm);await asyncio.sleep(.05)
        self.assertEqual(count,len(self.pcm));self.assertFalse(self.session.current(token))
        self.assertFalse(self.session.played(token,count*2400))
        with self.assertRaises(EdgeError):await self.session.start_text(identifier,'Hello.')
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT played,status FROM conversation_turns').fetchone(),(0,'cancelled'))
            self.assertEqual(db.execute('SELECT COUNT(*) FROM memory_jobs').fetchone()[0],0)
    async def test_new_input_cancel_does_not_cancel_prior_played_turn(self):
        await self.text('Hello.');self.assertTrue(self.session.played(self.session.token,self.session.expected_frames))
        await self.session.start_audio(uuid.uuid4().hex,'en-US');await self.session.cancel()
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT played,status FROM conversation_turns').fetchone(),(1,'completed'))
    async def test_deleted_source_stops_stream_and_cannot_create_memory(self):
        async def remove(texts):
            async for _ in texts:
                with self.store.connect() as db:self.store.remove_source(db,self.session.receipt,time.time())
                yield b'\x00\x10'*2400
        self.client.synthesize_stream=remove
        await self.session.start_text(uuid.uuid4().hex,'I like coffee.');await asyncio.wait_for(self.session.task,3)
        self.assertEqual(self.session.phase,'failed');self.assertFalse(self.pcm)
        self.assertFalse(self.session.played(self.session.token,self.session.expected_frames))
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM memory_jobs').fetchone()[0],0)
    async def test_incomplete_generation_does_not_acknowledge_partial_audio(self):
        async def fail(text,**_):
            yield 'Hello. '
            raise EdgeError('INCOMPLETE_GENERATION')
        self.client.generate=fail
        await self.session.start_text(uuid.uuid4().hex,'Hello.');await asyncio.wait_for(self.session.task,3)
        self.assertEqual(self.session.phase,'failed');self.assertFalse(self.session.played(self.session.token,self.session.expected_frames))
        self.assertFalse(any(x['kind']=='complete' for x in self.events))


if __name__=='__main__':unittest.main()
