import asyncio
from dataclasses import replace
import datetime as dt
import json
from pathlib import Path
import random
import shutil
import sqlite3
import tempfile
import threading
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch
import uuid

from ravora.comments import Comment,CommentInbox,normalize_youtube
from ravora.conversation import ConversationBridge,MemoryStore,SpeechController
from ravora.database import Database,MIGRATIONS
from ravora.edge_grpc import EdgeClients
from ravora.fast_voice import FastVoiceSession
from ravora.fast_voice_server import MockVoiceClients
from ravora.input_pipeline import InputPipeline
from ravora.persona import conversation_prompt
from ravora.providers import GeminiText
from ravora.relationships import JST,RELATIONSHIP_RULES,RelationshipEvidence,day_number,relationship_text
from ravora.safety import TurnFence
from ravora.settings import ProviderSettings,SettingsError,load_settings
from support import HTTP,Reaction,Speech,Text


class RelationshipTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.now=time.time();self.serial=0
        self.store=MemoryStore(self.root/'state.sqlite3')
        self.inbox=CommentInbox(self.store,clock=lambda:self.now,capacity=2)

    def post(self,text='Hello!',*,author='viewer-a',source='youtube',inbox=None,evidence=None,published=None):
        self.serial+=1
        c=Comment(source,'stream',str(self.serial),author,'Same display name',text,self.now if published is None else published)
        (inbox or self.inbox).put(c,relationship_evidence=evidence)
        return c

    def state(self,c,*,scope='offline'):
        return self.store.relationship_snapshot(c.viewer,scope=scope,now=self.now)

    def event(self,c):
        with self.store.connect() as db:
            db.row_factory=sqlite3.Row
            return dict(db.execute('SELECT * FROM relationship_events WHERE source_event_id=?',(c.receipt,)).fetchone())

    def turn(self,c,*,scope='offline',status='completed',played=False):
        turn=uuid.uuid4().hex
        with self.store.connect() as db:db.execute('UPDATE comment_inbox SET turn_id=? WHERE receipt=?',(turn,c.receipt))
        with patch('ravora.conversation.time.time',return_value=self.now):
            self.store.start(turn,scope+':'+c.viewer,c.text);self.store.finish(turn,'Oh, tell me more!',status,{})
            if played:self.assertTrue(self.store.played(turn,scope=scope,memory_enabled=False))
        return turn

    def check_totals(self):
        with self.store.connect() as db:
            for scope,viewer,*totals in db.execute('SELECT * FROM viewer_relationships'):
                expected=db.execute('''SELECT SUM(posted_comments),SUM(eligible_comments),SUM(played_exchanges),
                    SUM(eligible_exchanges),SUM(eligible_comments>0),SUM(min(eligible_comments,3)+min(eligible_exchanges,2))
                    FROM relationship_days WHERE scope=? AND viewer_key=?''',(scope,viewer)).fetchone()
                self.assertEqual(tuple(totals),expected)
            self.assertFalse(db.execute('PRAGMA foreign_key_check').fetchall())

    def test_reingestion_and_reopen_are_idempotent(self):
        c=self.post();before=self.state(c)
        self.assertEqual(self.inbox.put(c),'duplicate')
        other=CommentInbox(MemoryStore(self.store.path),clock=lambda:self.now)
        self.assertEqual(other.put(c),'duplicate');self.assertEqual(self.state(c),before)
        self.assertEqual(before['posted_comments'],1);self.check_totals()

    def test_page_replay_is_idempotent_and_ban_removes_contribution(self):
        from support import message,page,CHAT,NOW
        inbox=CommentInbox(self.store,clock=lambda:NOW)
        item=message();inbox.accept_page(CHAT,page([item,item]));c=normalize_youtube(item,CHAT)
        inbox.accept_page(CHAT,page([item]));self.assertEqual(self.state(c)['posted_comments'],1)
        ban={'id':'ban','snippet':{'liveChatId':CHAT,'type':'userBannedEvent','userBannedDetails':{'bannedUserDetails':{'channelId':'channel-one'}}}}
        inbox.accept_page(CHAT,page([ban]));self.assertEqual(self.state(c)['eligible_comments'],0)

    def test_inbox_and_statistics_rollback_together(self):
        with patch.object(self.store,'record_relationship_comment',side_effect=RuntimeError('injected transaction failure')):
            with self.assertRaises(RuntimeError):self.post()
        with self.store.connect() as db:
            for table in ('source_events','comment_inbox','relationship_events','viewer_relationships'):
                self.assertEqual(db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0],0)

    def test_concurrent_duplicate_receipts_are_serialized(self):
        c=Comment('youtube','stream','same','viewer-a','name','Hi!',self.now);errors=[]
        def receive():
            try:self.inbox.put(c)
            except Exception as exc:errors.append(exc)
        workers=[threading.Thread(target=receive) for _ in range(4)]
        for worker in workers:worker.start()
        for worker in workers:worker.join(5);self.assertFalse(worker.is_alive())
        self.assertFalse(errors);self.assertEqual(self.state(c)['posted_comments'],1);self.check_totals()

    def test_a_burst_counts_posts_but_has_a_daily_cap(self):
        for index in range(100):c=self.post(f'Conversation item {index}.')
        state=self.state(c)
        self.assertEqual((state['posted_comments'],state['eligible_comments'],state['active_days']),(100,100,1))
        self.assertEqual(state['familiarity_units'],3);self.assertLess(state['familiarity'],.05)
        # Dropped queue work is still a posted interaction; it is not labelled spam.
        self.assertEqual(self.event(c)['reason'],'eligible');self.check_totals()

    def test_distributed_contact_outweighs_the_same_post_count_in_one_day(self):
        initial=self.now
        for i in range(100):burst=self.post(str(i),author='burst')
        for day in range(20):
            self.now=initial+day*86400
            for i in range(5):regular=self.post(f'Topic {day}-{i}',author='regular')
        a,b=self.state(burst),self.state(regular)
        self.assertEqual(a['posted_comments'],b['posted_comments']);self.assertEqual(b['active_days'],20)
        self.assertEqual(b['familiarity_units'],60);self.assertGreater(b['familiarity'],a['familiarity']*10)
        self.assertGreaterEqual(b['active_weeks_90'],3);self.check_totals()

    def test_short_rapid_replies_and_emoji_are_not_spam(self):
        for text in ('yes','no','👍','😂','!','?','ok','why?'):
            c=self.post(text);self.assertEqual(self.event(c)['reason'],'eligible');self.now+=.1
        self.assertEqual(self.state(c)['eligible_comments'],8)

    def test_repeated_content_is_a_separate_soft_exclusion_not_distrust(self):
        first=self.post('Same message');self.now+=.1;second=self.post('  SAME   message ')
        self.assertEqual(self.event(second)['reason'],'duplicate_content')
        self.assertEqual((self.state(first)['posted_comments'],self.state(first)['eligible_comments']),(2,1))
        self.assertNotIn('trust',self.state(first));self.assertEqual(self.state(first)['familiarity_units'],1)
        self.now+=121;third=self.post('Same message');self.assertEqual(self.event(third)['reason'],'eligible')

    def test_same_short_answer_to_a_new_spoken_reply_is_eligible(self):
        first=self.post('yes');self.turn(first,played=True);self.now+=1
        second=self.post('yes');self.assertEqual(self.event(second)['reason'],'eligible')
        self.assertEqual(self.state(second)['played_exchanges'],1)

    def test_generated_or_cancelled_reply_cannot_reset_duplicate_context(self):
        first=self.post('yes');turn=self.turn(first);self.store.cancel_turn(turn)
        second=self.post('yes');self.assertEqual(self.event(second)['reason'],'duplicate_content')
        self.assertFalse(self.store.played(turn,memory_enabled=False));self.assertEqual(self.state(first)['played_exchanges'],0)

    def test_only_first_playback_ack_counts_and_source_is_counted_once(self):
        c=self.post();turn=self.turn(c)
        self.assertEqual(self.state(c)['played_exchanges'],0)
        for _ in range(3):self.assertTrue(self.store.played(turn,memory_enabled=False))
        self.turn(c,played=True)
        self.assertEqual((self.state(c)['played_exchanges'],self.state(c)['eligible_exchanges']),(1,1))
        self.check_totals()

    def test_spam_and_actual_spoken_facts_are_separate(self):
        c=self.post('Buy our product!',evidence=RelationshipEvidence('spam','moderator'));self.turn(c,played=True)
        state=self.state(c)
        self.assertEqual((state['posted_comments'],state['eligible_comments'],state['played_exchanges'],state['eligible_exchanges']),(1,0,1,0))
        self.assertEqual(state['familiarity_units'],0);self.assertIsNone(self.store.relationship_context(c.viewer,scope='offline'))

    def test_semantic_evidence_can_exclude_and_restore_without_double_counting(self):
        c=self.post();self.turn(c,played=True)
        for kind,eligible,units in (('unrelated',0,0),('unrelated',0,0),('normal',1,2),('normal',1,2)):
            self.assertTrue(self.store.assess_relationship_event(c.receipt,c.viewer,scope='offline',evidence=RelationshipEvidence(kind,'semantic')))
            state=self.state(c);self.assertEqual((state['eligible_comments'],state['familiarity_units'],state['played_exchanges']),(eligible,units,1))
            self.check_totals()

    def test_conservative_promotion_detection_allows_ordinary_links(self):
        c=self.post('Subscribe to my channel https://example.test/channel');self.assertEqual(self.event(c)['reason'],'promotion')
        for text in ('Here is the article: https://example.test/article','What do you think of the phrase buy now?'):
            self.assertEqual(self.event(self.post(text))['reason'],'eligible')

    def test_external_facts_are_owner_and_scope_bound_and_cannot_supply_scores(self):
        c=self.post();evidence=RelationshipEvidence('spam','semantic')
        for viewer,scope in (('other','offline'),(c.viewer,'cloud')):
            self.assertFalse(self.store.assess_relationship_event(c.receipt,viewer,scope=scope,evidence=evidence))
        with self.assertRaises(ValueError):RelationshipEvidence('trust=80','semantic')
        with self.assertRaises(ValueError):RelationshipEvidence('normal','viewer')
        with self.assertRaises(ValueError):self.post(evidence={'score':80})
        self.assertEqual(self.state(c)['eligible_comments'],1)

    def test_old_future_and_startup_history_count_as_facts_without_credit(self):
        old=self.post(published=self.now-60);future=self.post(published=self.now+40)
        start=Comment('youtube','stream','startup','viewer-a','n','History',self.now-1)
        with self.store.connect() as db:self.inbox._put(db,start,warm_start=self.now)
        self.assertEqual(self.state(old)['posted_comments'],3);self.assertEqual(self.state(old)['eligible_comments'],0)
        self.assertEqual(self.event(future)['reason'],'future_timestamp')
        self.assertFalse(self.store.assess_relationship_event(old.receipt,old.viewer,scope='offline',evidence=RelationshipEvidence('normal','moderator')))

    def test_jst_midnight_and_recency_do_not_treat_absence_as_distrust(self):
        self.now=dt.datetime(2026,11,17,23,59,59,tzinfo=JST).timestamp();c=self.post()
        self.now+=2;self.post('Next day');self.assertEqual(self.state(c)['active_days'],2)
        self.now+=91*86400;state=self.state(c)
        self.assertEqual((state['active_days_30'],state['active_weeks_90'],state['days_since_last_contact']),(0,0,91))
        self.assertEqual(state['familiarity_units'],2);self.assertNotIn('trust',state)

    def test_first_returning_comment_keeps_previous_visit_gap_after_ingestion(self):
        c=self.post();self.now+=46*86400;self.post('Back today')
        state=self.state(c);self.assertEqual(state['days_since_last_contact'],0)
        self.assertEqual(state['return_gap_days'],46)
        background=self.store.relationship_context(c.viewer,scope='offline',now=self.now)
        self.assertEqual(background['recent_pattern'],'reconnecting_after_a_gap')
        self.assertTrue(all(isinstance(value,str) for value in background.values()))
        self.post('A second normal reply');self.assertIsNone(self.state(c)['return_gap_days'])

    def test_new_viewer_has_no_invented_return_interval(self):
        c=self.post();self.assertIsNone(self.state(c)['return_gap_days'])

    def test_model_background_omits_numeric_counts_and_visit_durations(self):
        c=self.post();self.turn(c,played=True);self.now+=46*86400;self.post('Hello again')
        data=self.store.relationship_context(c.viewer,scope='offline',now=self.now)
        self.assertEqual(data['prior_contact'],'across_prior_days')
        self.assertEqual(data['prior_spoken_contact'],'previously_played')
        self.assertTrue(all(isinstance(value,str) for value in data.values()))
        for key in ('interaction_days','spoken_exchanges','return_gap_days','days_since_last_contact','viewer_key'):
            self.assertNotIn(key,data)

    def test_scope_viewer_platform_and_private_developer_isolation(self):
        a=self.post('A');b=self.post('B',author='viewer-b')
        cloud=CommentInbox(self.store,clock=lambda:self.now,scope='cloud')
        self.post('Cloud',inbox=cloud)
        for platform in ('developer','developer-local','developer-mock'):
            owner=self.post('Private project and secret preferences',source=platform)
            self.turn(owner,played=True)
            self.assertIsNone(self.state(owner));self.assertIsNone(self.store.relationship_context(owner.viewer,scope='cloud'))
        other_platform=self.post('Twitch',source='twitch')
        self.assertEqual(self.state(a)['posted_comments'],1);self.assertEqual(self.state(a,scope='cloud')['posted_comments'],1)
        self.assertEqual(self.state(b)['posted_comments'],1);self.assertEqual(self.state(other_platform)['posted_comments'],1)
        background=relationship_text(self.store.relationship_context(a.viewer,scope='offline'))
        for value in (a.viewer,b.viewer,'Private project','secret preferences','viewer-b','trust','score'):
            self.assertNotIn(value,background)
        self.assertLess(len(background),600)

    def test_source_removal_retracts_contribution_and_blocks_late_ack(self):
        c=self.post();turn=self.turn(c,played=True)
        with self.store.connect() as db:self.store.remove_source(db,c.receipt,self.now)
        state=self.state(c);self.assertEqual((state['posted_comments'],state['eligible_comments'],state['played_exchanges'],state['familiarity_units']),(1,0,0,0))
        event=self.event(c);self.assertIsNone(event['fingerprint']);self.assertIsNone(event['played_turn_id'])
        self.assertEqual(event['reason'],'source_removed');self.assertFalse(self.store.played(turn,memory_enabled=False))
        self.assertEqual(self.inbox.put(c),'duplicate');self.check_totals()

    def test_raw_expiry_retains_only_contact_counts_and_rejects_late_ack(self):
        c=self.post('A private sounding comment never copied to relationship state.');turn=self.turn(c)
        self.now+=86401;self.store.expire(now=self.now)
        self.assertIsNone(self.event(c)['fingerprint']);self.assertEqual(self.state(c)['eligible_comments'],1)
        self.assertFalse(self.store.played(turn,memory_enabled=False))
        self.assertNotIn('private',json.dumps(self.store.relationship_context(c.viewer,scope='offline',now=self.now)))
        self.assertFalse(self.store.assess_relationship_event(c.receipt,c.viewer,scope='offline',evidence=RelationshipEvidence('normal','semantic')))

    def test_expired_but_not_yet_purged_source_cannot_gain_spoken_credit(self):
        c=self.post();turn=self.turn(c)
        with patch('ravora.conversation.time.time',return_value=self.now+86401):self.store.played(turn,memory_enabled=False)
        self.assertEqual(self.state(c)['played_exchanges'],0)

    def test_optout_is_not_promoted_by_semantic_normal_label(self):
        c=self.post("Don't remember this conversation.",evidence=RelationshipEvidence('normal','semantic'))
        self.assertEqual(self.event(c)['reason'],'opt_out');self.assertIsNone(self.event(c)['fingerprint'])
        self.assertFalse(self.store.assess_relationship_event(c.receipt,c.viewer,scope='offline',evidence=RelationshipEvidence('normal','moderator')))

    def test_daily_exchange_cap_and_aggregate_consistency_after_reclassification(self):
        values=[]
        for i in range(12):
            c=self.post(f'Answer {i}');values.append(c);self.turn(c,played=True);self.now+=1
        self.assertEqual(self.state(c)['familiarity_units'],5);self.assertEqual(self.state(c)['played_exchanges'],12)
        rng=random.Random(17)
        for c in rng.sample(values,8):
            self.store.assess_relationship_event(c.receipt,c.viewer,scope='offline',evidence=RelationshipEvidence('spam','moderator'));self.check_totals()
        for c in values:
            with self.store.connect() as db:self.store.remove_source(db,c.receipt,self.now)
            self.check_totals()
        self.assertEqual(self.state(c)['familiarity_units'],0)

    def test_new_migration_preserves_old_data_and_does_not_backfill_guesses(self):
        legacy=self.root/'v8';legacy.mkdir()
        for file in MIGRATIONS.glob('*.sql'):
            if int(file.name[:3])<9:shutil.copyfile(file,legacy/file.name)
        path=self.root/'old.sqlite3';old=Database(path,migrations=legacy)
        c=Comment('youtube','old','old-id','a','name','Historical',self.now)
        with old.connect() as db:self.store.record_source(db,c,self.now)
        with old.connect() as db:before=db.execute('SELECT * FROM source_events').fetchall()
        for _ in range(2):MemoryStore(path)
        with MemoryStore.existing(path).connect() as db:
            self.assertEqual(before,db.execute('SELECT * FROM source_events').fetchall())
            self.assertEqual(db.execute('SELECT COUNT(*) FROM relationship_events').fetchone()[0],0)
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0],'ok')

    def test_settings_disable_collection_and_clear_stale_provider_background(self):
        for settings in (replace(ProviderSettings(),relationship_enabled=False),replace(ProviderSettings(),memory_enabled=False)):
            pipeline=InputPipeline(settings,self.store,None,[]);c=self.post('Disabled',inbox=pipeline.inbox)
            self.assertIsNone(self.state(c))
        s=load_settings(self.root/'absent',environ={'RAVORA_RELATIONSHIP_ENABLED':'false'})
        self.assertFalse(s.relationship_enabled);self.assertFalse(s.public()['relationship_enabled'])
        with self.assertRaises(SettingsError):replace(ProviderSettings(),relationship_enabled=1).validate()
        c=self.post('Existing contact');http=HTTP(stream=self.wire('Your call.'));adapter=GeminiText(s,http)
        adapter.set_relationship({'stale':'another viewer'})
        self.run_controller(c,s,adapter)
        self.assertIsNone(adapter.relationship);self.assertNotIn('Viewer relationship',json.dumps(http.calls[0][2]))

    @staticmethod
    def wire(text):
        return ('data: '+json.dumps({'candidates':[{'finishReason':'STOP','content':{'parts':[{'text':text}]}}]})+'\n\n').encode()

    def run_controller(self,c,s,adapter):
        turn=uuid.uuid4().hex
        with self.store.connect() as db:db.execute('UPDATE comment_inbox SET turn_id=? WHERE receipt=?',(turn,c.receipt))
        controller=SpeechController(s,self.store,self.root,providers=(adapter,Reaction(),Speech()));events=[]
        try:metrics=controller.run(c.text,c.viewer,turn,events.append,threading.Event())
        finally:controller.memory_worker.close()
        self.assertEqual(events[-1]['kind'],'complete');return metrics

    def test_http_llm_receives_background_without_replacing_character_or_reply_style(self):
        earlier=self.post('I enjoyed that story.');self.turn(earlier,played=True)
        c=self.post('Want to hear my news?');s=ProviderSettings();http=HTTP(stream=self.wire('Go on, impress me.'))
        metrics=self.run_controller(c,s,GeminiText(s,http));payload=http.calls[0][2]
        prompt=payload['systemInstruction']['parts'][0]['text']
        self.assertTrue(prompt.startswith(conversation_prompt(s.persona,s.persona_file,reply_style=s.reply_style)))
        self.assertIn(RELATIONSHIP_RULES,prompt);self.assertEqual(payload['contents'][-1]['parts'][-1]['text'],c.text)
        self.assertIn('Viewer relationship background',json.dumps(payload))
        background=json.loads(next(part['text'] for part in payload['contents'][-1]['parts'] if part['text'].startswith('Viewer relationship')).split('\n',1)[1])
        self.assertEqual(background['prior_spoken_contact'],'previously_played')
        self.assertTrue(metrics['relationship_context_prepared']);self.assertLess(metrics['relationship_context_chars'],600)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT SUM(attempted) FROM provider_calls').fetchone()[0],0)
        # Generating this next reply does not itself count as another exchange.
        self.assertEqual(self.state(c)['played_exchanges'],1)

    def test_bridge_requires_completion_and_all_audio_claims_before_counting_exchange(self):
        c=self.post();bridge=ConversationBridge(ProviderSettings(),self.store,self.root,TurnFence(),threading.RLock(),providers=(Text(),Reaction(),Speech()))
        self.addCleanup(bridge.controller.memory_worker.close)
        def bind(token):
            with self.store.connect() as db:db.execute('UPDATE comment_inbox SET turn_id=? WHERE receipt=?',(token['turn_id'],c.receipt))
        token=bridge.begin(c.text,c.viewer,on_begin=bind);bridge.worker.join(5);self.assertFalse(bridge.worker.is_alive())
        self.assertFalse(bridge.mark_played(token));self.assertEqual(self.state(c)['played_exchanges'],0)
        for event in bridge.events:
            if event['kind']=='audio':self.assertIsNotNone(bridge.claim(token,event['index']))
        self.assertTrue(bridge.mark_played(token));self.assertTrue(bridge.mark_played(token))
        self.assertEqual(self.state(c)['played_exchanges'],1)

    def test_queries_use_indexes_and_recent_window_is_bounded(self):
        c=self.post()
        with self.store.connect() as db:
            queries=[('SELECT * FROM relationship_days WHERE scope=? AND viewer_key=? AND day BETWEEN ? AND ?',('offline',c.viewer,1,day_number(self.now))),
                ('SELECT day FROM relationship_days WHERE scope=? AND viewer_key=? AND eligible_comments>0 ORDER BY day DESC LIMIT 1',('offline',c.viewer)),
                ('SELECT * FROM relationship_events WHERE scope=? AND viewer_key=? AND fingerprint=? AND observed_at>=?',('offline',c.viewer,'a',self.now-120))]
            for query,args in queries:
                plan=' '.join(row[3] for row in db.execute('EXPLAIN QUERY PLAN '+query,args))
                self.assertIn('SEARCH',plan);self.assertNotIn('SCAN',plan)
        queries=[];original=self.store.connect
        def traced():
            connection=original();connection.db.set_trace_callback(queries.append);return connection
        with patch.object(self.store,'connect',traced):self.store.relationship_context(c.viewer,scope='offline',now=self.now)
        self.assertEqual(sum(q.startswith('SELECT') for q in queries),3)
        self.assertTrue(any('BETWEEN' in q and str(day_number(self.now)-89) in q for q in queries))


