import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ravora.comments import Comment, CommentInbox
from ravora.conversation import MemoryStore
from ravora.live_metrics import INTERVAL, WINDOW, StudioMetrics, minute_usage


class MetricsTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = MemoryStore(Path(directory.name) / 'test.sqlite3')
        self.inbox = CommentInbox(self.store, scope='cloud')

    def turn(self, inbox, identifier):
        comment = Comment('live-host', inbox.session, identifier, 'owner', 'Host', 'private test text', time.time())
        inbox.put(comment)
        self.assertTrue(inbox.bind(inbox.take()['receipt'], identifier))
        self.store.start(identifier, 'cloud:' + comment.viewer, comment.text)

    def call(self, turn, provider, *, attempted=True, mode='cloud', **usage):
        key = self.store.prepare_call(turn, provider, 'test', {'characters': 8}, mode=mode)
        self.store.call_state(key, 'dispatched', attempted=attempted)
        self.store.call_usage(key, usage)
        return key

    def test_usage_counts_only_current_minute_remote_attempts_in_this_session(self):
        other = CommentInbox(self.store, scope='cloud')
        self.turn(self.inbox, 'first')
        self.turn(other, 'other')
        self.call('first', 'gemini', total_tokens=29)
        self.call('first', 'tts')
        self.call('other', 'jev', total_tokens=500)
        self.call('first', 'jev', attempted=False)
        self.call('first', 'tts', mode='offline')
        now = time.time()
        old = self.call('first', 'gemini', total_tokens=70)
        with self.store.connect() as db:
            db.execute('UPDATE api_usage SET created_at=? WHERE id=?', (now - 61, old))
        self.assertEqual(minute_usage(self.inbox, now), {'gemini':1, 'jev':0, 'tts':1, 'stt':0, 'calls':2,
            'audio_seconds':0, 'tokens':29, 'missing_tokens':0, 'characters':8, 'api_failed':0, 'api_unknown':0})
        self.store.call_state(self.call('first', 'jev'), 'outcome_unknown')
        self.store.call_state(self.call('first', 'tts'), 'failed')
        fresh = minute_usage(self.inbox, time.time())
        self.assertEqual(fresh['jev'], 1)
        self.assertEqual((fresh['tokens'], fresh['missing_tokens']), (29, 1))
        self.call('other', 'gemini')
        with self.store.connect() as db:
            db.execute("UPDATE api_usage SET total_tokens=NULL WHERE turn_id='other'")
        self.assertIsNone(minute_usage(other, time.time())['tokens'])
        self.assertEqual((fresh['api_failed'], fresh['api_unknown']), (1, 1))
        self.assertNotIn('private', json.dumps(fresh))

    def test_session_totals_outlive_chart_window_and_include_independent_stt_once(self):
        from ravora.input_records import InputRecords
        other = CommentInbox(self.store, scope='cloud')
        self.turn(self.inbox, 'ours')
        self.turn(other, 'theirs')
        old = self.call('ours', 'gemini', total_tokens=17)
        self.call('theirs', 'gemini', total_tokens=500)
        self.call('ours', 'jev', attempted=False)
        self.call('ours', 'tts')
        for i, (inbox, mode, attempted) in enumerate([(self.inbox, 'cloud', 1), (self.inbox, 'cloud', 0),
                (other, 'cloud', 1), (self.inbox, 'mock', 1)]):
            records = InputRecords(self.store, inbox.session, mode)
            key, _ = records.create(f'{i:032x}', 'audio', b'fixture')
            with self.store.connect() as db:
                db.execute('UPDATE developer_inputs SET duration_seconds=2.5, request_attempted=? WHERE id=?',
                           (attempted, key))
        program = SimpleNamespace(lock=threading.Lock(), frame=1, failed=0, host_stt_counts={},
                                  pipeline=SimpleNamespace(inbox=self.inbox))
        monitor = StudioMetrics(program)
        monitor.started_at -= WINDOW + 100
        with self.store.connect() as db:
            db.execute('UPDATE api_usage SET created_at=? WHERE id=?', (time.time()-WINDOW-10, old))
            db.execute('DELETE FROM comment_inbox WHERE session=?', (self.inbox.session,))
        for _ in range(2):
            monitor.sample(SimpleNamespace(transport=None, encoded_bytes=0))
            point = monitor.snapshot()['points'][-1]
            self.assertEqual(point['calls'], 2)
            total = point['api_total']
            self.assertEqual([total[k] for k in ('calls', 'gemini', 'jev', 'tts', 'stt')], [3,1,0,1,1])
            self.assertEqual([total[k] for k in ('tokens', 'characters', 'audio_seconds')], [17,8,2.5])

    def test_samples_use_real_elapsed_time_and_separate_sent_bytes_and_errors(self):
        offline = SimpleNamespace(scope='offline')
        program = SimpleNamespace(lock=threading.Lock(), frame=1, failed=1, host_stt_counts={'accepted':20},
                                  pipeline=SimpleNamespace(inbox=offline))
        transport = {'muxed_bytes': 10, 'queue_packets': 0, 'io_errors': 0}
        writer = SimpleNamespace(encoded_bytes=50, transport=SimpleNamespace(status=transport.copy))
        monitor = StudioMetrics(program)
        with patch('ravora.live_metrics.time.monotonic', side_effect=[5, 9]):
            monitor.sample(writer)
            program.frame += 120
            program.failed += 1
            program.host_stt_counts.update({'accepted': 21, 'HOST_NO_VOICE':2})
            writer.encoded_bytes += 3_000_000
            transport.update(muxed_bytes=2_000_010, io_errors=3)
            monitor.sample(writer)
        first, last = monitor.snapshot()['points']
        self.assertIsNone(first['fps'])
        self.assertEqual([last[k] for k in ('fps', 'encoded', 'sent', 'turn_errors', 'mic_errors', 'send_errors')],
                         [30, 6, 4, 1, 2, 3])
        writer.transport = None
        for _ in range(WINDOW // INTERVAL + 3):
            monitor.sample(writer)
        history = monitor.snapshot()['points']
        self.assertEqual(len(history), WINDOW // INTERVAL + 1)
        self.assertIsNone(history[-1]['sent'])
        self.assertEqual(history[-1]['tts'], 0)

    def test_unavailable_usage_is_unknown(self):
        missing = SimpleNamespace(scope='cloud', store=SimpleNamespace(path=self.store.path.with_name('missing')), session='s')
        self.assertTrue(all(v is None for v in minute_usage(missing, time.time()).values()))
        self.assertFalse(missing.store.path.exists())

    def test_metrics_endpoint_requires_host_token_and_audience_page_omits_charts(self):
        from urllib.error import HTTPError
        from urllib.request import Request, urlopen
        from ravora.developer_http import DeveloperHTTP
        try:
            from ravora.live_http import LiveHandler
        except ModuleNotFoundError as exc:
            if exc.name not in ('numpy', 'av', 'PIL'):
                raise
            self.skipTest('youtube-live media extras required')

        with DeveloperHTTP(None, 'test-host-token', 0, handler=LiveHandler) as server:
            server.metrics = StudioMetrics(None)
            origin = f'http://127.0.0.1:{server.server_port}'
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            try:
                with self.assertRaises(HTTPError) as denied:
                    urlopen(origin+'/v1/host/metrics', timeout=2)
                with denied.exception as error:
                    self.assertEqual(error.code, 401)
                req = Request(origin+'/v1/host/metrics', headers={'Authorization':'Bearer test-host-token'})
                with urlopen(req, timeout=2) as response:
                    self.assertEqual(json.load(response)['points'], [])
                with urlopen(origin+'/overlay', timeout=2) as response:
                    self.assertNotIn(b'metrics', response.read())
            finally:
                server.shutdown()
                thread.join()
