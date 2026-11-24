from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import tempfile
import time
import unittest
from unittest.mock import patch

from ravora.conversation import MemoryStore
from ravora.input_pipeline import InputPipeline
from ravora.ray_character import CharacterRuntime
from ravora.settings import ProviderSettings
from ravora.studio_observations import StudioObservations
try:
    from ravora.twitch_chat import MESSAGE, TwitchReader
except ModuleNotFoundError as exc:
    if exc.name not in ('websocket','requests'):
        raise
    raise unittest.SkipTest('Twitch extra required') from exc


class ScheduledAudienceTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.store = MemoryStore(Path(directory)/'fixture.db')
        self.now = time.time()
        self.mono = 0.
        self.ray = CharacterRuntime(clock=lambda: self.mono, idle_seconds=120, max_initiatives=-1)
        bridge = SimpleNamespace(worker=None, controller=SimpleNamespace(character=self.ray))
        self.pipeline = InputPipeline(replace(ProviderSettings(), comment_source='twitch'), self.store, bridge, [])
        self.pipeline.inbox.clock = lambda: self.now
        self.obs = StudioObservations(self.pipeline.inbox,'twitch',True,'twitch','stream',sample_rate=48000,fps=30)
        self.addCleanup(self.obs.close,0,'reading')
        self.pipeline.audience_present = self.obs.has_audience
        self.pipeline.audience_size = self.obs.audience_size

    def read(self):
        with patch('ravora.studio_observations.time.time',return_value=self.now):
            return self.pipeline.next(allow_host_input=True)

    def advance(self, elapsed):
        self.now += elapsed
        self.mono += elapsed

    def test_manual_owner_chat_beats_zero_and_does_not_wait_for_positive_sample(self):
        self.obs.audience(self.now,'live','stream',0)
        self.advance(130)
        self.assertFalse(self.read()['available'])
        self.assertEqual(self.ray.initiatives,0)
        reader = TwitchReader(self.pipeline.inbox, None, config={}, is_announcement=lambda key,_:key=='auto')
        for msg in ('auto','manual'):
            reader._notification({'subscription_type':MESSAGE,
                                  'message_timestamp':datetime.fromtimestamp(self.now,timezone.utc).isoformat()},
                                 {'event':{'broadcaster_user_id':'42','chatter_user_id':'42','chatter_user_name':'Owner',
                                           'message_id':msg,'message':{'text':'こんにちは'}}},'42')
        self.assertEqual(reader.counts['ignored_welcome'],1)
        choice = self.read()
        self.assertTrue(choice['available'])
        self.assertEqual(choice['comment']['source'],'twitch')
        self.assertEqual(self.ray.initiatives,0)

    def test_single_listener_backoff_resets_only_on_human_activity(self):
        def probe():
            self.obs.audience(self.now,'live','stream',1)
            result = self.read()
            if result['available']:
                self.pipeline.inbox.finish(result['comment']['receipt'],'cancelled')
            return result['available']
        self.assertTrue(probe())
        self.assertTrue(self.pipeline.followup_claim[1].greeting)
        for spacing in (120, 240, 480, 600, 600):
            self.advance(spacing-1)
            self.assertFalse(probe())
            self.advance(1)
            self.assertTrue(probe())
            self.assertFalse(self.pipeline.followup_claim[1].greeting)
        self.ray.note_mic_activity()  # Open mic without verified speech.
        self.assertGreaterEqual(self.ray.unanswered_openings,4)
        self.ray.note_mic_activity(speaking=True)
        self.assertEqual(self.ray.unanswered_openings,0)
        self.advance(120)
        self.assertTrue(probe())
        self.ray.begin('new','Hello',viewer='human')
        self.assertEqual(self.ray.unanswered_openings,0)

    def test_delayed_sample_does_not_reopen_an_empty_or_conflicted_room(self):
        self.obs.audience(self.now,'live','stream',0)
        self.obs.audience(self.now-2,'live','stream',3)
        self.assertFalse(self.read()['available'])
        self.advance(1)
        self.obs.audience(self.now,'live','stream',2)
        self.obs.audience(self.now-.5,'live','different',1)
        self.assertFalse(self.read()['available'])

    def test_positive_allows_one_opening_then_waits_and_freshness_is_stream_bound(self):
        self.obs.audience(self.now,'live','stream',1)
        first = self.read()['comment']
        self.assertEqual(first['source'],'ray-initiative')
        self.pipeline.inbox.finish(first['receipt'],'cancelled')
        self.advance(119)
        self.assertFalse(self.read()['available'])
        self.advance(1)
        more = self.read()['comment']
        self.assertEqual(more['source'],'ray-initiative')
        self.pipeline.inbox.finish(more['receipt'],'cancelled')
        self.advance(151)
        self.assertFalse(self.read()['available'])
        self.obs.audience(self.now,'unavailable')
        self.advance(1)
        self.assertFalse(self.read()['available'])
        self.obs.audience(self.now,'live','other',5)
        self.assertFalse(self.read()['available'])
        self.advance(1)
        self.obs.audience(self.now,'live','stream',10)
        self.assertFalse(self.read()['available'])


