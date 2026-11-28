from dataclasses import replace
from pathlib import Path
import json
import tempfile
import threading
import time
import unittest

from ravora.comments import Comment, CommentInbox
from ravora.conversation import MemoryStore, SpeechController
from ravora.providers import FixtureText, JevReaction
from ravora.settings import ProviderSettings, SettingsError, load_settings
from ravora.twitch_recall import asks_for_memory, candidates
from support import Speech


class TwitchRecallTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.store = MemoryStore(self.root/'db')
        self.inbox = CommentInbox(self.store)
        self.settings = ProviderSettings(twitch_recall_mode='jev')
        self.interview = self.note('owner', 'I have a job interview tomorrow.')
        self.other = self.note('another', 'I have a job interview in Berlin.')
        self.payloads = []

    def note(self, owner, text):
        event = Comment('twitch','previous',text,owner,'Same name',text,time.time())
        with self.store.connect() as db:
            self.store.record_source(db,event,time.time())
        key = self.store.candidate_memory(event.viewer,text,[event.receipt])
        self.store.approve_memory(key,reviewed_non_sensitive=True)
        return key

    def turn(self, text, *, wait=350, before_answer=None, score=.98):
        event = Comment('twitch','current','message','owner','Same name',text,time.time())
        self.inbox.put(event)
        self.inbox.bind(self.inbox.take()['receipt'],'now')
        class Transport:
            def json(_,url,headers,payload):
                self.payloads.append(payload)
                if before_answer:
                    before_answer()
                answer = {key:{'type':'noul','noul':.1} for key in ('joy','concern','surprise')}
                if 'viewer_memory' in payload['questions']:
                    answer['viewer_memory'] = {'type':'choice','choice':'e0','confidence':score,
                                               'probabilities':{'e0':score,'none':1-score}}
                return {'answers':answer}
        settings = replace(self.settings,semantic_wait_ms=wait)
        generator = FixtureText()
        controller = SpeechController(settings,self.store,self.root,
            providers=(generator,JevReaction(settings,Transport()),Speech()))
        self.addCleanup(controller.memory_worker.close)
        result = controller.run(text,event.viewer,'now',lambda event: None,threading.Event())
        self.assertGreater(result['chunks'],0)
        return result['twitch_recall'],generator.memories

    def test_lookup_excludes_future_and_other_scope(self):
        event = Comment('twitch','current','message','owner','Same name','Remember my interview?',time.time())
        self.inbox.put(event)
        self.inbox.bind(self.inbox.take()['receipt'],'now')
        self.store.start('now','offline:'+event.viewer,event.text)
        self.note('owner','My job interview just finished.')
        settings = replace(self.settings, semantic_candidate_limit=1)
        self.assertEqual([x['memory_id'] for x in candidates(self.store,event.viewer,event.text,settings,'now')],[self.interview])
        self.assertEqual(candidates(self.store,event.viewer,event.text,replace(settings,mode='cloud'),'now'),[])

    def test_mode_is_explicit(self):
        path = self.root/'settings'
        self.assertEqual(load_settings(path,environ={}).twitch_recall_mode,'off')
        self.assertEqual(load_settings(path,environ={'RAVORA_TWITCH_RECALL_MODE':'jev'}).twitch_recall_mode,'jev')
        with self.assertRaises(SettingsError):
            load_settings(path,environ={'RAVORA_TWITCH_RECALL_MODE':'unknown'})

    def test_approved_legacy_note_with_same_display_name(self):
        metrics, notes = self.turn('Do you remember my job interview?')
        self.assertEqual([m['memory_id'] for m in notes],[self.interview])
        self.assertEqual(metrics['outcome'],'selected')
        self.assertEqual(len(self.payloads),1)
        self.assertNotIn('Berlin',self.payloads[0]['state'])

    def test_revoked_during_selection(self):
        metrics, notes = self.turn('Do you remember my job interview?',
                                   before_answer=lambda:self.store.delete_memory(self.interview))
        self.assertEqual((metrics['outcome'],notes),('no_longer_eligible',[]))

    def test_late_judgment_does_not_change_prompt(self):
        metrics, notes = self.turn('Do you remember my job interview?',wait=10,before_answer=lambda:time.sleep(.1))
        self.assertEqual((metrics['outcome'],notes),('deadline',[]))

    def test_late_audit_survives_both_writer_orders(self):
        self.turn('Do you remember my job interview?', wait=10, before_answer=lambda:time.sleep(.1))
        with self.store.connect() as db:
            record=db.execute("SELECT call_id,decision,used_for_generation FROM semantic_retrievals WHERE turn_id='now'").fetchone()
        self.assertEqual(json.loads(record[1])['memory_id'],self.interview)
        self.assertEqual(record[2],0)
        self.store.record_semantic_retrieval('now','same query',[],None)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT call_id,decision,used_for_generation FROM semantic_retrievals WHERE turn_id='now'").fetchone(),record)

    def test_low_confidence_abstains(self):
        metrics, notes = self.turn('Do you remember my job interview?',score=.61)
        self.assertEqual((metrics['outcome'],notes),('abstained',[]))

    def test_ordinary_chat_has_no_selection_or_lookup(self):
        metrics, notes = self.turn('Hello!')
        self.assertEqual((notes,metrics['sqlite_ms'],metrics['wait_ms']),([],0,0))
        self.assertNotIn('viewer_memory',self.payloads[0]['questions'])
        self.assertTrue(asks_for_memory('この前話した楽器、覚えてる？'))
        self.assertFalse(asks_for_memory('今日は何してる？'))
        self.assertFalse(asks_for_memory('この話は覚えておいて。'))
        self.assertFalse(asks_for_memory("Please don't remember this."))
        self.assertFalse(asks_for_memory('Remember to drink water.'))
