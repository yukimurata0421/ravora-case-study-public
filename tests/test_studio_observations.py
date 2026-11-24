from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import Mock

from ravora.comments import Comment, CommentInbox
from ravora.conversation import MemoryStore
from ravora.studio_observations import StudioObservations, import_observation
from ravora.studio_report import report, timing
from ravora.twitch_audience import sample


class ObservationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = MemoryStore(Path(self.tmp.name) / 'test.db')

    def session(self, identity=None):
        inbox = CommentInbox(self.store, scope='cloud')
        return inbox, StudioObservations(inbox, 'twitch', True, 'twitch', identity, sample_rate=48000, fps=30)

    def test_duplicate_receipts_costs_test_labels_and_retention(self):
        inbox, obs = self.session()
        now = time.time()
        def put(message):
            comment = Comment('twitch','channel',message,'person','private name','private text',now)
            inbox.put(comment)
            return comment
        first = put('1')
        self.assertEqual(inbox.put(first), 'duplicate')
        self.assertEqual(inbox.take()['receipt'], first.receipt)
        inbox.bind(first.receipt, 't1', prefetched=True)
        self.store.start('t1','cloud:'+first.viewer,first.text)
        key = self.store.prepare_call('t1','gemini','generate',{},model='gemini-3.5-flash-lite',mode='cloud')
        self.store.call_state(key,'dispatched',attempted=True)
        self.store.call_usage(key,{'input_tokens':100,'cached_input_tokens':20,'total_tokens':108,'output_tokens':8})
        self.store.call_state(key,'accepted')
        for _ in range(2):
            key = self.store.prepare_call('t1','tts','synthesize',{'chunk':0,'characters':20},model='en-US-Chirp3-HD-Zephyr',mode='cloud')
            self.store.call_state(key,'failed',attempted=True)
        key = self.store.prepare_call('t1','jev','reaction',{},mode='cloud')
        self.store.call_state(key,'outcome_unknown',attempted=True)
        inbox.finish(first.receipt, 'cancelled','host_interrupt')
        second = put('2')
        inbox.take()
        inbox.bind(second.receipt,'t2')
        self.store.start('t2','cloud:'+second.viewer,second.text)
        obs.first_signal('t2',None,32,now+.1)
        obs.end_signal('t2',960,now+.2)
        self.store.finish('t2','A reply.','completed',{})
        self.store.played('t2',memory_enabled=False,relationship_enabled=False)
        inbox.finish(second.receipt, 'played')
        obs.audience(now,'live','stream1',0)
        obs.audience(now+60,'unavailable')
        obs.audience(now+120,'live','stream1',8)
        obs.close(6000,'reading')
        summary = {'kind':'summary','platform':'twitch','stream_id':'stream1',
                   'started_at':now-1,'ended_at':now+150,'unique_viewers':27}
        match = {'kind':'player_signal','session':inbox.session,'receipt':second.receipt,
                 'turn_id':'t2','method':'waveform_match','observed_at':now+.8}
        with self.store.connect() as db:
            import_observation(db,summary)
            import_observation(db,summary)
            import_observation(db,match)
            import_observation(db,match)
            with self.assertRaises(ValueError):
                import_observation(db,dict(match,turn_id='t1'))
            import_observation(db,{'kind':'test_comment','session':inbox.session,'receipt':second.receipt})
            db.execute('DELETE FROM comment_inbox WHERE session=?',(inbox.session,))
        inbox2, obs2 = self.session('stream1')
        obs2.close(300,'stopped')
        with self.store.connect() as db:
            result = report(db)
        row, = result['broadcasts']
        self.assertNotIn('private',str(result))
        self.assertFalse(result['legacy_sessions'])
        self.assertEqual(row['broadcast_seconds'],151)
        self.assertEqual(row['program_seconds'],210)
        self.assertEqual(len(row['sessions']),2)
        self.assertEqual(row['audience']['unique_viewers'],27)
        self.assertEqual((row['audience']['sample_count'],row['audience']['sample_average']), (2,4))
        self.assertEqual(row['comments']['received'],1)
        self.assertEqual(row['comments']['prefetched_discarded'],1)
        self.assertEqual(row['test_comments']['event_to_player']['p50_ms'],800)
        self.assertEqual(row['test_comments']['event_to_player']['n'],1)
        self.assertIsNone(row['test_comments']['event_to_player']['p95_ms'])
        self.assertEqual(row['comments']['received_to_program']['missing'],1)
        api = row['api']
        self.assertEqual((api['attempts'],api['discarded_prefetch_attempts'],api['unpriced_calls']), (4,4,1))
        self.assertIsNone(api['estimated_usd'])
        self.assertAlmostEqual(api['estimated_usd_known'],.0012446)
        tts, = [item for item in api['by_operation'] if item['provider']=='tts']
        self.assertEqual((tts['retries'],tts['failed']), (1,2))

    def test_idle_missing_and_clock_reset(self):
        from ravora.studio_report import conversation
        def row(turn, previous, first, end, received, session='a', played_at=None):
            return dict(receipt=turn,turn_id=turn,sample_rate=48000,previous_turn=previous,session=session,source='twitch',test=0,
                        first_sample=first,end_sample=end,first_written=10 if first is not None else None,
                        last_written=12 if end else None, received=received,viewer='p',
                        status='played' if played_at else 'failed',reason=None,
                        intake_reason=None,selected=1,prefetched=0,played_at=played_at,published=received,
                        player_at=None,ready_at=None,wav_ready=None)
        rows=[row('one',None,0,100,1,played_at=5),row('two','one',48100,50000,13),
              row('three','two',98000,99000,11),row('four','missing',110000,None,12),
              row('unheard',None,None,None,12),row('restart',None,0,100,4,session='b')]
        result = conversation(rows,False)
        self.assertEqual(result['returning_after_reply'],1)
        gaps = result['ray_gap_waiting_comment']
        self.assertEqual((gaps['n'],gaps['idle_excluded'],gaps['previous_end_unknown'],
                          gaps['no_program_signal'],gaps['first_in_session']), (1,1,1,1,2))
        self.assertEqual(gaps['p50_ms'],1000)
        self.assertEqual(timing(list(range(1,21)))['p95_ms'],19)

    def test_helix_validates_stream_and_keeps_offline_unknown(self):
        oauth = Mock(user_id='123',client_id='fixture',access='fixture')
        http = Mock()
        response = http.get.return_value
        response.status_code = 200
        payload = {'user_id':'123','type':'live','id':'456','viewer_count':0}
        response.json.return_value = {'data':[payload]}
        self.assertEqual(sample(http,oauth),('live','456',0,None))
        self.assertEqual(http.get.call_args.kwargs['params'],{'user_id':'123'})
        response.json.return_value = {'data':[]}
        self.assertEqual(sample(http,oauth),('offline',None,None,None))
        response.json.return_value = {'data':[dict(payload,user_id='other')]}
        with self.assertRaises(ValueError):
            sample(http,oauth)
        response.status_code = 429
        self.assertEqual(sample(http,oauth),('http_429',None,None,None))
        inbox, obs = self.session('456')
        obs.audience(10,'live','different',17)
        obs.close(1,'failed')
        with self.store.connect() as db:
            row, = report(db)['broadcasts']
            self.assertIsNone(row['stream_id'])
            self.assertTrue(row['sessions'][0]['identity_conflict'])
            with self.assertRaises(ValueError):
                import_observation(db,{'kind':'summary','platform':'twitch','stream_id':'different',
                                       'session':inbox.session,'started_at':1,'ended_at':5,'unique_viewers':7})

    def test_player_waveform_matches_the_bound_chunk_and_rejects_duplicates(self):
        import hashlib
        import io
        import wave
        try:
            import numpy as np
            from ravora.player_match import locate, player_observation
        except ModuleNotFoundError:
            self.skipTest('youtube-live extra required')
        def wav(samples):
            data = io.BytesIO()
            with wave.open(data,'wb') as sound:
                sound.setparams((1,2,48000,0,'NONE',''))
                sound.writeframes(samples.astype('<i2').tobytes())
            return data.getvalue()
        rng = np.random.default_rng(31)
        voice = rng.normal(0,3500,24000)
        silence = np.zeros(4800)
        reference = wav(voice)
        recording = wav(np.r_[silence,voice*.7,silence])
        self.assertAlmostEqual(locate(reference,recording)[0],.1,places=3)
        with self.assertRaisesRegex(ValueError,'AMBIGUOUS'):
            locate(reference,wav(np.r_[voice,silence,voice]))
        with self.assertRaisesRegex(ValueError,'SILENT'):
            locate(wav(silence),recording)
        path = Path(self.tmp.name)
        ref, cap = path/'first.wav', path/'player.wav'
        ref.write_bytes(reference)
        cap.write_bytes(recording)
        inbox, obs = self.session()
        stamp = time.time()
        msg = Comment('twitch','channel','1','person','Viewer','Hello',stamp-1)
        inbox.put(msg)
        inbox.take()
        inbox.bind(msg.receipt,'a')
        self.store.start('a','cloud:'+msg.viewer,msg.text)
        self.store.record_utterance('a',0,'Hello',ref,.5)
        obs.first_signal('a',None,100,stamp)
        obs.close(1,'reading')
        with self.store.connect() as db:
            record = player_observation(db,inbox.session,'a',cap,stamp+1)
            self.assertEqual(record['capture_sha256'],hashlib.sha256(recording).hexdigest())
            import_observation(db,record)
            with self.assertRaisesRegex(ValueError,'REFERENCE_TURN_MISMATCH'):
                player_observation(db,inbox.session,'a',cap,stamp+1,cap)
            self.assertAlmostEqual(report(db)['broadcasts'][0]['comments']['event_to_player']['p50_ms'],2100,places=0)