class AudiencePlayoutTests(unittest.TestCase):
    def test_zero_cancels_unheard_opening_but_owner_reply_reaches_encoded_audio(self):
        import threading
        from contextlib import ExitStack
        from test_studio_replay import FixtureReply, Tone, av
        if av is None:
            self.skipTest('live media dependencies required')
        import numpy as np
        from ravora.conversation import ConversationBridge
        from ravora.live_media import ProgramWriter
        from ravora.live_program import LiveProgram
        from ravora.providers import FixtureReaction
        from ravora.safety import TurnFence

        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root = Path(directory)
            store = MemoryStore(root/'studio.db')
            settings = replace(ProviderSettings(),comment_source='twitch',memory_enabled=False,
                               character_max_initiatives=-1,character_idle_seconds=120)
            bridge = ConversationBridge(settings, store, root, TurnFence(), threading.RLock(),
                                        providers=(FixtureReply(),FixtureReaction(),Tone()))
            pipe = InputPipeline(settings,store,bridge,[])
            program = LiveProgram(pipe,root,prefetch=True,ray_reverb_mix=0)
            stack.callback(program.close)
            observations = program.observe('twitch','stream','fixture')
            pipe.audience_present = observations.has_audience
            observations.audience(time.time(),'live','stream',1)
            program.next_frame()
            self.assertTrue(program.initiative_waiting)
            observations.audience(time.time(),'live','stream',0)
            program.next_frame()
            self.assertIsNone(program.token)
            bridge.worker.join(3)
            reader = TwitchReader(pipe.inbox,None,config={})
            reader._notification({'subscription_type':MESSAGE,'message_timestamp':datetime.now(timezone.utc).isoformat()},
                                 {'event':{'broadcaster_user_id':'42','chatter_user_id':'42','chatter_user_name':'Owner',
                                           'message_id':'manual','message':{'text':'Hello Ray'}}},'42')
            frame = av.VideoFrame.from_ndarray(np.zeros((96,160,3),dtype='uint8'),format='rgb24')
            scene = SimpleNamespace(render=lambda stage:frame.reformat(format='yuv420p'))
            stack.enter_context(patch('ravora.live_media.SIZE',(160,96)))
            output = root/'owner.flv'
            writer = ProgramWriter(output,scene,video_encoder='libx264')
            try:
                for _ in range(150):
                    stage, pcm = program.next_frame()
                    writer.write(stage,pcm)
                    program.encoded(stage)
                    if program.completed:break
                    time.sleep(.015)
            finally:
                writer.close()
            self.assertEqual(program.completed,1)
            with av.open(str(output)) as recording:
                self.assertTrue(any(np.max(np.abs(f.to_ndarray()))>.01 for f in recording.decode(audio=0)))
            with store.connect() as db:
                self.assertEqual(db.execute("SELECT source,count(*) FROM studio_inputs WHERE played_at IS NOT NULL GROUP BY source").fetchall(),[('twitch',1)])


