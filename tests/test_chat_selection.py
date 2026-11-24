"""Exercise the studio's actual receipt, reservation, playback and report boundaries."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from ravora.comments import Comment, CommentInbox
from ravora.conversation import MemoryStore
from ravora.studio_observations import StudioObservations
from ravora.studio_report import report


class ChatSelectionTests(unittest.TestCase):
    def setUp(self):
        root = self.enterContext(tempfile.TemporaryDirectory())
        self.now = time.time()
        self.store = MemoryStore(Path(root)/'studio.db')
        self.inbox = CommentInbox(self.store, session='fictional', clock=lambda:self.now, scope='cloud')
        self.observer = StudioObservations(self.inbox,'twitch',True,'twitch','show',sample_rate=48000,fps=30)
        self.addCleanup(self.observer.close,0,'reading')
        self.serial = 0

    def post(self, who, text='Hello', *, age=0, parent=None):
        self.serial += 1
        item = Comment('twitch','channel',str(self.serial),who,who,text,self.now-age,parent)
        self.inbox.put(item)
        return item

    def say(self, reply='Heard you.', *, chosen=None):
        selected = chosen or self.inbox.take()
        self.assertIsNotNone(selected)
        receipt = selected['receipt']
        text, owner = self.inbox.claimed(receipt)
        self.assertEqual((text,owner),(selected['comment'],selected['viewer']))
        self.assertTrue(self.inbox.bind(receipt,receipt))
        self.store.start(receipt,'cloud:'+owner,text)
        self.store.finish(receipt,reply,'completed',{})
        self.inbox.finish(receipt,'generated')
        with patch('ravora.conversation.time.time',return_value=self.now):
            self.assertTrue(self.store.played(receipt,memory_enabled=owner.startswith('audience:')))
        self.inbox.finish(receipt,'played')
        self.now += .1
        return selected

    def values(self, sql, *args):
        with self.store.connect() as db:
            return db.execute(sql,args).fetchall()

    def test_expiry_before_admission_and_time_needed_before_dispatch(self):
        for who in 'abcde':
            self.post(who)
        self.now += 46
        fresh = self.post('f')
        self.assertEqual(self.say()['receipt'],fresh.receipt)
        old = self.post('g',age=40)
        self.assertIsNone(self.inbox.take(viewer_only=True,playout_delay=8))
        self.assertEqual(self.values('SELECT reason,turn_id FROM comment_inbox WHERE receipt=?',old.receipt),
                         [('playout_deadline',None)])

    def test_latest_update_keeps_wait_and_ticket_and_deduplicates_replay(self):
        first = self.post('a')
        self.now += 1
        self.post('b')
        self.now += 1
        updated = self.post('a','A correction.')
        self.post('a','older delivery',age=1)
        self.assertEqual(self.inbox.put(first),'duplicate')
        rows = self.values('SELECT waiting_since,ticket FROM chat_candidates WHERE receipt IN (?,?)',
                           first.receipt,updated.receipt)
        self.assertEqual(rows[0],rows[1])
        choice = self.inbox.take()
        self.assertEqual((choice['receipt'],choice['comment']),(updated.receipt,'A correction.'))

    def test_single_claim_and_one_future_candidate_per_active_speaker(self):
        for who in 'ab':
            self.post(who)
            self.now += .01
        with ThreadPoolExecutor(max_workers=2) as pool:
            choices = list(pool.map(lambda _: self.inbox.take(), range(2)))
        chosen, = [c for c in choices if c]
        self.inbox.bind(chosen['receipt'],'playing')
        self.inbox.finish(chosen['receipt'],'generated')
        later = self.post(chosen['display_name'],'one more')
        with ThreadPoolExecutor(max_workers=2) as pool:
            prepared = list(pool.map(lambda _: self.inbox.take(viewer_only=True),range(2)))
        other, = [c for c in prepared if c]
        self.assertNotEqual(other['viewer'],chosen['viewer'])
        self.assertNotEqual(other['receipt'],later.receipt)

    def test_two_unanswered_one_continuation_one_open_and_bounded_followup(self):
        for who in ('r','s','t','a'):
            source = self.post(who)
            self.say('Which colour do you like?' if who=='a' else 'Hi.')
        self.post('r','A returning speaker')
        self.now += .1
        self.post('x')
        self.now += .1
        self.post('y')
        answer = self.post('a','Green.',parent=source.message_id)
        chosen = self.say('What other colour do you like?')
        self.assertEqual(chosen['receipt'],answer.receipt)
        self.post('a','Blue.',parent=answer.message_id)
        for name in ('x','y','r'):
            self.assertEqual(self.say()['display_name'],name)
        lanes = self.values("""SELECT opportunity,lane FROM chat_candidates WHERE claimed_at IS NOT NULL
            ORDER BY claimed_at DESC LIMIT 4""")
        self.assertEqual(list(reversed(lanes)),[(x,x) for x in ('continuation','first','first','open')])
        self.assertEqual(self.values('SELECT asked FROM chat_candidates WHERE receipt=?',answer.receipt),[(0,)])

    def test_full_pool_admits_unanswered_speaker_and_explicit_wrong_parent_does_not_continue(self):
        self.inbox.capacity = 3
        for who in 'abcd':
            self.post(who)
            self.say('What do you think?' if who=='d' else 'Hi.')
        for who in 'abc':
            self.post(who)
        fresh = self.post('new')
        self.assertEqual(self.say()['receipt'],fresh.receipt)
        wrong = self.post('d','Green',parent='different-thread')
        for _ in range(3):
            self.say()
        self.assertNotEqual(self.values('SELECT lane FROM chat_candidates WHERE receipt=?',wrong.receipt),
                            [('continuation',)])

    def test_group_playout_never_credits_a_person_or_populates_personal_memory(self):
        self.post('a','What is this song?')
        self.post('b','この曲の名前は？')
        self.post('c','What is your favourite song?')
        choice = self.say()
        self.assertEqual(choice['reply_kind'],'group')
        self.assertTrue(choice['viewer'].startswith('audience:'))
        self.assertEqual(self.values('SELECT count(*) FROM memory_jobs WHERE turn_id=?',choice['receipt']),[(0,)])
        self.assertEqual(self.values('SELECT count(*) FROM relationship_events WHERE played_turn_id=?',choice['receipt']),[(0,)])
        with self.store.connect() as db:
            result = report(db)['broadcasts'][0]['comments']
        self.assertEqual((result['received'],result['played']),(3,1))
        selection = result['selection']
        self.assertEqual((selection['eligible_speakers'],selection['individually_replied_speakers'],
                          selection['group_replies'],selection['group_members_covered']),(3,0,1,1))
        self.assertNotEqual(self.say().get('reply_kind'),'group')

    def test_removing_a_group_member_revokes_prepared_audio_and_releases_survivors(self):
        topics=[self.post(who,'What is this song?') for who in 'abc']
        chosen=self.inbox.take(viewer_only=True)
        self.assertTrue(self.inbox.bind(chosen['receipt'],'prepared',prefetched=True))
        members=[c for c in topics if c.receipt!=chosen['receipt']]
        self.post(members[1].display_name,'newer question')
        self.inbox.remove('twitch','channel',members[0].message_id)
        self.assertTrue(self.inbox.removed_turn('prepared'))
        self.assertFalse(self.inbox.playable(chosen['receipt']))
        self.inbox.finish(chosen['receipt'],'cancelled','source_deleted')
        self.assertEqual(self.values("SELECT count(*) FROM comment_inbox WHERE status='queued'"),[(1,)])
        self.assertEqual(self.inbox.take()['comment'],'newer question')
