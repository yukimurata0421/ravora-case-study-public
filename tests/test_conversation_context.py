import asyncio
from dataclasses import replace
import io
import json
from pathlib import Path
import shutil
import tempfile
import threading
import time
from types import SimpleNamespace as NS
import unittest
import uuid

from ravora.comments import Comment,CommentInbox
from ravora.context_prompt import CONTEXT_RULES,context_text
from ravora.conversation import MemoryStore,SpeechController
from ravora.conversation_context import developer_keys
from ravora.database import Database,MIGRATIONS
from ravora.edge_grpc import EdgeClients
from ravora.fast_voice import FastVoiceSession
from ravora.fast_voice_server import MockVoiceClients
from ravora.providers import GeminiText
from ravora.settings import ProviderSettings,SettingsError,load_settings
from support import HTTP,Reaction,Speech


class ContextTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.store=MemoryStore(self.root/'context.sqlite3');self.owner=developer_keys('local-developer')[0]
    def source(self,question,*,author='viewer-a',platform='youtube',store=None,relationships=True):
        store=store or self.store;c=Comment(platform,'chat',uuid.uuid4().hex,author,'display',question,time.time())
        inbox=CommentInbox(store,relationships_enabled=relationships);self.assertEqual(inbox.put(c),'queued');inbox.take();inbox.claimed(c.receipt)
        turn=uuid.uuid4().hex;self.assertTrue(inbox.bind(c.receipt,turn));return c,turn
    def turn(self,question,answer,*,author='viewer-a',platform='youtube',scope='offline',played=True,store=None,relationships=True):
        store=store or self.store;c,key=self.source(question,author=author,platform=platform,store=store,relationships=relationships)
        store.start(key,scope+':'+c.viewer,question);store.finish(key,answer,'completed',{})
        if played:self.assertTrue(store.played(key,scope=scope,memory_enabled=False,relationship_enabled=relationships))
        return c,key
    def seed(self):
        self.turn('I prefer concise explanations about coffee.','I will keep coffee answers brief.',author='local-developer',platform='developer')
        for i in range(5):self.turn(f'How should I prepare coffee number {i}?',f'I previously suggested coffee method {i}.')
    def recall(self,viewer=None,**kwargs):return self.store.recall_context(viewer or self.owner,'coffee',scope='offline',developer_id='local-developer',**kwargs)
    def memory(self,*,author='local-developer',platform='developer',scope='offline'):
        c,_=self.turn('I prefer coffee. My unpublished project is Hazel.','Thanks for telling me.',author=author,platform=platform,scope=scope)
        key=self.store.candidate_memory(c.viewer,'Developer said they prefer coffee.',[c.receipt],scope=scope)
        self.store.approve_memory(key,reviewed_non_sensitive=True);return c,key
    def test_tiers_order_bound_payload_and_deduplicate_turns(self):
        self.seed();entries=self.recall()
        self.assertEqual(set(e['tier'] for e in entries),{1,2,3});self.assertEqual([e['tier'] for e in entries],sorted(e['tier'] for e in entries))
        self.assertLessEqual(len(context_text(entries)),3600);self.assertLessEqual(len(entries),8)
        ids=[e['source_turn_id'] for e in entries];self.assertEqual(len(ids),len(set(ids)))
        self.assertEqual(entries[0]['kind'],'developer_dialogue')
    def test_public_viewer_cannot_read_private_owner_or_other_viewer(self):
        self.seed();c,_=self.turn('My own coffee question.','Your own coffee answer.',author='current-viewer')
        entries=self.recall(c.viewer);text=context_text(entries)
        self.assertNotIn('concise explanations',text);self.assertNotIn('method 4',text);self.assertNotIn(1,[e['tier'] for e in entries])
        self.assertIn('own coffee',text)
    def test_only_explicitly_reviewed_summary_is_shared_not_raw_dialogue(self):
        c,key=self.memory();public=Comment('youtube','x','x','public','p','coffee',time.time()).viewer
        self.assertFalse(self.recall(public))
        with self.assertRaises(ValueError):self.store.share_context_memory(key,developer_id='local-developer')
        share=self.store.share_context_memory(key,developer_id='local-developer',reviewed_non_sensitive=True)
        self.assertEqual(share,self.store.share_context_memory(key,developer_id='local-developer',reviewed_non_sensitive=True))
        entries=self.recall(public);self.assertEqual([e['tier'] for e in entries],[1]);self.assertEqual(entries[0]['share_id'],share)
        self.assertIn('prefer coffee',context_text(entries));self.assertNotIn('Hazel',context_text(entries))
        self.assertEqual(self.store.revoke_context_share(key),1);self.assertFalse(self.recall(public))
        with self.store.connect() as db:self.assertIsNotNone(db.execute('SELECT revoked_at FROM context_shares').fetchone()[0])
    def test_sharing_rejects_other_author_and_detects_changed_summary(self):
        _,other=self.memory(author='viewer-a',platform='youtube')
        with self.assertRaises(ValueError):self.store.share_context_memory(other,developer_id='local-developer',reviewed_non_sensitive=True)
        _,key=self.memory();self.store.share_context_memory(key,developer_id='local-developer',reviewed_non_sensitive=True)
        with self.store.connect() as db:db.execute('UPDATE memory_items SET summary=? WHERE memory_id=?',('Changed after review.',key))
        self.assertFalse(self.recall('another-viewer'))
    def test_reviewed_turn_summary_can_be_shared_without_calling_an_extractor(self):
        c,key=self.turn('I prefer coffee. Project Hazel is private.','Thanks.',author='local-developer',platform='developer')
        with self.assertRaises(ValueError):self.store.share_turn_context(key,'Developer prefers coffee.',developer_id='local-developer')
        result=self.store.share_turn_context(key,'Developer prefers coffee.',developer_id='local-developer',reviewed_non_sensitive=True)
        self.assertTrue(result['memory_id']);text=context_text(self.recall('public-viewer'))
        self.assertIn('prefers coffee',text);self.assertNotIn('Hazel',text)
        _,other=self.turn('coffee','answer')
        with self.assertRaises(ValueError):self.store.share_turn_context(other,'summary',developer_id='local-developer',reviewed_non_sensitive=True)
    def test_failed_unplayed_cancelled_and_deleted_turns_are_not_recalled(self):
        c,key=self.turn('coffee','Unplayed coffee answer.',played=False)
        self.assertFalse(self.recall());self.store.cancel_turn(key);self.assertFalse(self.recall())
        c,key=self.turn('coffee','Deleted coffee answer.')
        with self.store.connect() as db:self.store.remove_source(db,c.receipt,time.time())
        self.assertFalse(self.recall())
    def test_expiry_removes_raw_index_but_approved_shared_summary_can_outlive_raw(self):
        _,key=self.memory();self.store.share_context_memory(key,developer_id='local-developer',reviewed_non_sensitive=True)
        self.store.expire(now=time.time()+86401)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM conversation_fts WHERE conversation_fts MATCH 'Hazel'").fetchone()[0],0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM conversation_context').fetchone()[0],0)
        entries=self.recall('public-viewer');self.assertEqual(len(entries),1);self.assertEqual(entries[0]['kind'],'shared_developer_memory')
    def test_scope_and_sensitive_or_opt_out_text_are_excluded(self):
        self.turn('coffee','Cloud-only coffee answer.',scope='cloud')
        self.turn('coffee','Email me at private@example.invalid.')
        self.turn('Do not remember this coffee conversation.','I understand.')
        self.assertFalse(self.recall())
    def test_fts_operators_are_quoted_and_sql_is_not_executed(self):
        self.seed();result=self.store.recall_context(self.owner,'coffee" OR * NEAR( ) DROP TABLE source_events;',scope='offline',developer_id='local-developer')
        self.assertTrue(result)
        with self.store.connect() as db:self.assertGreater(db.execute('SELECT COUNT(*) FROM source_events').fetchone()[0],0)
    def test_new_schema_backfills_existing_replies_without_changing_rows(self):
        directory=self.root/'v5';directory.mkdir()
        for path in MIGRATIONS.glob('*.sql'):
            if int(path.name[:3])<=5:shutil.copy2(path,directory/path.name)
        path=self.root/'old.sqlite3';Database(path,migrations=directory);old=MemoryStore.existing(path,readonly=False)
        # Seed the v5 fixture using only mechanisms available before the v9 migration.
        self.turn('Remember espresso?','An existing espresso reply.',store=old,relationships=False)
        with old.connect() as db:before=db.execute('SELECT * FROM conversation_turns').fetchall()
        upgraded=MemoryStore(path)
        with upgraded.connect() as db:
            self.assertEqual(before,db.execute('SELECT * FROM conversation_turns').fetchall())
            self.assertEqual(db.execute("SELECT COUNT(*) FROM conversation_fts WHERE conversation_fts MATCH 'espresso'").fetchone()[0],1)
            self.assertEqual(db.execute('SELECT DISTINCT tier FROM conversation_context ORDER BY tier').fetchall(),[(2,),(3,)])
    def test_legacy_llm_wire_payload_and_saved_reply_have_context_and_audit(self):
        self.seed();c,turn=self.source('Please explain coffee.',author='local-developer',platform='developer')
        output='A short coffee answer.'
        wire=('data: '+json.dumps({'candidates':[{'finishReason':'STOP','content':{'parts':[{'text':output}]}}]})+'\n\n').encode()
        http=HTTP(stream=wire);adapter=GeminiText(ProviderSettings(),http)
        controller=SpeechController(ProviderSettings(),self.store,self.root,providers=(adapter,Reaction(),Speech()))
        events=[]
        try:controller.run(c.text,c.viewer,turn,events.append,threading.Event())
        finally:controller.memory_worker.close()
        self.assertEqual(events[-1]['kind'],'complete')
        payload=http.calls[0][2];self.assertIn(CONTEXT_RULES,payload['systemInstruction']['parts'][0]['text'])
        entries=json.loads(next(p['text'] for p in payload['contents'][-1]['parts'] if p['text'].startswith('Retrieved conversation context')).split('\n',1)[1])
        self.assertEqual(set(e['tier'] for e in entries),{1,2,3})
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT reply,played FROM conversation_turns WHERE turn_id=?',(turn,)).fetchone(),(output,0))
            self.assertEqual(db.execute('SELECT COUNT(*) FROM context_retrievals WHERE turn_id=? AND context_prepared=1',(turn,)).fetchone()[0],len(entries))
            self.assertEqual(db.execute('SELECT SUM(attempted) FROM provider_calls').fetchone()[0],0)
    def test_context_settings_can_be_disabled_and_validate_limits(self):
        for values in ({'context_max_chars':0},{'context_max_items':1},{'context_enabled':1}):
            with self.assertRaises(SettingsError):replace(ProviderSettings(),**values).validate()
        s=load_settings(self.root/'absent',environ={'RAVORA_CONTEXT_ENABLED':'false'})
        self.assertFalse(s.context_enabled)


class AsyncContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_fast_voice_consumes_all_tiers_and_saves_played_answer_for_next_turn(self):
        helper=ContextTests();helper.setUp();self.addCleanup(helper.tmp.cleanup);helper.seed();captured=[];events=[]
        class Capture(MockVoiceClients):
            async def generate(self,text,**kwargs):
                captured.append(kwargs['context'])
                yield 'I will give you a concise coffee answer.'
        async def send(event):events.append(event)
        async def pcm(token,data):pass
        s=FastVoiceSession(ProviderSettings(memory_extractor='local_rules'),helper.store,helper.root,Capture(),send,pcm,mock=True)
        try:
            await s.start_text(uuid.uuid4().hex,'What about coffee?');await asyncio.wait_for(s.task,5)
            self.assertEqual(s.phase,'generated',events);self.assertEqual(set(e['tier'] for e in captured[0]),{1,2,3})
            token=s.token;self.assertTrue(s.played(token,s.expected_frames))
            with helper.store.connect() as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM conversation_context WHERE turn_id=?',(token['turn_id'],)).fetchone()[0],2)
                self.assertGreater(db.execute('SELECT COUNT(*) FROM context_retrievals WHERE turn_id=? AND context_prepared=1',(token['turn_id'],)).fetchone()[0],0)
                self.assertEqual(db.execute('SELECT SUM(attempted) FROM provider_calls').fetchone()[0],0)
        finally:await s.close()
    async def test_grpc_request_uses_same_priority_contract_without_opening_network(self):
        captured=[]
        class Gen:
            Content=Part=GenerateContentRequest=GenerationConfig=staticmethod(lambda **kwargs:NS(**kwargs))
        class LLM:
            async def stream_generate_content(self,**kwargs):
                captured.append(kwargs['request'])
                async def replies():
                    yield NS(usage_metadata=None,candidates=[NS(index=0,content=NS(parts=[NS(text='A reply.',thought=False)]),finish_reason=NS(name='STOP'))])
                return replies()
        client=EdgeClients({'gemini_key':'not-a-key'});client.gen_types=Gen();client.llm=LLM()
        context=[{'tier':1,'kind':'developer_dialogue','text':'Keep replies concise.'},{'tier':3,'kind':'assistant_answer','text':'A previously spoken answer.'}]
        self.assertEqual(''.join([x async for x in client.generate('Hello.',context=context)]),'A reply.')
        self.assertIn(CONTEXT_RULES,captured[0].system_instruction.parts[0].text)
        self.assertIn('Keep replies concise.',captured[0].contents[-1].parts[0].text)


if __name__=='__main__':unittest.main()