class ScheduledInputPriorityTests(unittest.TestCase):
    def setUp(self):
        import threading
        from test_studio_replay import FixtureReply, Tone, av
        if av is None:
            self.skipTest('live media dependencies required')
        from ravora.conversation import ConversationBridge
        from ravora.live_program import LiveProgram
        from ravora.providers import FixtureReaction
        from ravora.safety import TurnFence

        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.store = MemoryStore(root / 'studio.db')
        settings = replace(ProviderSettings(), comment_source='twitch', memory_enabled=False,
                           character_max_initiatives=-1, character_idle_seconds=120)
        bridge = ConversationBridge(settings, self.store, root, TurnFence(), threading.RLock(),
                                    providers=(FixtureReply(), FixtureReaction(), Tone()))
        self.pipe = InputPipeline(settings, self.store, bridge, [])
        self.program = LiveProgram(self.pipe, root, prefetch=True, ray_reverb_mix=0,
                                   interrupt_initiatives=True, silence_ms=300)
        self.addCleanup(self.program.close)
        self.obs = self.program.observe('twitch', 'stream', 'fixture')
        self.pipe.audience_present = self.obs.has_audience
        self.pipe.audience_size = self.obs.audience_size

    def tick(self):
        stage, _ = self.program.next_frame()
        self.program.encoded(stage)
        return stage

    def until(self, predicate):
        deadline = time.monotonic() + 5
        while not predicate() and time.monotonic() < deadline:
            self.tick()
            time.sleep(.007)
        self.assertTrue(predicate(), self.program.last_error)

    def start_audible_initiative(self):
        self.obs.audience(time.time(), 'live', 'stream', 1)
        self.until(lambda: self.program.last_signal is not None and self.program.generated)
        self.assertEqual(self.program.completed, 0)
        self.assertIsNotNone(self.pipe.active_followup)

    def chat(self, message="first", viewer="25"):
        reader = TwitchReader(self.pipe.inbox, None, config={})
        meta = {'subscription_type': MESSAGE, 'message_timestamp': datetime.now(timezone.utc).isoformat()}
        payload = {'event': {'broadcaster_user_id': '42', 'chatter_user_id': viewer, 'chatter_user_name': 'Viewer',
                             'message_id': message, 'message': {'text': 'Hello Ray!'}}}
        reader._notification(meta, payload, '42')
        reader._notification(meta, payload, '42')
        self.assertEqual(reader.counts, {'queued': 1, 'duplicate': 1})

    def test_chat_at_final_sentence_preserves_ack_and_prepares_reply_with_unknown_viewers(self):
        self.start_audible_initiative()
        first = self.program.token
        self.chat()
        self.obs.audience(time.time(), 'unavailable')
        self.assertEqual(self.tick().speaker, 'Ray')
        self.assertEqual(self.program.token, first)
        self.until(lambda: self.program.completed == 2)
        with self.store.connect() as db:
            rows = db.execute('SELECT source,status,prefetched FROM studio_inputs ORDER BY received').fetchall()
        self.assertEqual(rows, [('ray-initiative','played',0), ('twitch','played',1)])

    def test_only_first_initiative_sentence_is_synthesized_and_viewer_is_prefetched(self):
        import wave
        from ravora.ray_character import public_fixture
        from ravora.live_media import RATE, SAMPLES

        class TwoSentences:
            def stream(self, comment, history):
                yield public_fixture('ひとつ目です。ふたつ目です。')
            stream.ray_public_output = True

        utterances = []
        class Sound:
            def create(self, text, path):
                utterances.append(text)
                with wave.open(str(path),'wb') as writer:
                    writer.setparams((1,2,RATE,0,'NONE',''))
                    writer.writeframes((1500).to_bytes(2,'little')*RATE)
                return {'duration':1., 'source':'fixture'}

        self.program.bridge.controller.text = TwoSentences()
        self.program.bridge.controller.speech = Sound()
        self.start_audible_initiative()
        first = self.program.token['turn_id']
        self.chat()
        self.obs.audience(time.time(), 'unavailable')
        self.until(lambda: self.program.token is None or self.program.token['turn_id'] != first)
        self.until(lambda: self.program.completed == 2)
        self.assertEqual(utterances, ['ひとつ目です。','ひとつ目です。ふたつ目です。'])
        with self.store.connect() as db:
            rows = db.execute('SELECT source,status,reason,prefetched,first_sample,end_sample FROM studio_inputs ORDER BY received').fetchall()
        self.assertEqual(rows[0][:4],('ray-initiative','played',None,0))
        self.assertEqual(rows[0][5]-rows[0][4],RATE)
        self.assertEqual(rows[1][:4],('twitch','played',None,1))
        self.assertGreaterEqual(rows[1][4],rows[0][5])
        self.assertGreaterEqual(rows[1][5]-rows[1][4],RATE-SAMPLES)

    def test_long_synthesized_opening_is_held_including_any_streamed_prefix(self):
        import wave
        from ravora.live_media import RATE
        class Oversized:
            streams_program_pcm = True
            def create(self, text, path, **options):
                assert 'on_pcm' not in options
                with wave.open(str(path),'wb') as wav:
                    wav.setparams((1,2,RATE,0,'NONE',''))
                    wav.writeframes(b'\x30\x02' * (RATE*4))
                return {'duration':.5,'source':'fixture'}
        self.program.bridge.controller.speech = Oversized()
        self.obs.audience(time.time(),'live','stream',1)
        self.until(lambda: self.program.last_error == 'INITIATIVE_AUDIO_TOO_LONG')
        with self.store.connect() as db:
            row = db.execute("SELECT status,reason,first_sample,played_at FROM studio_inputs").fetchone()
            calls = db.execute("SELECT COUNT(*) FROM provider_calls WHERE provider='tts'").fetchone()[0]
            policy = db.execute("SELECT policy,first_frame FROM studio_speech_policies").fetchall()
        self.assertEqual(row,('held','INITIATIVE_AUDIO_TOO_LONG',None,None))
        self.assertEqual(calls,1)
        self.assertEqual(policy,[('short-turn-v1',0)])

    def test_overlong_text_is_held_before_tts(self):
        from ravora.ray_character import public_fixture
        class Verbose:
            def stream(self, comment, history):
                yield public_fixture('Here is a much longer thought for today.')
            stream.ray_public_output = True
        self.program.bridge.controller.text = Verbose()
        self.obs.audience(time.time(),'live','stream',1)
        self.until(lambda: self.program.last_error == 'INITIATIVE_TEXT_TOO_LONG')
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT status,first_sample FROM studio_inputs').fetchone(),('held',None))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM provider_calls WHERE provider='tts'").fetchone()[0],0)

    def test_chat_starts_while_fenced_unheard_tts_is_still_in_flight(self):
        import threading
        from test_studio_replay import Tone
        inside, release = threading.Event(), threading.Event()
        class Delayed(Tone):
            def create(self, text, path):
                if not inside.is_set():
                    inside.set()
                    if not release.wait(5): raise RuntimeError('fixture timed out')
                return super().create(text,path)
        self.addCleanup(release.set)
        self.program.bridge.controller.speech = Delayed()
        self.obs.audience(time.time(),'live','stream',1)
        self.until(inside.is_set)
        self.chat()
        self.obs.audience(time.time(),'unavailable')
        try:
            self.until(lambda: self.program.completed == 1)
            self.assertTrue(self.pipe.retiring_initiative.worker.is_alive())
            with self.store.connect() as db:
                rows = db.execute('SELECT source,status,first_sample FROM studio_inputs ORDER BY received').fetchall()
            self.assertEqual(rows[0],('ray-initiative','cancelled',None))
            self.assertEqual(rows[1][:2],('twitch','played'))
            self.assertIsNotNone(rows[1][2])
        finally:
            release.set()
        self.until(lambda: self.pipe.retiring_initiative is None)

    def test_deleted_comment_during_sentence_does_not_cancel_ray(self):
        self.program.prefetch = False
        self.start_audible_initiative()
        self.chat()
        self.tick()
        self.pipe.inbox.remove('twitch','42','first')
        self.until(lambda: self.program.completed == 1)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT source,status FROM studio_inputs ORDER BY received').fetchall(),
                             [('ray-initiative','played'),('twitch','removed')])

    def test_viewers_finish_in_order_until_operator_voice_takes_the_floor(self):
        self.obs.audience(time.time(),'live','stream',0)
        self.chat()
        self.until(lambda: self.program.last_signal is not None)
        active = self.program.token
        self.chat('second','26')
        self.assertEqual(self.tick().speaker,'Ray')
        self.assertEqual(self.program.token,active)
        with patch.object(self.program.handsfree,'speaking',return_value=True), \
                patch.object(self.program.handsfree,'status',return_value={'state':'speaking'}):
            self.tick()
            self.assertIsNone(self.program.token)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT status,reason FROM studio_inputs WHERE turn_id=?",
                                        (active['turn_id'],)).fetchone(),('cancelled','host_interrupt'))

    def test_a_streaming_prefix_window_is_not_a_sentence_boundary(self):
        from ravora.speech_priority import SpeechPriority
        arbiter = SpeechPriority()
        program = SimpleNamespace(token={'turn_id':'t'},last_signal=(10,1.),prefix=object(),clip=None)
        self.assertFalse(arbiter.yield_initiative(program,True))
        self.assertFalse(arbiter.yield_initiative(program,False))

    def test_chat_after_final_audio_preserves_prior_playback_ack(self):
        self.start_audible_initiative()
        self.until(lambda: self.program.clip is not None and self.program.clip.done and not self.program.audio)
        self.chat()
        self.obs.audience(time.time(), 'live', 'stream', 0)
        self.until(lambda: self.program.completed == 2)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT source,status FROM studio_inputs ORDER BY received').fetchall(),
                             [('ray-initiative','played'),('twitch','played')])

    def test_operator_handsfree_yields_on_voice_onset_and_replies_with_zero_viewers(self):
        import json
        import threading
        import urllib.request
        import numpy as np
        from ravora.developer_http import DeveloperHTTP
        from ravora.live_http import LiveHandler
        from ravora.live_mic import PACKET_BYTES

        server = self.enterContext(DeveloperHTTP(self.program, 'fixture', 0, handler=LiveHandler))
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        base = f'http://127.0.0.1:{server.server_port}/v1/host/mic/'
        def post(endpoint, data=b'{}', **headers):
            req = urllib.request.Request(base+endpoint, data=data, headers={
                'Authorization': 'Bearer fixture', 'Content-Type': 'application/octet-stream' if endpoint=='pcm' else 'application/json',
                **headers})
            with urllib.request.urlopen(req, timeout=2) as response:
                return json.load(response)

        self.start_audible_initiative()
        # Open the authenticated mic during Ray's audio. A verified echo-cancelled track may listen immediately.
        owner = post('open-auto', **{'X-Mic-Echo-Cancelled': '1'})['session']
        self.tick()
        self.assertEqual(self.program.handsfree.status()['state'], 'listening')
        self.assertIsNotNone(self.pipe.active_followup)
        self.obs.audience(time.time(), 'live', 'stream', 0)
        loud = (np.sin(np.arange(PACKET_BYTES // 2) * .12) * 6000).astype('<i2').tobytes()
        heard = self.enterContext(patch.object(self.program.stt, 'transcribe', return_value={'speech':True,'text':'Hello Ray'}))
        for seq, data in enumerate([loud]*3 + [bytes(PACKET_BYTES)]*3):
            post('pcm', data, **{'X-Mic-Session':owner, 'X-Mic-Sequence':str(seq)})
            self.tick()
            if seq == 0:
                self.assertTrue(self.program.handsfree.speaking())
                self.assertIsNone(self.program.token)
            time.sleep(.1)
        self.until(lambda: self.program.completed == 1)
        heard.assert_called_once()
        post('mute', **{'X-Mic-Session':owner})
        self.assertEqual(self.program.handsfree.status()['state'], 'off')
        with self.store.connect() as db:
            rows = db.execute('SELECT source,status FROM studio_inputs ORDER BY received').fetchall()
        self.assertEqual(rows, [('ray-initiative','cancelled'),('live-host','played')])

    def test_open_mic_without_echo_cancellation_holds_idle_and_allows_comments(self):
        owner = self.program.mic.open(automatic=True)
        self.obs.audience(time.time(), 'live', 'stream', 1)
        self.tick()
        self.assertIsNone(self.program.token)
        self.obs.audience(time.time(), 'unavailable')
        self.chat()
        self.until(lambda: self.program.completed == 1)
        self.program.mic.mute(owner)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT source,status FROM studio_inputs').fetchall(), [('twitch','played')])
