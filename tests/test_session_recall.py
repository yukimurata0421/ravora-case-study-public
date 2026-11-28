from pathlib import Path
import tempfile
import time
import unittest

from ravora.comments import Comment, CommentInbox
from ravora.conversation import MemoryStore
from ravora.viewer_recall import candidates, recent_reference
from ravora.session_window import SessionWindow, validated
from ravora.settings import ProviderSettings


class WindowTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = MemoryStore(Path(folder.name)/'ray.sqlite')
        self.now = [0.]
        self.window = SessionWindow(clock=lambda: self.now[0],capacity=2)
        self.store.window = self.window
        self.addCleanup(self.window.close)
        self.s = ProviderSettings(mode='cloud', twitch_recall_mode='session')
        self.inbox = CommentInbox(self.store,scope='cloud')
        self.addCleanup(self.inbox.close)
        self.seq = 0

    def turn(self, *, user='alpha', slot='s1', scope='cloud', text=None, play=True):
        self.seq += 1
        ident = str(self.seq)
        c = Comment('twitch',slot,ident,user,'same display',text or 'I painted a cup '+ident,time.time())
        binding = {}
        self.assertEqual(self.inbox.put(c),'queued')
        self.assertTrue(self.inbox.bind(self.inbox.take()['receipt'],ident))
        self.store.start(ident,scope+':'+c.viewer,c.text,window_binding=binding)
        self.store.finish(ident,'A painted cup '+ident,'completed',{})
        if play:
            self.store.played(ident,scope=scope,memory_enabled=False,relationship_enabled=False)
        self.inbox.finish(c.receipt,'played' if play else 'cancelled')
        return c,binding

    def test_owner_slot_scope_only_played_and_bound(self):
        current,b = self.turn()
        self.turn(play=False)
        self.turn(user='beta')
        self.assertEqual([r.source for r in self.window.previous(b['key'],time.time())],[current.receipt])
        self.assertEqual(self.window.previous(b['key'],b['received']),())
        self.assertEqual(self.window.previous((b['key'][0],'s2'),time.time()),())
        self.assertEqual(self.window.previous(('offline:'+current.viewer,'s1'),time.time()),())

    def test_four_turns_idle_touch_and_discard(self):
        for i in range(6):
            _,b = self.turn()
        self.assertEqual([r.turn for r in self.window.previous(b['key'],time.time())],['3','4','5','6'])
        self.now[0]=599
        self.assertEqual(len(self.window.previous(b['key'],time.time(),touch=True)),4)
        self.now[0]=1200
        self.assertEqual(self.window.previous(b['key'],time.time()),())
        c,_ = self.turn()
        self.window.close()
        self.assertEqual(self.window.previous(b['key'],time.time()),())
        self.store.played(str(self.seq),scope='cloud',memory_enabled=False,relationship_enabled=False)
        self.assertFalse(self.window.entries)

    def test_lru_and_private_turn(self):
        _,a = self.turn()
        self.turn(user='beta')
        self.turn(user='gamma')
        self.assertFalse(self.window.previous(a['key'],time.time()))
        _,b = self.turn(user='delta',text='off the record, I collect mugs')
        self.assertFalse(self.window.previous(b['key'],time.time()))

    def test_japanese_optout_excluded(self):
        _,b = self.turn(text='ここだけの話、星が好き。')
        self.assertFalse(self.window.previous(b['key'],time.time()))

    def test_external_revocation_and_edit(self):
        c,b = self.turn()
        rows=self.window.previous(b['key'],time.time())
        self.assertEqual(validated(self.store,b['key'],rows),rows)
        with self.store.connect() as db:
            db.execute('UPDATE conversation_turns SET reply=? WHERE turn_id=?',('corrected','1'))
        self.assertEqual(validated(self.store,b['key'],rows),())
        c,b = self.turn()
        rows = self.window.previous(b['key'],time.time())
        with self.store.connect() as db:
            db.execute('UPDATE source_events SET is_deleted=1 WHERE id=?',(c.receipt,))
        self.assertEqual(validated(self.store,b['key'],rows),())
        with self.store.connect() as db:
            self.store.remove_source(db,c.receipt,time.time())
        self.assertFalse(self.window.entries)

    def test_indirect_context_revoked_by_second_writer(self):
        source,_ = self.turn(text='I play accordion.')
        _,b = self.turn(text='Tell me a story.')
        rows = self.window.previous(b['key'],time.time())
        # Keep only an exchange whose source itself has not changed.
        derived = rows[-1:]
        self.assertEqual(validated(self.store,b['key'],derived),derived)
        second = MemoryStore.existing(self.store.path,readonly=False)
        with second.connect() as db:
            second.remove_source(db,source.receipt,time.time())
        self.assertEqual(validated(self.store,b['key'],derived),())
        self.assertEqual(self.turn(text='Next turn',play=False)[1]['revision']>derived[0].revision,True)

    def test_delayed_play_ack_after_revocation_is_not_cached(self):
        _,b = self.turn()
        c,b = self.turn(play=False)
        self.store.finish(str(self.seq),'A derived reply','completed',{'session_window':{'revision':b['revision']}})
        with self.store.connect() as db:
            db.execute("UPDATE conversation_turns SET reply='Edited by another writer' WHERE turn_id='1'")
        self.store.played(str(self.seq),scope='cloud',memory_enabled=False,relationship_enabled=False)
        self.assertNotIn(c.receipt,[r.source for r in self.window.previous(b['key'],time.time())])

    def test_generic_two_topics_and_deleted_note(self):
        c,_ = self.turn(text='Chess club meets on Saturday.')
        chess=self.store.candidate_memory(c.viewer,c.text,[c.receipt],tags=['chess'],scope='cloud')
        self.store.approve_memory(chess,reviewed_non_sensitive=True)
        c,_ = self.turn(text='Drama club meets on Monday.')
        drama=self.store.candidate_memory(c.viewer,c.text,[c.receipt],tags=['drama'],scope='cloud')
        self.store.approve_memory(drama,reviewed_non_sensitive=True)
        query='Compare chess and drama: which comes earlier in the week?'
        self.turn(text=query,play=False)
        notes=candidates(self.store,c.viewer,query,self.s,str(self.seq),multiple=True)
        self.assertEqual({x['memory_id'] for x in notes},{chess,drama})
        self.store.delete_memory(chess)
        self.assertFalse(self.window.entries)
        self.assertNotIn(chess,[n['memory_id'] for n in candidates(self.store,c.viewer,query,self.s,str(self.seq),multiple=True)])

    def test_immediate_and_mixed_time_references(self):
        self.assertTrue(recent_reference('さっき言った名前は？'))
        self.assertTrue(recent_reference('What did I just mention in my last message?'))
        self.assertFalse(recent_reference('さっき言った日と先週話した日は同じ？'))
        self.assertFalse(recent_reference('Compare last week with what I just said.'))

    def test_timer_discards_without_new_input(self):
        self.store.window = actual = SessionWindow(idle=.04)
        self.addCleanup(actual.close)
        self.turn()
        self.assertTrue(actual.entries)
        limit=time.monotonic()+1
        while actual.entries and time.monotonic()<limit:
            time.sleep(.01)
        self.assertFalse(actual.entries)
