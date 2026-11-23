import asyncio
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import shutil
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch
import uuid

from ravora.comments import Comment, CommentInbox
from ravora.conversation import MemoryStore, SpeechController
from ravora.database import Database, MIGRATIONS
from ravora.early_reaction import EarlyReactions
from ravora.fast_voice import FastVoiceSession
from ravora.fast_voice_server import MockVoiceClients
from ravora.gemini_memory import GeminiExtraction, GeminiMemoryExtractor
from ravora.memory_worker import MemoryWorker
from ravora.providers import GeminiText, JevFinalConnection, JevReaction, ProviderError
from ravora.semantic_memory import (EPISODE_POLICY, EPISODE_SCHEMA, EpisodeCandidate,
    encoded, parse_judgment, semantic_questions, load_speech_background, speech_background, validate_episodes)
from ravora.settings import ProviderSettings, SettingsError, load_settings
from test_gemini_memory import HTTP, response
from legacy_turn import insert_legacy_turn

INTERVIEW='I have a hiring interview next week.'


def candidate(text=INTERVIEW, **changes):
    return EpisodeCandidate(**{'evidence_quote':text,'subject_quote':'hiring interview',
        'event_state':'waiting','state_quote':'next week','time_quote':'next week',
        'tags':('job interview','recruitment'),'sensitivity':'non_sensitive',
        'previous_memory_id':'','relation':'new',**changes})


def seed(store, *, text=INTERVIEW, item=None, owner='viewer', platform='semantic-fixture', scope='offline', played=True, policy=EPISODE_POLICY):
    turn=uuid.uuid4().hex
    comment=Comment(platform,'stream',turn,owner,'Fixture',text,time.time())
    inbox=CommentInbox(store,scope=scope);inbox.put(comment);inbox.bind(inbox.take()['receipt'],turn)
    store.start(turn,scope+':'+comment.viewer,text);store.finish(turn,'Understood.','completed',{'synthetic':True})
    if played:store.played(turn,scope=scope,memory_policy=policy,memory_model='gemini-fixture')
    if item is not None:
        job=store.claim_memory_job(scope,policy=policy)
        store.complete_memory_job(job,GeminiExtraction((item,)))
    return turn,comment


def answer(candidates, *, reference='e0', relation='related_only', flow='unknown', progress='unknown', confidence=.95):
    labels={'memory_reference':reference,'memory_relation':relation,'topic_flow':flow,'event_progress':progress}
    values={}
    for name,q in semantic_questions(candidates).items():
        values[name]={'type':'choice','choice':labels[name],'confidence':confidence,
            'probabilities':{key:float(key==labels[name]) for key in q['criteria']}}
    values.update({key:{'type':'noul','noul':.1} for key in ('joy','surprise','concern')})
    return {'model':'jev-fixture','answers':values,'usage':{'input_tokens':123,'output_tokens':20,'total_tokens':143}}