class AsyncRelationshipTests(unittest.IsolatedAsyncioTestCase):
    async def test_grpc_wire_keeps_free_response_and_character_with_bounded_background(self):
        captured=[]
        class Gen:
            Content=Part=GenerateContentRequest=GenerationConfig=staticmethod(lambda **kwargs:NS(**kwargs))
        class LLM:
            async def stream_generate_content(self,**kwargs):
                captured.append(kwargs['request'])
                async def replies():
                    yield NS(usage_metadata=None,candidates=[NS(index=0,content=NS(parts=[NS(text='You got me curious.',thought=False)]),finish_reason=NS(name='STOP'))])
                return replies()
        client=EdgeClients({'gemini_key':'not-a-key'});client.gen_types=Gen();client.llm=LLM()
        background={'contact_history':'recurring_contact','prior_contact':'across_prior_days','prior_spoken_contact':'previously_played'}
        persona=conversation_prompt('cheeky',reply_style='brief')
        self.assertEqual(''.join([p async for p in client.generate('Guess what?',prompt=persona,relationship=background)]),'You got me curious.')
        self.assertEqual(captured[0].system_instruction.parts[0].text,persona+RELATIONSHIP_RULES)
        self.assertEqual(captured[0].contents[-1].parts[-1].text,'Guess what?')
        self.assertIn('recurring_contact',captured[0].contents[-1].parts[0].text)

    async def test_fast_developer_path_has_no_viewer_profile_or_private_leak(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);store=MemoryStore(root/'state.sqlite3');calls=[]
            class Clients(MockVoiceClients):
                async def generate(self,text,**kwargs):
                    calls.append(kwargs)
                    async for part in super().generate(text,**kwargs):yield part
            async def send(*_):pass
            session=FastVoiceSession(ProviderSettings(),store,root,Clients(),send,send,mock=True)
            try:
                await session.start_text(uuid.uuid4().hex,'My unpublished private project.');await session.task
                self.assertEqual(session.phase,'generated');self.assertIsNone(calls[0]['relationship'])
                self.assertTrue(session.played(session.token,session.expected_frames))
                with store.connect() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM relationship_events').fetchone()[0],0)
            finally:await session.close()


if __name__=='__main__':unittest.main()
