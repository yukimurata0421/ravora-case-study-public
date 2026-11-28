import io
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from ravora.conversation import MemoryStore
from ravora.providers import GeminiStreamConnection, ProviderError
from ravora.ray_character import PublicPlan, PublicDecoder, topics_in, studio_public_wire, studio_wire_schema, CharacterError
from ravora.session_window import SessionWindow
from ravora.viewer_dialogue import apply_plan, direction, kind, session_topics, position_choices, grounded_choices, asks_choice_reason
from ravora.comments import Comment, CommentInbox
from ravora.studio_observations import StudioObservations

class ViewerDialogueTests(unittest.TestCase):
    def test_subject_change_stance_and_other_people(self):
        self.assertEqual(kind('音楽以外の話は？'),'shift')
        self.assertEqual(kind('このコートはほしい？'),'stance')
        self.assertEqual(kind('他の人とは今日何を話したの？'),'other_conversation')
        self.assertEqual(kind('なぜ星空が好きなの？'),'')
        self.assertEqual(kind('今日はどんな日だった？'),'day')
        self.assertEqual(kind('冬だね'),'season')
        self.assertEqual(kind('なんで？'),'reason')
        plan=PublicPlan('share_interest','joy',topic='music',project='music')
        mode=kind('面白い話をして')
        updated=apply_plan(plan,mode,direction(mode,[('music',3)],4))
        self.assertFalse(updated.topic)
        self.assertIn('music',updated.delivery)
        self.assertEqual(plan.topic,'music')

    def test_constrained_stance_only_for_simple_preference_questions(self):
        self.assertIn('欲しいよ。',position_choices('冬服はほしい？'))
        self.assertIn('うん、やってみたい。',position_choices('パズルを自分で作りたい？'))
        self.assertIn('I do like it.',position_choices('Do you like chess?'))
        for q in ('冬服がほしい。','私が何を好きか覚えてる？','彼女は冬服がほしい？',
                  '青と赤、どちらが好き？','なぜ花が好きなの？','Do you like tea or coffee?'):
            self.assertEqual(position_choices(q),(),q)

    def test_compact_fields_still_use_full_public_validation(self):
        choices=position_choices('冬服はほしい？')
        self.assertEqual(studio_wire_schema(choices)['properties']['s']['enum'],list(choices))
        frame=dict(s=choices[0],e='joy',m='none',l='ja',c=.99,u=False)
        decoder=PublicDecoder(structured=True,language_routing=True)
        self.assertEqual(decoder.feed(studio_public_wire(json.dumps(frame),choices),final=True)[0].speech,choices[0])
        self.assertEqual(decoder.language_result,('ja',.99))
        for bad in (dict(frame,s='unlisted'),dict(frame,other=True)):
            with self.assertRaises(CharacterError):studio_public_wire(json.dumps(bad),choices)
        for bad in (dict(frame,l='XX'),dict(frame,u=1),dict(frame,e='private'),dict(frame,s='private_thought')):
            with self.assertRaises(CharacterError):
                PublicDecoder(structured=True,language_routing=True).feed(studio_public_wire(json.dumps(bad)),final=True)
        with self.assertRaises(CharacterError):studio_public_wire('{"s":"a","s":"b"}')

    def test_stream_keeps_first_text_separate_from_validated_complete_speech(self):
        decoder=PublicDecoder(structured=True,language_routing=True,expand_wire=studio_public_wire)
        self.assertEqual(decoder.feed('{"s":"Hello",'),[])
        self.assertEqual(decoder.feed('"e":"joy","m":"none","l":"en","c":1}'),[])
        self.assertEqual(decoder.feed('',final=True)[0].speech,'Hello')
        self.assertEqual(decoder.language_result,('en',1))

    def test_scope_followup_uses_previous_question_and_generic_shift_gives_a_new_subject(self):
        prior=[('user','僕以外の人と何の話をした？'),('model','他の人との会話は分からない。')]
        choices=grounded_choices('','秘密？',prior,(),'ja')
        self.assertTrue(choices)
        self.assertTrue(all('参照' in s or '確かめ' in s for s in choices))
        self.assertFalse(grounded_choices('','秘密？',[('user','プレゼントは何？'),('model','まだ内緒。')],(),'ja'))
        starts=grounded_choices('shift','パズル以外の話はないの？',(),[('technology',3)],'ja')
        self.assertTrue(starts)
        self.assertTrue(all('パズル' not in s and 'エレベーター' not in s for s in starts))
        self.assertFalse(grounded_choices('shift','パズル以外に、料理の話をして',(),(),'ja'))

    def test_day_and_choice_reasons_use_this_chat(self):
        self.assertIn('puzzles', topics_in('do you like pazzle?'))
        self.assertNotIn('puzzles', topics_in('a nonpazzleword'))
        question = '今日はどんな日だった？'
        choices = grounded_choices('day', question, (), [('music', 2)], 'ja')
        self.assertTrue(choices and all('音楽' in text for text in choices))
        self.assertFalse(grounded_choices('day', question+'駅ではどうだった？', (), [('music', 2)], 'ja'))
        prior = [('user', 'パズルのどこが好きなの？'), ('model', '形がはまるところ。')]
        self.assertTrue(asks_choice_reason('なんでパズルなの？', prior))
        self.assertFalse(asks_choice_reason('なんで音楽なの？', prior))
        self.assertFalse(asks_choice_reason('なんで雨なの？', [('user','なんで晴れたの？'),('model','分からないよ。')]))

    def test_only_eligible_played_same_viewer_and_session_topics(self):
        with tempfile.TemporaryDirectory() as temp:
            store=MemoryStore(Path(temp)/'db')
            store.window=SessionWindow()
            inbox=CommentInbox(store,scope='cloud')
            obs=StudioObservations(inbox,'twitch',True,'twitch','chan',sample_rate=48000,fps=30)
            def add(idx, viewer, question, text, played=True):
                event=Comment('twitch','chan',str(idx),viewer,'Anon',question,time.time())
                self.assertEqual(inbox.put(event),'queued')
                t=str(idx)
                self.assertTrue(inbox.bind(inbox.take()['receipt'],t))
                binding={}
                store.start(t,'cloud:'+event.viewer,question,window_binding=binding)
                if played:
                    store.finish(t,text,'completed',{'session_window':{'revision':binding['revision']}})
                    store.played(t,scope='cloud',memory_enabled=False,relationship_enabled=False)
                    inbox.finish(event.receipt,'played')
                return binding,event
            try:
                _,one=add(1,'a','パズルは好き？','好き。')
                add(2,'b','星を見たい。','星座がいいな。')
                add(3,'a','冬服はほしい？','ほしいよ。')
                binding,_=add(4,'a','今日はどんな話をした？','',False)
                topics=dict(session_topics(store,binding,'4')[0])
                self.assertEqual(set(topics),{'puzzles','winter clothes'})
                with store.connect() as db:
                    db.execute('UPDATE source_events SET is_deleted=1 WHERE id=?',(one.receipt,))
                self.assertEqual(session_topics(store,binding,'4')[0],())
            finally:
                obs.close(0,'reading')
                store.window.close()
                inbox.close()

    def test_post_is_never_replayed_on_uncertain_delivery(self):
        s=SimpleNamespace(api_enabled=True,timeout=2)
        transport=GeminiStreamConnection(s)
        conn=MagicMock()
        conn.getresponse.side_effect=ConnectionResetError()
        transport._new=lambda:conn
        with self.assertRaisesRegex(ProviderError,'PROVIDER_CONNECTION_FAILED'):
            with transport.open(transport.origin+'model',{},{}):pass
        self.assertEqual(conn.request.call_count,1)
        conn.close.assert_called_once()
        self.assertIsNone(transport.idle)
        with self.assertRaisesRegex(ProviderError,'UNSUPPORTED_PROVIDER_ORIGIN'):
            with transport.open('https://elsewhere.invalid/',{},{}):pass

    def test_reuses_only_a_consumed_response(self):
        transport=GeminiStreamConnection(SimpleNamespace(api_enabled=True,timeout=2))
        conn=MagicMock()
        response=conn.getresponse.return_value
        response.status=200
        response.will_close=False
        response.isclosed.return_value=True
        transport._new=lambda:conn
        with transport.open(transport.origin+'model',{},{}):pass
        self.assertIs(transport.idle,conn)
        response.isclosed.return_value=False
        with patch('ravora.providers.select.select',return_value=([],[],[])):
            with transport.open(transport.origin+'model',{},{}):pass
        self.assertIsNone(transport.idle)
        conn.close.assert_called_once()
