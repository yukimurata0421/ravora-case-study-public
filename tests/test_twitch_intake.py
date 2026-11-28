import datetime as dt
from pathlib import Path
import sqlite3
import tempfile
import threading
import types
import unittest
from importlib.util import find_spec
from unittest.mock import patch

if find_spec('requests') is None or find_spec('websocket') is None:
    raise unittest.SkipTest('youtube-live extra required')


from ravora.comments import CommentInbox
from ravora.conversation import MemoryStore
from ravora.twitch_chat import DELETE, MESSAGE, TwitchReader
from ravora.twitch_intake import attach_intake
from ravora import twitch_language_hint


class CommittedIntakeTests(unittest.TestCase):
    def setUp(self):
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.now = 1_800_000_000.0
        self.store = MemoryStore(folder / 'chat.db')
        self.inbox = CommentInbox(self.store, capacity=20, clock=lambda: self.now)
        self.reader = TwitchReader(self.inbox, folder/'grant', config=folder/'cfg', irc_token_path=folder/'irc')
        self.program = types.SimpleNamespace(next_check=999, pipeline=types.SimpleNamespace())

    def attach(self):
        self.assertTrue(attach_intake(self.program, self.reader))
        self.intake = self.reader.intake
        self.addCleanup(self.intake.close)

    def send(self, mid, *, delete=False, irc=False, text='How are you?'):
        send = self.reader.irc.notify if irc else self.reader._notification
        published = dt.datetime.fromtimestamp(self.now, dt.timezone.utc).isoformat()
        send({'subscription_type': DELETE if delete else MESSAGE, 'message_timestamp': published},
             {'event': {'broadcaster_user_id':'42', 'chatter_user_id':'v'+mid, 'chatter_user_name':'Guest',
                        'message_id':mid, 'message':{'text':text}}}, '42')

    def rows(self):
        with self.store.connect() as db:
            return db.execute('SELECT message_id,status,received,text FROM comment_inbox ORDER BY rowid').fetchall()

    def test_arrival_batch_order_deletion_and_durable_wake(self):
        entered, release = threading.Event(), threading.Event()
        base = self.inbox._put
        def delayed(db, event, **kw):
            if event.message_id == 'first':
                entered.set()
                if not release.wait(5):
                    raise TimeoutError()
            return base(db,event,**kw)
        self.inbox._put = delayed
        callback = []
        self.reader.on_removed = lambda: callback.append(self.rows())
        self.attach()
        self.addCleanup(release.set)
        self.send('first')
        self.assertTrue(entered.wait(3))
        self.send('v0')
        self.send('v0',irc=True)
        self.send('later',delete=True)
        self.send('later',irc=True)
        self.send('v0',delete=True,irc=True)
        for i in range(9):
            self.send('m'+str(i))
        self.assertEqual(self.rows(), [])
        self.intake.wake_idle(self.program)
        self.assertEqual(self.program.next_check,999)
        self.now += 2
        release.set()
        self.intake.close()
        rows = self.rows()
        self.assertEqual([r[0] for r in rows],['first','v0']+['m'+str(i) for i in range(9)])
        self.assertTrue(all(r[2] == self.now - 2 for r in rows))
        self.assertEqual(rows[1][1::2],('removed',''))
        self.assertEqual(callback[0][1][1], 'removed')
        self.assertEqual(self.reader.counts['intake_commits'], 3)
        self.assertEqual(self.reader.counts['intake_batched_commits'], 2)
        self.assertEqual(self.reader.counts['duplicate'],1)
        self.assertEqual(self.reader.counts['removed'],2)
        self.intake.wake_idle(self.program)
        self.assertEqual(self.program.next_check,0)
        self.program.next_check=123
        self.intake.wake_idle(self.program)
        self.assertEqual(self.program.next_check,123)

    def test_delayed_event_expires_without_a_wake(self):
        self.attach()
        with self.reader.event_lock:
            self.send('expired')
            self.now += 46
        self.intake.close()
        self.assertEqual(self.rows()[0][1],'expired')
        self.assertFalse(self.intake.committed.is_set())

    def test_failure_rolls_back_without_an_admission_or_wake(self):
        base=self.inbox._put
        def fail(db,event,**kw):
            base(db,event,**kw)
            raise sqlite3.IntegrityError('fixture')
        self.inbox._put=fail
        self.attach()
        with self.assertLogs('ravora.twitch_intake',level='ERROR'):
            self.send('bad')
            self.intake.close()
        self.assertFalse(self.intake.committed.is_set())
        self.assertEqual(self.rows(),[])
        self.assertEqual(self.reader.counts['intake_failed_or_unconfirmed'],1)
        self.assertEqual(self.reader.code,'TWITCH_CHAT_DB_ERROR')
        with self.assertRaises(sqlite3.OperationalError):
            self.send('another')

    def test_callback_failure_cannot_retry_committed_deletion(self):
        self.attach()
        def fail():
            exc=sqlite3.OperationalError('fixture_callback_busy')
            exc.sqlite_errorcode=sqlite3.SQLITE_BUSY
            raise exc
        self.reader.on_removed=fail
        with self.assertLogs('ravora.twitch_intake',level='ERROR'):
            self.send('deleted',delete=True)
            self.intake.close()
        self.assertEqual(self.reader.counts['intake_commits'],1)
        self.assertEqual(self.reader.counts['removed'],1)
        self.assertEqual(self.reader.counts['intake_failed_or_unconfirmed'],0)
        self.assertTrue(self.intake.failed)

    def test_early_language_result_is_reused_with_uncertain_fallback(self):
        self.attach()
        for mid,text,expected in [('en','How is your intake test?', 'en'),('ja','こんにちは、テストです。','ja'),
                                  ('mixed','Kanon 123?',None)]:
            self.send(mid,text=text)
            with patch.object(twitch_language_hint,'clear_reply_language',side_effect=AssertionError('recomputed')):
                self.assertEqual(twitch_language_hint.reply_language(text),expected)
        self.assertEqual(twitch_language_hint.reply_language('Where are you going today?'),'en')

    def test_locked_transaction_retries_then_publishes_once(self):
        calls=[]
        base=self.inbox._put
        def busy_once(db,event,**kw):
            result=base(db,event,**kw)
            calls.append(result)
            if len(calls)==1:
                exc=sqlite3.OperationalError('fixture_busy')
                exc.sqlite_errorcode=sqlite3.SQLITE_BUSY
                raise exc
            return result
        self.inbox._put=busy_once
        self.attach()
        self.send('retry')
        self.intake.close()
        self.assertEqual(calls,['queued','queued'])
        self.assertEqual(self.reader.counts['intake_commits'],1)
        self.assertEqual(self.reader.counts['queued'],1)
        self.assertTrue(self.intake.committed.is_set())

    def test_full_queue_applies_backpressure_and_shutdown_drains_every_event(self):
        entered, release, attempting, completed = (threading.Event() for _ in range(4))
        base=self.inbox._put
        def pause(db,event,**kw):
            if event.message_id=='head':
                entered.set()
                if not release.wait(5):
                    raise TimeoutError()
            return base(db,event,**kw)
        self.inbox._put=pause
        self.attach()
        self.addCleanup(release.set)
        self.send('head')
        self.assertTrue(entered.wait(2))
        for i in range(64):
            self.send('head',irc=True)
        def extra():
            attempting.set()
            self.send('head',irc=True)
            completed.set()
        producer=threading.Thread(target=extra,daemon=True)
        producer.start()
        self.assertTrue(attempting.wait(2))
        self.assertFalse(completed.wait(.03))
        release.set()
        self.assertTrue(completed.wait(4))
        producer.join(1)
        self.intake.close()
        self.assertEqual(self.reader.counts['queued'],1)
        self.assertEqual(self.reader.counts['duplicate'],65)
        self.assertEqual(len(self.rows()),1)