class EpisodeTests(unittest.TestCase):
    def setUp(self):
        for name in ('socket.create_connection','socket.getaddrinfo'):
            guard=patch(name,side_effect=AssertionError('NETWORK_FORBIDDEN'));guard.start();self.addCleanup(guard.stop)
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.store=MemoryStore(self.root/'db.sqlite3')
        self.s=ProviderSettings(semantic_memory_mode='jev',memory_extractor='gemini')

    def candidates(self,viewer,query='It did not work out.'):
        return self.store.episode_candidates(viewer,query,scope='offline')

    def test_quote_backed_extraction_uses_existing_schema_transport_and_one_job(self):
        turn,c=seed(self.store)
        http=HTTP(response([candidate().__dict__]))
        settings=replace(self.s,api_enabled=True,memory_api_enabled=True,gemini_key='fixture')
        worker=MemoryWorker(self.store,settings,extractor=GeminiMemoryExtractor(settings,http));self.addCleanup(worker.close)
        worker.wake();self.assertTrue(worker.wait_for(c.viewer))
        self.assertEqual(len(http.calls),1)
        self.assertEqual(http.calls[0][2]['generationConfig']['responseJsonSchema'],EPISODE_SCHEMA)
        rows=self.candidates(c.viewer);self.assertEqual(rows[0]['summary'],'Viewer reported: '+INTERVIEW)
        self.assertEqual(rows[0]['event_state'],'waiting')
        self.assertEqual(rows[0]['state_kind'],'model_interpretation')
        self.store.played(turn,memory_policy=EPISODE_POLICY,memory_model='gemini-fixture');worker.wake()
        self.assertTrue(worker.wait_for(c.viewer));self.assertEqual(len(http.calls),1)

    def test_unplayed_reply_cannot_create_episode_or_candidate(self):
        turn,c=seed(self.store,played=False)
        self.assertIsNone(self.store.claim_memory_job('offline',policy=EPISODE_POLICY))
        self.assertEqual(self.candidates(c.viewer),[])
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT text FROM source_events').fetchone()[0],INTERVIEW)

    def test_no_word_overlap_still_offers_open_event(self):
        _,c=seed(self.store,item=candidate())
        self.assertEqual(len(self.candidates(c.viewer,'結局ダメだった')),1)
        self.assertEqual(self.candidates(c.viewer)[0]['subject_quote'],'hiring interview')

    def test_owner_and_scope_isolation(self):
        _,c=seed(self.store,item=candidate())
        self.assertEqual(self.candidates('different-viewer'),[])
        self.assertEqual(self.store.episode_candidates(c.viewer,'interview',scope='cloud'),[])

    def test_payload_count_and_serialized_size_are_bounded(self):
        _,c=seed(self.store,item=candidate())
        for i in range(20):
            text=f'I am waiting for project {i} to finish.'
            seed(self.store,text=text,item=candidate(text,subject_quote=f'project {i}',state_quote='waiting',time_quote=''))
        items=self.store.episode_candidates(c.viewer,'project',scope='offline',limit=2,max_chars=800)
        self.assertLessEqual(len(items),2);self.assertLessEqual(len(encoded(items)),800)

    def test_forged_quotes_states_and_model_ids_are_rejected(self):
        for changes in ({'evidence_quote':'I won my interview already.'},{'subject_quote':'cooking'},
                        {'event_state':'ended','state_quote':''},{'time_quote':'Tuesday'},
                        {'previous_memory_id':'forged','relation':'new'}, {'tags':('password',)}):
            with self.subTest(changes=changes):
                if changes=={'tags':('password',)}:
                    self.assertEqual(validate_episodes(GeminiExtraction((candidate(**changes),)),INTERVIEW).candidates,())
                else:
                    with self.assertRaises(ValueError):validate_episodes(GeminiExtraction((candidate(**changes),)),INTERVIEW)

    def test_japanese_quote_and_optout(self):
        text='来週、採用面接があります。'
        item=candidate(text,subject_quote='採用面接',state_quote='来週',time_quote='来週')
        self.assertEqual(len(validate_episodes(GeminiExtraction((item,)),text).candidates),1)
        self.assertEqual(validate_episodes(GeminiExtraction((item,)),text+'保存しないで').candidates,())

    def test_correction_is_proposed_until_owner_confirms_and_history_is_retained(self):
        _,c=seed(self.store,item=candidate());old=self.candidates(c.viewer)[0]
        text='Actually the hiring interview is tomorrow, not next week.'
        seed(self.store,text=text,item=candidate(text,previous_memory_id=old['memory_id'],relation='correction',
             state_quote='tomorrow',time_quote='tomorrow'))
        rows=self.candidates(c.viewer);new=next(r for r in rows if r['memory_id']!=old['memory_id'])
        self.assertEqual(len(rows),2);self.assertEqual(new['link_state'],'proposed')
        with self.assertRaises(ValueError):self.store.confirm_episode_link(new['memory_id'],old['memory_id'],viewer='other',scope='offline')
        self.store.confirm_episode_link(new['memory_id'],old['memory_id'],viewer=c.viewer,scope='offline')
        rows=self.candidates(c.viewer);self.assertEqual(len(rows),1);self.assertEqual(rows[0]['episode_id'],old['episode_id'])
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM memory_items').fetchone()[0],2)
            self.assertEqual(db.execute('SELECT summary FROM memory_items WHERE memory_id=?',(old['memory_id'],)).fetchone()[0],old['summary'])
        self.store.delete_memory(new['memory_id'])
        self.assertEqual(self.candidates(c.viewer),[])  # Never resurrect superseded reports.
        self.assertEqual(self.store.conversation_memories(c.viewer,'interview',self.s)[0],[])

    def test_model_cannot_link_another_viewer_or_non_offered_id(self):
        _,a=seed(self.store,item=candidate(),owner='other');foreign=self.candidates(a.viewer)[0]
        _,b=seed(self.store)
        job=self.store.claim_memory_job('offline',policy=EPISODE_POLICY)
        with self.assertRaisesRegex(ValueError,'PARENT'):
            self.store.complete_memory_job(job,GeminiExtraction((candidate(previous_memory_id=foreign['memory_id'],relation='update'),)))
        self.assertEqual(self.candidates(b.viewer),[])

    def test_deletion_expiry_and_source_removal_revalidate_candidates(self):
        for operation in ('memory','source','expiry'):
            turn,c=seed(self.store,item=candidate(),owner=operation);old=self.candidates(c.viewer)
            if operation=='memory':self.store.delete_memory(old[0]['memory_id'])
            elif operation=='source':
                with self.store.connect() as db:self.store.remove_source(db,c.receipt,time.time())
            else:
                with self.store.connect() as db:db.execute('UPDATE memory_items SET created_at=?,expires_at=? WHERE memory_id=?',(time.time()-20,time.time()-1,old[0]['memory_id']))
            self.assertEqual(self.store.revalidate_episode_candidates(c.viewer,'interview',self.s,old),[])
            if operation!='expiry':
                with self.store.connect() as db:self.assertEqual(db.execute('SELECT subject_quote FROM memory_episodes WHERE memory_id=?',(old[0]['memory_id'],)).fetchone()[0],'')

    def test_expired_raw_source_does_not_extend_approved_memory_ttl(self):
        _,c=seed(self.store,item=candidate())
        with self.store.connect() as db:db.execute('UPDATE source_events SET expires_at=?',(time.time()-1,))
        self.store.expire()
        self.assertEqual(len(self.candidates(c.viewer)),1)  # existing reviewed-summary retention contract
        self.assertEqual(self.store.semantic_previous_message(c.viewer,scope='offline'),'')

    def test_deleted_source_during_extraction_drops_late_result(self):
        _,c=seed(self.store);job=self.store.claim_memory_job('offline',policy=EPISODE_POLICY)
        with self.store.connect() as db:self.store.remove_source(db,c.receipt,time.time())
        self.assertFalse(self.store.complete_memory_job(job,GeminiExtraction((candidate(),))))
        self.assertEqual(self.candidates(c.viewer),[])

    def test_slow_old_extraction_does_not_become_the_newest_report(self):
        _,c=seed(self.store)
        older=self.store.claim_memory_job('offline',policy=EPISODE_POLICY)
        text='My hiring interview is tomorrow, not next week.'
        seed(self.store,text=text,item=candidate(text,state_quote='tomorrow',time_quote='tomorrow'))
        self.store.complete_memory_job(older,GeminiExtraction((candidate(),)))
        rows=self.candidates(c.viewer)
        self.assertGreater(next(r['created_at'] for r in rows if r['time_quote']=='tomorrow'),
                           next(r['created_at'] for r in rows if r['time_quote']=='next week'))

    def test_cancelled_playback_job_is_not_retried_after_timeout(self):
        _,c=seed(self.store)
        http=HTTP(error=ProviderError('PROVIDER_CONNECTION_FAILED'))
        s=replace(self.s,api_enabled=True,memory_api_enabled=True,gemini_key='fixture')
        worker=MemoryWorker(self.store,s,extractor=GeminiMemoryExtractor(s,http));self.addCleanup(worker.close)
        worker.wake();self.assertTrue(worker.wait_for(c.viewer));worker.wake();self.assertTrue(worker.wait_for(c.viewer))
        self.assertEqual(len(http.calls),1)
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT reason FROM memory_jobs').fetchone()[0],'OUTCOME_UNKNOWN')

    def test_choice_abstention_and_separate_topic_event_states(self):
        _,c=seed(self.store,item=candidate());items=self.candidates(c.viewer)
        decision=parse_judgment(answer(items,reference='unrelated',flow='switch'),items)
        self.assertEqual(decision['topic_flow'],'switch');self.assertEqual(decision['event_progress'],'unknown')
        self.assertEqual(self.store.conversation_memories(c.viewer,'Let us play a game.',self.s,judgment=decision)[0],[])
        self.assertEqual(self.candidates(c.viewer)[0]['event_state'],'waiting')
        low=parse_judgment(answer(items,confidence=.4),items)
        self.assertEqual(low['reference'],'uncertain');self.assertIsNone(low['memory_id'])
        unsupported=parse_judgment(answer(items,reference='e0',progress='resolved',relation='unknown'),items)
        self.assertEqual(unsupported['event_progress'],'unknown')
        self.assertEqual(unsupported['reported_event_progress'],'resolved')

    def test_preferences_remain_available_alongside_episodes(self):
        _,c=seed(self.store,item=candidate())
        key=self.store.candidate_memory(c.viewer,'Viewer likes coffee.',[c.receipt],tags=['coffee'])
        self.store.approve_memory(key,reviewed_non_sensitive=True)
        notes,_=self.store.conversation_memories(c.viewer,'coffee',self.s)
        self.assertIn(key,[n['memory_id'] for n in notes])

    def test_legacy_controller_shares_one_jev_call_and_keeps_playback_gate(self):
        from support import Speech
        _,c=seed(self.store,item=candidate());candidates=self.candidates(c.viewer)
        http=HTTP(answer(candidates));generator=GeminiText(self.s,http)
        controller=SpeechController(self.s,self.store,self.root,providers=(generator,JevReaction(self.s,http),Speech()))
        self.addCleanup(controller.memory_worker.close)
        events=[];turn=uuid.uuid4().hex
        controller.run('It fell through.',c.viewer,turn,events.append,threading.Event())
        self.assertEqual(events[-1]['kind'],'complete',events[-1])
        self.assertEqual(len(http.calls),1)
        self.assertEqual(len(http.calls[0][2]['questions']),7)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT played FROM conversation_turns WHERE turn_id=?',(turn,)).fetchone()[0],0)
            self.assertIsNone(db.execute('SELECT 1 FROM memory_jobs WHERE turn_id=?',(turn,)).fetchone())

    def test_malformed_choice_never_creates_false_certainty(self):
        _,c=seed(self.store,item=candidate());items=self.candidates(c.viewer)
        for value in (True,float('nan'),2):
            raw=answer(items);raw['answers']['memory_reference']['confidence']=value
            with self.assertRaises(ValueError):parse_judgment(raw,items)
        raw=answer(items);raw['answers']['memory_reference']['choice']='e999'
        with self.assertRaises(ValueError):parse_judgment(raw,items)

    def test_combined_jev_request_preserves_reaction_and_usage(self):
        _,c=seed(self.store,item=candidate());items=self.candidates(c.viewer)
        http=HTTP(answer(items));adapter=JevReaction(self.s,http)
        result=adapter.classify('It fell through.',candidates=items,previous_message=INTERVIEW)
        self.assertEqual(len(http.calls),1);self.assertEqual(len(http.calls[0][2]['questions']),7)
        self.assertEqual(result['reaction'],'neutral');self.assertEqual(result['semantic']['memory_relation'],'related_only')
        self.assertEqual(adapter.usage['input_tokens'],123)
        self.assertIn('current_message',http.calls[0][2]['questions']['joy']['instructions'])
        self.assertEqual(self.store.conversation_memories(c.viewer,'It fell through.',self.s,judgment=result['semantic'])[0],[])
        self.assertNotIn(items[0]['memory_id'],http.calls[0][2]['state'])

    def test_invalid_semantic_response_keeps_valid_reaction(self):
        _,c=seed(self.store,item=candidate());items=self.candidates(c.viewer)
        raw=answer(items);del raw['answers']['topic_flow']
        result=JevReaction(self.s,HTTP(raw)).classify('Hello',candidates=items)
        self.assertEqual(result['reaction'],'neutral');self.assertEqual(result['semantic']['reference'],'unavailable')

    def test_schema_v10_upgrade_preserves_every_existing_row_and_backup(self):
        legacy=self.root/'legacy';legacy.mkdir()
        for p in MIGRATIONS.glob('*.sql'):
            if int(p.name[:3])<=10:shutil.copy2(p,legacy/p.name)
        path=self.root/'old.sqlite3';Database(path,migrations=legacy);old=MemoryStore.existing(path,readonly=False)
        turn=uuid.uuid4().hex
        comment=Comment('semantic-fixture','stream',turn,'viewer','Fixture',INTERVIEW,time.time())
        inbox=CommentInbox(old,scope='offline')
        inbox.put(comment);inbox.bind(inbox.take()['receipt'],turn)
        insert_legacy_turn(old,comment,turn,'Understood.',played=False)
        with old.connect() as db:
            tables=[r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE '%fts%' AND name!='schema_migrations'")]
            projections={name:','.join(r[1] for r in db.execute('PRAGMA table_info('+name+')')) for name in tables}
            queries={name:'SELECT '+columns+' FROM '+name for name,columns in projections.items()}
            before={name:db.execute(query).fetchall() for name,query in queries.items()}
        migrated=MemoryStore(path)
        with migrated.connect() as db:
            self.assertEqual(before,{name:db.execute(query).fetchall() for name,query in queries.items()})
            self.assertEqual(db.execute('SELECT usage_session FROM conversation_turns').fetchone(),(inbox.session,))
            self.assertEqual(db.execute('SELECT COUNT(*) FROM memory_episodes').fetchone()[0],0)
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(),[])
        backup=self.root/'backup.sqlite3';migrated.backup(backup)
        self.assertEqual(MemoryStore(backup).status()['integrity_check'],['ok'])

    def test_preconnected_final_sends_only_on_dispatch_and_never_retries_an_unknown(self):
        transport=JevFinalConnection(replace(self.s,api_enabled=True,stt_api_enabled=True))
        connection=MagicMock()
        connection.getresponse.side_effect=ConnectionResetError()
        with patch.object(transport,'connect',return_value=connection) as factory:
            transport.warm()
            connection.connect.assert_called_once_with()
            connection.request.assert_not_called()
            with self.assertRaisesRegex(ProviderError,'CONNECTION_FAILED'):
                transport.json(transport.url,{}, {'synthetic':True})
            with self.assertRaisesRegex(ProviderError,'CLOSED'):
                transport.json(transport.url,{}, {'synthetic':True})
            self.assertEqual(factory.call_count,1)
            self.assertEqual(connection.request.call_count,1)
            connection.close.assert_called_once_with()

    def test_disabled_transport_cannot_even_warm_a_socket(self):
        transport=JevFinalConnection(self.s)
        with patch.object(transport,'connect') as factory:
            transport.warm()
            with self.assertRaisesRegex(ProviderError,'API_DISABLED'):
                transport.json(transport.url,{}, {})
            factory.assert_not_called()

    def test_cancelling_a_tls_handshake_discards_late_socket(self):
        transport=JevFinalConnection(replace(self.s,api_enabled=True,stt_api_enabled=True))
        connection=MagicMock()
        connection.connect.side_effect=transport.close
        with patch.object(transport,'connect',return_value=connection):
            transport.warm()
        connection.close.assert_called_once_with()
        connection.request.assert_not_called()
        with self.assertRaisesRegex(ProviderError,'CLOSED'):
            transport.json(transport.url,{},{})

    def test_uncertain_continuation_cannot_reintroduce_an_event_through_history(self):
        history=[('user',INTERVIEW),('model','A guess about a job offer.')]
        context=[{'tier':1,'text':'An earlier interview.'}]
        for reference in ('uncertain','unrelated','unavailable'):
            judged={'reference':reference,'topic_flow':'continuation'}
            self.assertEqual(speech_background(self.s,'It fell through.',True,judged,history,context),([],[]))
        chosen={'topic_flow':'continuation','memory_id':'episode','memory_relation':'same_event'}
        self.assertEqual(speech_background(self.s,'And then?',True,chosen,history,context),([],[]))
        self.assertEqual(speech_background(self.s,'And then?',True,chosen,history,context,
            selected=[{'retrieval':'semantic_candidate','memory_id':'episode'}]),([history[0]],[]))
        self.assertEqual(speech_background(self.s,'Can you repeat that?',True,None,history,context),(history,[]))
        self.assertEqual(speech_background(self.s,'Hello',False,None,history,context),(history,context))

    def test_local_recap_and_assent_keep_session_history_even_when_jev_abstains(self):
        from ravora.memory_extraction import session_recap, acknowledgment
        history=[('user','Please keep this off the stream.'),('model','Understood.')]
        def no_old_context():
            self.fail('session recap should not read older context')
        for text in ('What did I? Talk with the before.', 'What were we talking about?', 'Oh, do you remember it?', 'Yeah, sure.'):
            self.assertEqual(load_speech_background(self.s,text,True,None,lambda:history,no_old_context), (history,[]))
        self.assertFalse(session_recap('What do you talk about on stream?'))
        self.assertFalse(session_recap('What did I mention last week?'))
        self.assertFalse(acknowledgment('No, thanks.'))

    def test_abstention_skips_unused_context_queries_but_reads_requested_history(self):
        history=MagicMock(return_value=[('user','A statement.'),('model','A reply.')])
        context=MagicMock(return_value=[{'tier':1}])
        self.assertEqual(load_speech_background(self.s,'That was funny.',True,None,history,context),([],[]))
        history.assert_not_called()
        context.assert_not_called()
        self.assertEqual(load_speech_background(self.s,'Can you repeat that?',True,None,history,context),
                         (history.return_value,[]))
        history.assert_called_once_with()
        context.assert_not_called()

    def test_new_settings_are_opt_in_and_bounded(self):
        self.assertEqual(ProviderSettings().semantic_memory_mode,'off')
        self.assertEqual(load_settings(Path('/nonexistent'),environ={'RAVORA_SEMANTIC_MEMORY_MODE':'jev'}).semantic_memory_mode,'jev')
        for change in ({'semantic_wait_ms':501},{'semantic_candidate_limit':13},{'semantic_max_chars':799},{'semantic_memory_mode':'magic'}):
            with self.assertRaises(SettingsError):replace(self.s,**change).validate()


class SemanticVoiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.store=MemoryStore(self.root/'voice.sqlite3');self.events=[];self.prompts=[]
        self.settings=ProviderSettings(semantic_memory_mode='jev',semantic_wait_ms=40,memory_extractor='local_rules')
        async def send(value):self.events.append(value)
        async def pcm(*args):pass
        client=MockVoiceClients();original=client.generate
        async def generate(text,**kw):
            self.prompts.append(kw)
            async for delta in original(text,**kw):yield delta
        client.generate=generate
        self.session=FastVoiceSession(self.settings,self.store,self.root,client,send,pcm,mock=True)
        self.addAsyncCleanup(self.session.close)
        # Seed through the real source/job APIs with this session's owner identity.
        _,comment=seed(self.store,item=candidate(),platform='developer-mock',owner=self.settings.developer_id)
        self.viewer=comment.viewer

    async def test_fast_path_semantics_share_final_call_and_do_not_duplicate_requests(self):
        async def classify(early,text):
            return {'reaction':'neutral','semantic':parse_judgment(answer(early.candidates,relation='same_event'),early.candidates)},{}
        with patch.object(EarlyReactions,'_classify',classify):
            await self.session.start_text(uuid.uuid4().hex,'It did not work out.')
            await asyncio.wait_for(self.session.task,2)
        self.assertEqual(self.session.phase,'generated',self.events)
        self.assertEqual(self.prompts[-1]['memories'][0]['retrieval'],'semantic_candidate')
        self.assertEqual(self.session.early.phase_requests['final'],1)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM provider_calls WHERE provider='jev'").fetchone()[0],1)
            self.assertEqual(db.execute('SELECT used_for_generation FROM semantic_retrievals').fetchone()[0],1)
        self.assertNotIn('semantic',next(e for e in self.events if e['kind']=='reaction'))

    async def test_recorded_transcripts_get_distinct_listener_instructions_after_abstention(self):
        cases=[
            ("So that didn't go as planned.",'neutral','a detail in their current words'),
            ('That interview in the racing game was hilarious.','joy','Leave the people, dialogue, cause and punchline unspecified'),
        ]
        for transcript,reaction,expected in cases:
            async def classify(early,text):
                self.assertEqual(text,transcript)
                judgment=parse_judgment(answer(early.candidates,reference='uncertain'),early.candidates)
                return {'reaction':reaction,'semantic':judgment},{}
            with patch.object(EarlyReactions,'_classify',classify):
                await self.session.start_text(uuid.uuid4().hex,transcript)
                await asyncio.wait_for(self.session.task,2)
            self.assertEqual(self.session.phase,'generated',self.events)
            self.assertIn(expected,self.prompts[-1]['prompt'])
            self.assertEqual(self.prompts[-1]['memories'],[])
            self.assertTrue(self.session.played(self.session.token,self.session.expected_frames))

    async def test_slow_judgment_does_not_delay_generation_or_rewrite_prompt(self):
        gate=asyncio.Event()
        async def classify(early,text):
            await gate.wait()
            return {'reaction':'neutral','semantic':parse_judgment(answer(early.candidates),early.candidates)},{}
        with patch.object(EarlyReactions,'_classify',classify):
            await self.session.start_text(uuid.uuid4().hex,'It did not work out.')
            await asyncio.wait_for(self.session.task,1)
            self.assertEqual(self.session.phase,'generated',self.events)
            self.assertIn('a detail in their current words',self.prompts[-1]['prompt'])
            for key in ('memories','history','context'):
                self.assertEqual(self.prompts[-1][key],[])
            await self.session.cancel();gate.set();await asyncio.sleep(.02)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT used_for_generation FROM semantic_retrievals').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM memory_episodes').fetchone()[0],1)

    async def test_semantic_voice_keeps_tts_closed_until_generated_text(self):
        gate=asyncio.Event()
        awaiting_token=asyncio.Event()
        original=self.session.clients.generate
        async def slow_generate(*args,**kwargs):
            awaiting_token.set()
            await gate.wait()
            async for delta in original(*args,**kwargs):
                yield delta
        self.session.clients.generate=slow_generate
        await self.session.start_text(uuid.uuid4().hex,'It fell through.')
        await asyncio.wait_for(awaiting_token.wait(),1)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM provider_calls WHERE provider='tts'").fetchone()[0],0)
        gate.set()
        await asyncio.wait_for(self.session.task,2)
        self.assertEqual(self.session.phase,'generated',self.events)

    async def test_pending_memory_job_does_not_hold_fast_reply(self):
        with patch.object(self.session.memory,'wait_for',side_effect=AssertionError('SHOULD_NOT_WAIT')):
            await self.session.start_text(uuid.uuid4().hex,'Hello.')
            await asyncio.wait_for(self.session.task,1)
        self.assertEqual(self.session.phase,'generated',self.events)

    async def test_deletion_during_judgment_excludes_candidate_before_generation(self):
        async def classify(early,text):
            result=parse_judgment(answer(early.candidates),early.candidates)
            self.store.delete_memory(early.candidates[0]['memory_id'])
            return {'reaction':'neutral','semantic':result},{}
        with patch.object(EarlyReactions,'_classify',classify):
            await self.session.start_text(uuid.uuid4().hex,'It did not work out.')
            await asyncio.wait_for(self.session.task,2)
        self.assertEqual(self.prompts[-1]['memories'],[])


if __name__=='__main__':unittest.main()
