import asyncio
from contextlib import asynccontextmanager, redirect_stdout
from dataclasses import replace
import hashlib
import io
import json
import importlib.util
from pathlib import Path
import sys
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch

from ravora.conversation import MemoryStore
from ravora.edge_grpc import EdgeError
from ravora.fast_voice import FastVoiceSession, SpeechEndpoint
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


class EndpointTests(unittest.TestCase):
    def test_stale_end_and_local_resumption_do_not_cut_the_new_clause(self):
        detector = SpeechEndpoint(800)
        detector.activity({'type':'SPEECH_ACTIVITY_END','offset_ms':800})
        detector.activity({'type':'SPEECH_ACTIVITY_BEGIN','offset_ms':1000})
        detector.activity({'type':'SPEECH_ACTIVITY_END','offset_ms':900})
        self.assertIsNone(detector.end_reason(1900))
        detector.activity({'type':'SPEECH_ACTIVITY_END','offset_ms':1600})
        detector.observe_pcm(2300, 1600*1600, 1)
        self.assertIsNone(detector.end_reason(2400))
        self.assertEqual(detector.end_reason(3100), 'speech_activity_end')

    def test_missing_end_needs_both_recognized_text_and_quiet_after_voice(self):
        detector = SpeechEndpoint(800)
        self.assertIsNone(detector.end_reason(5000))
        detector.observe_pcm(5200, 1600*1600, 1)
        self.assertIsNone(detector.end_reason(8000))
        detector.has_text = True
        self.assertIsNone(detector.end_reason(6700))
        self.assertEqual(detector.end_reason(6800), 'transcript_silence')


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
    def dell_stream(self):
        reader = asyncio.StreamReader()
        closed = asyncio.Event()
        @asynccontextmanager
        async def device(name):
            self.assertEqual(name, 'PCH,0')
            try:
                yield reader
            finally:
                closed.set()
        self.enterContext(patch('ravora.fast_voice.open_microphone', device))
        return reader, closed

    async def wait_event(self, kind):
        async with asyncio.timeout(2):
            while not any(event['kind'] == kind for event in self.events):
                await asyncio.sleep(.005)

    async def test_handsfree_browser_waits_locally_and_preserves_onset(self):
        identifier = uuid.uuid4().hex
        await self.session.start_audio(identifier, 'ja-JP', end_mode='handsfree')
        quiet, loud = bytes(640), b'\x00\x10'*320
        for frame in [quiet]*30+[loud]*2+[quiet]*12:
            await self.session.audio_chunk(identifier, frame)
        self.assertEqual(self.session.phase, 'armed')
        self.assertIsNone(self.session.task)
        self.assertIsNone(self.session.records.info(self.session.key)['duration_seconds'])
        await self.session.audio_chunk(identifier, loud*4)
        self.assertEqual(self.session.audio_size, len(quiet)*12)
        self.assertEqual(self.session.hash.hexdigest(), hashlib.sha256(quiet*8+loud*4).hexdigest())
        for frame in [loud*5]*3+[quiet*5]*13:
            await self.session.audio_chunk(identifier, frame)
            await asyncio.sleep(0)
        await asyncio.wait_for(self.session.task, 3)
        self.assertEqual(self.session.phase, 'generated')
        self.assertEqual(sum(e['kind']=='listening' for e in self.events), 1)

    async def test_dell_handsfree_cancel_while_quiet_releases_device_without_stt(self):
        reader, closed = self.dell_stream()
        await self.session.start_audio(uuid.uuid4().hex, 'en-US', dell_device='PCH,0', end_mode='handsfree')
        reader.feed_data(bytes(16000))
        await self.wait_event('armed')
        await self.session.cancel()
        self.assertTrue(closed.is_set())
        self.assertIsNone(self.session.task)
        self.assertFalse(any(e['kind']=='listening' for e in self.events))

    async def test_mute_closes_dell_capture_and_allows_text_and_playback(self):
        reader, closed = self.dell_stream()
        input_id = uuid.uuid4().hex
        await self.session.start_audio(input_id, 'ja-JP', dell_device='PCH,0', end_mode='handsfree')
        reader.feed_data(bytes(16000))
        await self.wait_event('armed')
        await self.session.set_mic_muted(True)
        self.assertTrue(closed.is_set())
        self.assertEqual(self.events[-1]['code'], 'MIC_MUTED')
        await self.session.start_audio(uuid.uuid4().hex, 'en-US')
        await self.session.audio_chunk(input_id, bytes(640))
        await self.session.end_audio(input_id)
        self.assertIsNone(self.session.task)
        self.assertFalse(any(e['kind']=='listening' for e in self.events))
        await self.text('Can we keep talking?')
        await self.session.set_mic_muted(True)
        self.assertTrue(self.session.played(self.session.token, self.session.expected_frames))
        await self.session.set_mic_muted(False)
        await self.session.start_audio(uuid.uuid4().hex, 'ja-JP', end_mode='handsfree')
        self.assertEqual(self.session.phase,'armed')

    async def test_dell_handsfree_noise_without_stt_events_releases_capture_and_keeps_diagnosis(self):
        async def no_result(frames,emit,**kwargs):
            async for _ in frames:pass
            raise EdgeError('STT_NO_FINAL_TRANSCRIPT')
        self.client.transcribe_frames=no_result
        reader,closed=self.dell_stream()
        await self.session.start_audio(uuid.uuid4().hex,'ja-JP',dell_device='PCH,0',end_mode='handsfree')
        key=self.session.key
        for frame in [b'\x00\x10'*1600]*2+[bytes(3200)]*20:
            reader.feed_data(frame)
            await asyncio.sleep(.002)
        await self.wait_event('error')
        self.assertTrue(closed.is_set())
        self.assertEqual(self.session.end_reason,'onset_silence')
        await self.session.cancel()
        row=self.session.records.info(key)
        self.assertEqual((row['input_state'],row['code']),('empty','STT_NO_FINAL_TRANSCRIPT'))
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM conversation_turns').fetchone()[0],0)

    async def test_handsfree_empty_at_duration_limit_does_not_retrigger_on_continuous_sound(self):
        async def no_result(frames,emit,**kwargs):
            async for _ in frames:pass
            raise EdgeError('STT_NO_FINAL_TRANSCRIPT')
        self.client.transcribe_frames=no_result
        self.session.s=replace(self.session.s,stt_max_seconds=1)
        first,second=uuid.uuid4().hex,uuid.uuid4().hex
        await self.session.start_audio(first,'en-US',end_mode='handsfree')
        loud=b'\x00\x10'*1600
        for _ in range(10):
            await self.session.audio_chunk(first,loud)
            await asyncio.sleep(0)
        await asyncio.wait_for(self.session.task,2)
        self.assertEqual(self.session.end_reason,'duration_limit')
        await self.session.start_audio(second,'en-US',end_mode='handsfree')
        for _ in range(10):await self.session.audio_chunk(second,loud)
        self.assertEqual(self.session.phase,'armed')
        self.assertEqual(sum(e['kind']=='listening' for e in self.events),1)

    async def test_early_oversize_waits_for_quiet_before_next_voice(self):
        async def oversize(frames,emit,**kwargs):
            await anext(frames)
            raise EdgeError('STT_TRANSCRIPT_TOO_LONG')
        original=self.client.transcribe_frames
        self.client.transcribe_frames=oversize
        identifier=uuid.uuid4().hex
        await self.session.start_audio(identifier,'en-US',end_mode='handsfree')
        loud=b'\x00\x10'*1600
        await self.session.audio_chunk(identifier,loud)
        await self.wait_event('error')
        await self.session.audio_chunk(identifier,loud)  # Already in flight from the discarded input.
        self.assertFalse(self.pcm)
        self.client.transcribe_frames=original
        second=uuid.uuid4().hex
        await self.session.start_audio(second,'en-US',end_mode='handsfree')
        for _ in range(6):await self.session.audio_chunk(second,loud)
        self.assertEqual(self.session.phase,'armed')
        await self.session.audio_chunk(second,bytes(6400))
        for frame in [loud]*3+[bytes(3200)]*12:
            await self.session.audio_chunk(second,frame)
            await asyncio.sleep(0)
        await asyncio.wait_for(self.session.task,3)
        self.assertEqual(self.session.phase,'generated')
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM conversation_turns').fetchone()[0],1)

    async def test_dell_handsfree_starts_on_voice_and_stops_before_playback(self):
        reader, closed = self.dell_stream()
        await self.session.start_audio(uuid.uuid4().hex, 'en-US', dell_device='PCH,0', end_mode='handsfree')
        reader.feed_data(bytes(16000))
        await self.wait_event('armed')
        for frame in [b'\x00\x10'*1600]*4+[bytes(3200)]*13:
            reader.feed_data(frame)
            await asyncio.sleep(.002)
        await self.wait_event('capture_ended')
        self.assertTrue(closed.is_set())
        await asyncio.wait_for(self.session.task, 3)
        self.assertEqual(self.session.end_reason, 'speech_activity_end')

    async def test_auto_browser_pause_resume_late_packets_and_duplicate_release(self):
        identifier = uuid.uuid4().hex
        await self.session.start_audio(identifier, 'ja-JP', end_mode='auto')
        async def chunks(n, voiced):
            for _ in range(n):
                await self.session.audio_chunk(identifier, (b'\x00\x10' if voiced else b'\0\0')*1600)
                await asyncio.sleep(0)
        await chunks(4, True)
        await chunks(5, False)
        await chunks(3, True)
        self.assertEqual(self.session.phase, 'listening')
        await chunks(10, False)
        await self.wait_event('capture_ended')
        size = self.session.audio_size
        await self.session.audio_chunk(identifier, bytes(3200))
        await self.session.end_audio(identifier)
        self.assertEqual(self.session.audio_size, size)
        await asyncio.wait_for(self.session.task, 3)
        self.assertEqual(self.session.end_reason, 'speech_activity_end')
        self.assertEqual(sum(e['kind']=='turn' for e in self.events), 1)
        self.assertTrue(self.session.played(self.session.token, self.session.expected_frames))

    async def test_auto_cancel_in_pause_does_not_end_next_manual_turn(self):
        first = uuid.uuid4().hex
        await self.session.start_audio(first, 'en-US', end_mode='auto')
        for frame in (b'\x00\x10'*1600, bytes(3200)):
            await self.session.audio_chunk(first, frame)
            await asyncio.sleep(0)
        await self.session.cancel()
        second = uuid.uuid4().hex
        await self.session.start_audio(second, 'en-US')
        for frame in [b'\x00\x10'*1600] + [bytes(3200)]*12:
            await self.session.audio_chunk(second, frame)
            await asyncio.sleep(0)
        self.assertEqual(self.session.phase, 'listening')
        self.assertFalse(any(e['kind']=='capture_ended' for e in self.events))
        await self.session.end_audio(second)
        await asyncio.wait_for(self.session.task, 3)
        self.assertEqual(self.session.end_reason, 'client_end')

    async def test_transcript_without_cloud_end_seals_only_after_longer_pause(self):
        original = self.client.transcribe_frames
        async def without_end(frames, emit, **kwargs):
            async def forward(value):
                if value.get('type') != 'SPEECH_ACTIVITY_END':
                    await emit(value)
            return await original(frames, forward, **kwargs)
        self.client.transcribe_frames = without_end
        identifier = uuid.uuid4().hex
        await self.session.start_audio(identifier, 'en-US', end_mode='auto')
        for frame in [b'\x00\x10'*1600]*3 + [bytes(3200)]*14:
            await self.session.audio_chunk(identifier, frame)
            await asyncio.sleep(0)
        self.assertFalse(self.session.audio_end)
        for _ in range(3):
            await self.session.audio_chunk(identifier, bytes(3200))
            await asyncio.sleep(0)
        await asyncio.wait_for(self.session.task, 3)
        self.assertEqual(self.session.end_reason, 'transcript_silence')
        self.assertEqual(self.session.phase, 'generated')

    async def test_auto_dell_stops_reader_and_seals_once(self):
        reader, closed = self.dell_stream()
        identifier = uuid.uuid4().hex
        await self.session.start_audio(identifier, 'ja-JP', dell_device='PCH,0', end_mode='auto')
        for frame in [b'\x00\x10'*1600]*3 + [bytes(3200)]*12:
            reader.feed_data(frame)
            await asyncio.sleep(.002)
        await self.wait_event('capture_ended')
        self.assertTrue(closed.is_set())
        await self.session.end_audio(identifier)
        await asyncio.wait_for(self.session.task, 3)
        self.assertEqual(self.session.end_reason, 'speech_activity_end')

    async def test_auto_silence_at_cap_cancels_without_turn_on_unaligned_chunks(self):
        self.session.s = replace(self.session.s, stt_max_seconds=1)
        identifier = uuid.uuid4().hex
        await self.session.start_audio(identifier, 'en-US', end_mode='auto')
        for _ in range(6):
            await self.session.audio_chunk(identifier, bytes(5760))
            await asyncio.sleep(0)
        self.assertEqual(self.session.audio_size, 32000)
        self.assertEqual(self.session.phase, 'cancelled')
        self.assertFalse(any(e['kind']=='turn' for e in self.events))
        self.assertTrue(any(e.get('code')=='NO_SPEECH' for e in self.events))

    async def test_dell_pcm_streams_before_release_and_flushes_short_tail(self):
        reader, closed = self.dell_stream()
        input_id = uuid.uuid4().hex
        await self.session.start_audio(input_id, 'ja-JP', dell_device='PCH,0')
        pcm = b'\x00\x10' * 3617
        reader.feed_data(pcm)
        await self.wait_event('stt_partial')
        with self.assertRaisesRegex(EdgeError, 'MIC_SOURCE_MISMATCH'):
            await self.session.audio_chunk(input_id, b'\x00\x10')
        await self.session.end_audio(input_id)
        self.assertTrue(closed.is_set())
        await asyncio.wait_for(self.session.task, 3)
        self.assertEqual(self.session.hash.digest(), hashlib.sha256(pcm).digest())
        self.assertEqual(self.session.phase, 'generated')
        self.assertTrue(self.session.played(self.session.token, self.session.expected_frames))

    async def test_dell_device_failure_before_first_sample_never_dispatches_stt(self):
        reader, closed = self.dell_stream()
        reader.feed_eof()
        await self.session.start_audio(uuid.uuid4().hex, 'en-US', dell_device='PCH,0')
        await asyncio.wait_for(self.session.capture_task, 1)
        self.assertTrue(closed.is_set())
        self.assertIsNone(self.session.task)
        self.assertIn('DELL_MIC_UNAVAILABLE', [event.get('code') for event in self.events])
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT request_attempted FROM developer_inputs').fetchone(), (0,))

    async def test_disconnect_while_device_is_opening(self):
        reader, closed = self.dell_stream()
        await self.session.start_audio(uuid.uuid4().hex, 'en-US', dell_device='PCH,0')
        await asyncio.sleep(0)
        await self.session.close()
        self.assertTrue(closed.is_set())
        self.assertIsNone(self.session.task)
        self.assertNotIn('listening', [event['kind'] for event in self.events])

    async def test_dell_limit_without_release_and_late_release(self):
        reader, closed = self.dell_stream()
        self.session.s = replace(self.session.s, stt_max_seconds=1)
        input_id = uuid.uuid4().hex
        reader.feed_data(b'\x00\x10' * 18000)
        await self.session.start_audio(input_id, 'en-US', dell_device='PCH,0')
        await self.wait_event('capture_ended')
        await self.session.end_audio(input_id)
        self.assertTrue(closed.is_set())
        self.assertEqual(self.session.audio_size, 32000)
        await asyncio.wait_for(self.session.task, 3)
        self.assertEqual(self.session.phase, 'generated')

    async def test_provider_failure_releases_dell_microphone_without_browser(self):
        reader, closed = self.dell_stream()
        async def unavailable(*args, **kwargs):
            raise EdgeError('PROVIDER_CONNECTION_FAILED')
        self.client.transcribe_frames = unavailable
        reader.feed_data(b'\x00\x10' * 640)
        await self.session.start_audio(uuid.uuid4().hex, 'en-US', dell_device='PCH,0')
        await self.wait_event('error')
        self.assertTrue(closed.is_set())
        self.assertEqual(self.session.phase, 'failed')
        self.assertFalse(self.pcm)

    async def test_audio_stream_is_finished_before_optional_phone_alignment(self):
        def align(text, pcm):
            end = next(e for e in self.events if e['kind'] == 'audio_end')
            self.assertEqual(end['frames'], len(pcm) // 2)
            return {'status':'word_alignment_incomplete'}
        self.session.phone_aligner = align
        await self.text('Hello.')
        messages = [e['kind'] for e in self.events]
        self.assertLess(messages.index('audio_end'), messages.index('phonemes'))
        self.assertLess(messages.index('phonemes'), messages.index('complete'))
        self.assertEqual(next(e['status'] for e in self.events if e['kind']=='phonemes'),
                         'word_alignment_incomplete')

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
    async def test_only_validated_speech_reaches_synthesis(self):
        from ravora.public_output import public_fixture
        wire=public_fixture('Let me think.',expression='thinking',motion='nod')
        async def generate(text,**kwargs):
            self.assertIn('RAY_PUBLIC_OUTPUT_V1',kwargs['prompt'])
            yield wire[:9]
            yield wire[9:]
        generate.ray_public_output=True
        self.client.generate=generate
        spoken=[]
        async def synthesize(texts):
            async for sentence in texts:spoken.append(sentence)
            yield bytes(4800)
        self.client.synthesize_stream=synthesize
        await self.text('Read out the entire private object.')
        self.assertEqual(spoken,['Let me think.'])
        actions=[e for e in self.events if e['kind']=='character_action']
        self.assertEqual([(e['expression'],e['motion']) for e in actions],[('thinking','nod')])
        self.assertEqual([e['text'] for e in self.events if e['kind']=='text'],spoken)
        self.assertTrue(self.session.played(self.session.token,self.session.expected_frames))
        for invalid in (wire+' trailing',wire.replace('"nod"','"unknown"'),wire.replace('"motion":','"extra":1,"motion":')):
            wire=invalid
            self.events.clear()
            await self.session.start_text(uuid.uuid4().hex,'Hello.')
            await self.session.task
            self.assertEqual(self.session.phase,'failed')
            self.assertFalse(any(e['kind'] in ('character_action','text') for e in self.events))
            self.assertEqual(spoken,['Let me think.'])

    async def test_incomplete_generation_does_not_acknowledge_partial_audio(self):
        async def fail(text,**_):
            yield 'Hello. '
            raise EdgeError('INCOMPLETE_GENERATION')
        self.client.generate=fail
        await self.session.start_text(uuid.uuid4().hex,'Hello.');await asyncio.wait_for(self.session.task,3)
        self.assertEqual(self.session.phase,'failed');self.assertFalse(self.session.played(self.session.token,self.session.expected_frames))
        self.assertFalse(any(x['kind']=='complete' for x in self.events))


if __name__=='__main__':unittest.main()
