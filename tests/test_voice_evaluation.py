import asyncio
from dataclasses import replace
import hashlib
import io
import json
import math
from pathlib import Path
import stat
import sys
import tempfile
import time
import unittest
import uuid
import wave
from unittest.mock import patch

from ravora.conversation import MemoryStore
from ravora.fast_voice import FastVoiceSession
from ravora.fast_voice_server import FastVoiceServer, MockVoiceClients
from ravora.settings import ProviderSettings
from ravora.voice_evaluation import EvaluationConfig, EvaluationError, VoiceEvaluation, error_counts, percentile
from contextlib import redirect_stdout


class RecorderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.recorder = VoiceEvaluation(self.root/'evaluation', EvaluationConfig(), mode='mock')
        self.addCleanup(self.recorder.close)
        self.settings = ProviderSettings()

    def start(self, **kwargs):
        key = hashlib.sha256(uuid.uuid4().bytes).hexdigest()
        self.recorder.start(key, uuid.uuid4().hex, 'session', source='dell', end_mode='auto', language='en-US', settings=self.settings)
        if kwargs: self.recorder.update(key, **kwargs)
        return key

    def test_exact_pcm_survives_fragmentation_cancel_and_report_escaping(self):
        key = self.start()
        pcm = bytes(640) + (3000).to_bytes(2,'little')*640 + bytes(3200)
        for i in range(0,len(pcm),318): self.recorder.pcm(key,pcm[i:i+318])
        self.recorder.end(key,'DEVELOPER_CANCELLED')
        self.recorder.update(key,outcome='cancelled',transcript='<script>alert(1)</script>')
        self.recorder.close()
        row = self.recorder.rows[key]
        with wave.open(str(self.recorder.root/row['audio_file'])) as reader:
            self.assertEqual((reader.getnchannels(),reader.getsampwidth(),reader.getframerate()),(1,2,16000))
            self.assertEqual(reader.readframes(reader.getnframes()),pcm)
        self.assertEqual(row['last_voice_pcm_ms'],60)
        self.assertEqual(len(row['audio_sha256']),64)
        self.assertEqual(row['pcm_sha256'],hashlib.sha256(pcm).hexdigest())
        self.assertEqual(stat.S_IMODE((self.recorder.root/row['audio_file']).stat().st_mode),0o640)
        self.assertEqual(stat.S_IMODE(self.recorder.root.stat().st_mode),0o2770)
        self.assertIn('&lt;script&gt;', (self.recorder.root/'review.html').read_text())
        self.assertNotIn('<script>', (self.recorder.root/'review.html').read_text())
        self.assertEqual(json.loads((self.recorder.root/'summary.json').read_text())['outcomes'],{'cancelled':1})

    def test_percentiles_count_missing_and_do_not_mix_conditions_or_failed_turns(self):
        for gap,state in [(100,'played'),(300,'played'),(None,'played'),(9000,'failed')]:
            key=self.start(outcome=state,browser={'input_end_to_audio_ms':gap})
        self.start(outcome='played',input_source='windows',browser={'input_end_to_audio_ms':900})
        groups=self.recorder.snapshot()['summary']['cohorts']
        self.assertEqual(len(groups),2)
        metric=groups[0]['latency']['input_end_to_audio_ms']
        self.assertEqual(metric,{'n':2,'missing':1,'p50':100,'p95':300})
        self.assertEqual(groups[0]['false_endpoint'],{'reviewed':0,'unreviewed':4,'errors':0,'rate':None})
        self.assertIsNone(percentile([], .95))

    def test_reference_annotations_score_actual_edits_and_leave_unknowns_unknown(self):
        key=self.start(transcript='I like tea.')
        self.recorder.annotate(key,{'reference':'I like coffee.','stt_error':True,'false_endpoint':None,'note':'proper noun'})
        group=self.recorder.snapshot()['summary']['cohorts'][0]
        self.assertEqual(group['transcription']['word_edits'],1)
        self.assertAlmostEqual(group['transcription']['wer'],1/3)
        self.assertEqual(group['false_endpoint']['unreviewed'],1)
        self.assertEqual(error_counts({'reference':'今日は晴れ。','transcript':'今日は雨。'})['character_edits'],2)
        self.assertEqual(error_counts({'reference':'hello','transcript':None})['word_edits'],1)
        with self.assertRaises(EvaluationError):
            self.recorder.annotate(key,{'reference':None,'stt_error':'false','false_endpoint':False,'note':''})

    def test_fifty_played_inputs_close_collection_but_keep_last_input_and_failures(self):
        for _ in range(50):
            key=self.start(outcome='played')
        self.assertTrue(self.recorder.snapshot()['summary']['target_reached'])
        self.start()
        self.assertEqual(len(self.recorder.rows),50)
        self.assertEqual(self.recorder.limit,'max_turns')

    def test_storage_limit_keeps_valid_partial_wave_and_never_saves_more_audio(self):
        self.recorder.config=replace(self.recorder.config,max_audio_mib=1)
        key=self.start()
        for _ in range(170):self.recorder.pcm(key,b'\x00\x10'*3200)
        row=self.recorder.rows[key]
        self.assertTrue(row['audio_truncated'])
        self.assertLessEqual(sum(p.stat().st_size for p in self.recorder.root.glob('*.wav')),1024**2)
        with wave.open(str(self.recorder.root/row['audio_file'])) as reader:
            self.assertEqual(reader.getnframes()*2,row['pcm_bytes'])

    def test_invalid_and_duplicate_timing_do_not_enter_percentiles(self):
        key=self.start(turn_id='turn')
        value={'input_end_to_audio_ms':100,'stt_final_to_audio_ms':80,'signal_server_ms':1000,'sync_uncertainty_ms':2,'underflow_frames':0}
        with self.assertRaises(EvaluationError):self.recorder.browser(key,{'turn_id':'other'},value)
        with self.assertRaises(EvaluationError):self.recorder.browser(key,{'turn_id':'turn'},{**value,'input_end_to_audio_ms':math.nan})
        self.recorder.browser(key,{'turn_id':'turn'},value)
        with self.assertRaises(EvaluationError):self.recorder.browser(key,{'turn_id':'turn'},value)

    def test_write_error_is_visible_and_does_not_escape_to_conversation(self):
        key=self.start()
        with patch('ravora.voice_evaluation.os.open',side_effect=OSError('disk full')):
            self.assertIsNone(self.recorder.call('pcm',key,b'\0\0'*320))
        self.assertTrue(self.recorder.snapshot()['summary']['errors'])

    def test_late_cleanup_does_not_downgrade_an_accepted_playback(self):
        key=self.start(outcome='played')
        self.recorder.update(key,outcome='failed',error='TURN_CANCELLED')
        self.recorder.close()
        self.assertEqual(self.recorder.rows[key]['outcome'],'played')
        self.assertNotIn('error',self.recorder.rows[key])

    def test_invalid_config_is_rejected(self):
        for value in ({'enabled':'yes'},{'max_inputs':20},{'max_audio_mib':999},{'unknown':True}):
            with self.assertRaises(EvaluationError):EvaluationConfig.parse(value)

    def test_installed_review_command_reopens_a_closed_run_and_preserves_unknowns(self):
        from ravora.voice_evaluation import main,load_evaluation
        key=self.start(transcript='I like tea.')
        with self.assertRaises(EvaluationError):load_evaluation(self.recorder.root)
        self.recorder.close()
        with patch.object(sys,'argv',['ravora-voice-evaluation','annotate',str(self.recorder.root),'--id',key,
                                     '--reference','I like coffee.','--stt-error','yes']),redirect_stdout(io.StringIO()):
            main()
        saved=json.loads((self.recorder.root/'turns.json').read_text())[0]
        self.assertTrue(saved['stt_error'])
        self.assertIsNone(saved['false_endpoint'])


class EvaluationPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.store=MemoryStore(self.root/'conversation.sqlite3')
        self.recorder=VoiceEvaluation(self.root/'evaluation',EvaluationConfig(provenance='synthetic_fixture'),mode='mock')
        self.events=[]
        async def send(value):self.events.append(value)
        async def pcm(token,data):pass
        self.session=FastVoiceSession(ProviderSettings(memory_enabled=False),self.store,self.root,MockVoiceClients(),send,pcm,mock=True,evaluation=self.recorder)
        self.addAsyncCleanup(self.session.close);self.addCleanup(self.recorder.close)

    async def complete_input(self):
        identifier=uuid.uuid4().hex
        await self.session.start_audio(identifier,'en-US')
        for _ in range(3):
            await self.session.audio_chunk(identifier,b'\x00\x10'*1600)
            await asyncio.sleep(0)
        await self.session.end_audio(identifier)
        await asyncio.wait_for(self.session.task,5)
        return self.session.key

    async def test_normal_input_to_browser_ack_and_cancellation_keep_separate_evidence(self):
        key=await self.complete_input()
        row=self.recorder.rows[key]
        now=time.monotonic()*1000
        self.recorder.browser(key,self.session.token,{'input_end_to_audio_ms':125,'stt_final_to_audio_ms':110,
            'signal_server_ms':now,'sync_uncertainty_ms':1,'underflow_frames':0})
        self.assertTrue(self.session.played(self.session.token,self.session.expected_frames))
        self.assertEqual(row['outcome'],'played')
        self.assertIsNotNone(row['browser']['last_voice_to_audio_ms'])
        self.assertEqual(row['transcript'],'I like coffee and jazz.')
        await self.session.start_audio(uuid.uuid4().hex,'en-US')
        self.session.accept_pcm(self.session.input_id,b'\x00\x10'*1600,source='windows')
        cancelled=self.session.key
        await self.session.cancel()
        self.assertEqual(self.recorder.rows[cancelled]['outcome'],'cancelled')
        self.assertTrue((self.recorder.root/(cancelled+'.wav')).is_file())
        self.assertEqual(self.recorder.rows[key]['outcome'],'played')

    async def test_failure_and_disconnect_keep_wave_without_claiming_playback(self):
        identifier=uuid.uuid4().hex
        await self.session.start_audio(identifier,'en-US')
        await self.session.audio_chunk(identifier,b'\x00\x10'*1600)
        key=self.session.key
        await self.session.close()
        self.assertEqual(self.recorder.rows[key]['outcome'],'interrupted')
        self.assertTrue(self.recorder.rows[key]['capture_closed'])

    async def test_authenticated_websocket_review_and_audio_require_access(self):
        from websockets.asyncio.client import connect
        server=await FastVoiceServer(ProviderSettings(memory_enabled=False),self.store,self.root,mode='mock',evaluation=self.recorder).start()
        self.addAsyncCleanup(server.close)
        async with connect(f'ws://127.0.0.1:{server.ws_port}/voice?token={server.secret}',origin=server.origin) as ws:
            self.assertTrue(json.loads(await ws.recv())['config']['evaluation_enabled'])
            await ws.send(json.dumps({'kind':'evaluation_annotate','id':[], 'fields':{}}))
            self.assertEqual(json.loads(await ws.recv())['kind'],'evaluation_error')
            await ws.send(json.dumps({'kind':'evaluation_snapshot'}))
            self.assertEqual(json.loads(await ws.recv())['kind'],'evaluation_snapshot')
            await ws.send(json.dumps({'kind':'evaluation_sync','sequence':0}))
            self.assertGreater(json.loads(await ws.recv())['server_ms'],0)
        reader,writer=await asyncio.open_connection('127.0.0.1',server.port)
        writer.write(f'GET /evaluation/audio/{"a"*64}.wav HTTP/1.1\r\nHost: 127.0.0.1:{server.port}\r\n\r\n'.encode());await writer.drain()
        self.assertIn(b'401 Unauthorized',await reader.read())
        writer.close();await writer.wait_closed()

    async def test_normal_startup_config_and_sigterm_finalize_the_evaluation(self):
        for enabled in (True,False):
            run=self.root/('enabled' if enabled else 'disabled');run.mkdir()
            code=("import asyncio,json;from pathlib import Path;from ravora.conversation import MemoryStore;"
                  "from ravora.fast_voice_server import serve;from ravora.settings import ProviderSettings;"
                  f"p=Path({str(run)!r});"
                  "asyncio.run(serve(ProviderSettings(),MemoryStore(p/'db.sqlite3'),p,mode='mock',port=0,seconds=20,"
                  "write_json=lambda p,v:p.write_text(json.dumps(v)),default_input='windows',dell_device=None,"
                  f"evaluation_config={{'enabled':{enabled!r}}}))")
            child=await asyncio.create_subprocess_exec(sys.executable,'-B','-c',code,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
            try:
                async with asyncio.timeout(5):
                    while not (run/'server.json').exists():await asyncio.sleep(.03)
                child.terminate()
                out,err=await asyncio.wait_for(child.communicate(),5)
                self.assertEqual(child.returncode,0,(out,err))
                self.assertTrue((run/'server-stopped.json').is_file())
                self.assertEqual((run/'evaluation').exists(),enabled)
                if enabled:self.assertTrue(json.loads((run/'evaluation/summary.json').read_text())['closed'])
            finally:
                if child.returncode is None:child.kill();await child.wait()

    async def test_public_console_retains_only_the_evaluation_after_shutdown(self):
        from ravora.case_study import console
        from ravora.edge_grpc import EdgeClients
        destination=self.root/'retained-evaluation'
        parent=self.root/'temporary-sessions';parent.mkdir()
        printed=io.StringIO()
        with patch('tempfile.tempdir',str(parent)),redirect_stdout(printed), \
                patch.object(EdgeClients,'open',side_effect=AssertionError('LIVE_PROVIDER_FORBIDDEN')):
            task=asyncio.create_task(console(0,20,evaluation_dir=destination))
            try:
                async with asyncio.timeout(5):
                    while 'Open:' not in printed.getvalue():await asyncio.sleep(.03)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):await task
            finally:
                if not task.done():task.cancel();await asyncio.gather(task,return_exceptions=True)
        self.assertTrue(json.loads((destination/'summary.json').read_text())['closed'])
        self.assertEqual(list(parent.iterdir()),[])


if __name__=='__main__':unittest.main()
