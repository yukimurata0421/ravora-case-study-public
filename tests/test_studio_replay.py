"""Replay three fixed viewer arrivals through the studio and a local AAC writer."""
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import wave

from ravora.comments import Comment
from ravora.conversation import ConversationBridge, MemoryStore
from ravora.input_pipeline import InputPipeline
from ravora.providers import FixtureReaction
from ravora.ray_character import public_fixture
from ravora.safety import TurnFence
from ravora.settings import ProviderSettings
from ravora.studio_report import report

try:
    import av
    import numpy as np
    from ravora.live_media import FPS, RATE, SAMPLES, ProgramWriter
    from ravora.live_program import LiveProgram
except ModuleNotFoundError:
    av = None


class FixtureReply:
    def stream(self, comment, history):
        yield public_fixture('I heard you.')

    stream.ray_public_output = True


class Tone:
    def create(self, text, path):
        time.sleep(.22)
        signal = np.sin(np.arange(RATE * 4 // 5) * (2 * np.pi * 440 / RATE)) * 6000
        with wave.open(str(path), 'wb') as wav:
            wav.setparams((1, 2, RATE, 0, 'NONE', ''))
            wav.writeframes(signal.astype('<i2').tobytes())
        return {'duration': len(signal) / RATE, 'source': 'fixture'}


def replay(root, *, arrivals=None, expected=3, **options):
    if arrivals is None:
        arrivals = [(tick,str(tick),'Hello Ray!') for tick in (0,15,35)]
    settings = replace(ProviderSettings(), comment_source='twitch', memory_enabled=False,
                       character_initiative_enabled=False)
    store = MemoryStore(root / 'fixture.sqlite3')
    bridge = ConversationBridge(settings, store, root, TurnFence(), threading.RLock(),
                                providers=(FixtureReply(), FixtureReaction(), Tone()))
    pipe = InputPipeline(settings, store, bridge, [])
    extents = {}
    with ExitStack() as stack:
        program = LiveProgram(pipe, root, ray_reverb_mix=0, **options)
        stack.callback(program.close)
        program.observe('local')
        frame = av.VideoFrame.from_ndarray(np.zeros((96, 160, 3), dtype='uint8'), format='rgb24')
        scene = SimpleNamespace(render=lambda stage: frame.reformat(format='yuv420p'))
        stack.enter_context(patch('ravora.live_media.SIZE', (160, 96)))
        writer = ProgramWriter(root / 'local.flv', scene, video_encoder='libx264')
        stack.callback(writer.close)
        start = time.monotonic()
        for tick in range(180):
            for index,(arrival,viewer,text) in enumerate(arrivals):
                if tick==arrival:
                    pipe.inbox.put(Comment('twitch','fixture-channel',str(index),viewer,'Viewer',text,time.time()))
            stage, pcm = program.next_frame()
            writer.write(stage, pcm)
            program.encoded(stage)
            signal = np.flatnonzero(np.abs(np.frombuffer(pcm, '<i2').astype('int32')) >= 33)
            if stage.speaker == 'Ray' and signal.size:
                turn = program.token['turn_id']
                extent = extents.setdefault(turn, [tick*SAMPLES + int(signal[0]), None])
                extent[1] = tick*SAMPLES + int(signal[-1]) + 1
            if program.completed == expected:
                break
            time.sleep(max(0, start + (tick + 1) / FPS - time.monotonic()))
        assert program.completed == expected, program.last_error
    with av.open(str(root / 'local.flv')) as recording:
        assert any(np.max(np.abs(f.to_ndarray())) > .02 for f in recording.decode(audio=0))
    positions = list(extents.values())
    with store.connect() as db:
        row, = report(db)['broadcasts']
    return [round((b[0]-a[1])*1000/RATE, 2) for a,b in zip(positions, positions[1:])], row


@unittest.skipUnless(av, 'youtube-live extra required')
class StudioReplay(unittest.TestCase):
    def test_two_music_questions_have_one_prefetched_group_reply_on_the_writer(self):
        with tempfile.TemporaryDirectory() as directory:
            gaps, row = replay(Path(directory),prefetch=True,expected=2,
                arrivals=[(0,'a','Hello'),(10,'b','What is this song?'),(10,'c','この曲の名前は？')])
        counts=row['comments']
        self.assertEqual(len(gaps),1)
        self.assertEqual((counts['received'],counts['played'],counts['prefetched']),(3,2,1))
        self.assertEqual(counts['selection']['individually_replied_speakers'],1)
        self.assertEqual(counts['selection']['group_replies'],1)
        self.assertEqual(row['api']['attempts'],0)

    def test_same_arrivals_serial_and_prefetched(self):
        results = []
        for mode in (False, True):
            with tempfile.TemporaryDirectory() as directory:
                gaps, row = replay(Path(directory), prefetch=mode)
            comments = row['comments']
            self.assertEqual((comments['received'], comments['selected'], comments['played']), (3,3,3))
            self.assertEqual(comments['prefetched'], 2 if mode else 0)
            self.assertEqual(row['api']['attempts'], 0)
            self.assertEqual(row['sessions'][0]['timing_write_losses'], 0)
            metric = comments['ray_gap_waiting_comment']
            self.assertEqual((metric['n'], metric['first_in_session'], metric['p95_ms']), (2,1,None))
            self.assertAlmostEqual(metric['max_ms'], max(gaps), places=2)
            results.append(gaps)
        self.assertTrue(max(results[1]) < min(results[0]), results)
        self.assertTrue(all(gap >= 220 for gap in results[0]), results)

    def test_prepared_audio_can_be_revoked_before_playout(self):
        for reason in ('delete','expired','host','stop','previous_removed'):
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
                root = Path(directory)
                store = MemoryStore(root/'studio.db')
                settings = replace(ProviderSettings(), character_initiative_enabled=False)
                bridge = ConversationBridge(settings,store,root,TurnFence(),threading.RLock(),
                                            providers=(FixtureReply(),FixtureReaction(),Tone()))
                pipe = InputPipeline(settings,store,bridge,[])
                program = LiveProgram(pipe,root,prefetch=True,ray_reverb_mix=0)
                stack.callback(program.close)
                program.observe('local')
                base = time.time()
                comments = [Comment('twitch','channel',str(i),str(i),'Viewer','Hi!',base) for i in (1,2)]
                pipe.inbox.put(comments[0])
                def tick():
                    stage, pcm = program.next_frame()
                    program.encoded(stage)
                    return stage.speaker
                tick()
                bridge.worker.join(2)
                self.assertEqual(tick(),'Ray')
                pipe.inbox.put(comments[1])
                self.assertEqual(tick(),'Ray')
                pending = pipe.prepared
                pending.bridge.worker.join(2)
                turn = pending.token['turn_id']
                with store.connect() as db:
                    self.assertEqual(db.execute('SELECT played FROM conversation_turns WHERE id=?',(turn,)).fetchone(), (0,))
                    self.assertEqual(db.execute('SELECT count(*) FROM memory_jobs WHERE turn_id=?',(turn,)).fetchone(), (0,))
                    self.assertEqual(db.execute('SELECT status FROM comment_inbox WHERE receipt=?',(comments[1].receipt,)).fetchone(), ('generated',))
                self.assertNotIn(turn,str([c.viewer_turn for c in program.monitor_history]))
                if reason in ('delete','previous_removed'):
                    msg = comments[1 if reason=='delete' else 0]
                    pipe.inbox.remove(msg.source,msg.stream_id,msg.message_id)
                    pipe.check_removed()
                elif reason=='expired':
                    pipe.inbox.clock = lambda:base+settings.comment_ttl+1
                elif reason=='host':
                    with program.ingress:
                        tick()
                else:
                    program.close()
                    stack.pop_all()
                if reason!='stop':
                    for _ in range(45):
                        tick()
                with store.connect() as db:
                    status,played = db.execute('SELECT status,played FROM conversation_turns WHERE id=?',(turn,)).fetchone()
                    self.assertEqual(played,0)
                    self.assertIn(status, ('cancelled','source_removed'))
                    self.assertEqual(db.execute('SELECT count(*) FROM memory_jobs WHERE turn_id=?',(turn,)).fetchone(),(0,))
                    receipt, ready = db.execute('SELECT status,first_sample FROM studio_inputs WHERE turn_id=?',(turn,)).fetchone()
                    self.assertEqual(receipt,{'delete':'removed','expired':'expired'}.get(reason,'cancelled'))
                    self.assertIsNone(ready)
                    self.assertEqual(db.execute('SELECT count(*) FROM provider_calls WHERE turn_id=?',(turn,)).fetchone(),(3,))
                self.assertFalse(list(root.glob(turn+'-*.wav')))
