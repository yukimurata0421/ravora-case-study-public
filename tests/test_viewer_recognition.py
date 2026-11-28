from dataclasses import astuple, replace
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
import unittest

from ravora.comments import Comment, CommentInbox, normalize_youtube
from ravora.conversation import MemoryStore
from ravora.database import MIGRATIONS
from ravora.persona import storage_direction
from ravora.ray_character import CharacterRuntime
from ravora.ray_personality import RAY_PERSONALITY
try:
    from ravora.twitch_chat import MESSAGE, TwitchReader
except ModuleNotFoundError as exc:
    if exc.name not in ('requests', 'websocket'):
        raise
    TwitchReader = None


class ViewerTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.now = time.time()
        self.store = MemoryStore(self.root/'history.sqlite3')
        self.inbox = self.make_inbox('original')
        self.seq = 0

    def make_inbox(self,session):
        return CommentInbox(self.store,session=session,scope='cloud',clock=lambda:self.now)

    def post(self, name='Mia', author='100', words=None, *, inbox=None, stream='42', source='twitch'):
        self.seq += 1
        post=Comment(source,stream,str(self.seq),author,name,words or f'Post {self.seq}',self.now)
        (inbox or self.inbox).put(post)
        return post

    def bind(self,post):
        turn='turn-'+post.message_id
        with self.store.connect() as db:
            db.execute('UPDATE comment_inbox SET turn_id=? WHERE receipt=?',(turn,post.receipt))
        self.store.start(turn,'cloud:'+post.viewer,post.text)
        return turn

    def context(self,post):
        return self.store.relationship_context(post.viewer,scope='cloud',turn_id='turn-'+post.message_id,now=self.now)

    def lookup(self,**identity):
        return self.store.find_viewer('twitch',scope='cloud',now=self.now,**identity)

    def test_ingest_lookup_rename_collisions_scope_and_duplicate(self):
        a=self.post(); self.bind(a)
        self.assertEqual(self.inbox.put(a),'duplicate')
        self.assertEqual(self.lookup(author_id='100'),a.viewer)
        self.assertEqual(self.lookup(name='MIA'),a.viewer)
        b=self.post(author='200'); self.bind(b)
        self.assertIsNone(self.lookup(name='Mia'))
        self.assertEqual(self.context(b)['prior_contact'],'none_recorded')
        renamed=self.post(name='Kay');self.bind(renamed)
        self.assertEqual(self.lookup(name='Mia'),b.viewer)
        self.assertEqual(self.lookup(name='Kay'),a.viewer)
        self.assertEqual(self.context(a)['display_name'],'Mia')
        self.assertEqual(self.context(renamed)['display_name'],'Kay')
        self.assertIsNone(self.store.find_viewer('youtube',scope='cloud',name='Kay'))
        self.assertIsNone(self.store.find_viewer('twitch',scope='offline',author_id='100'))
        cli=Path(__file__).resolve().parents[1]/'scripts/memory.py'
        out=subprocess.check_output([sys.executable,'-B',str(cli),'viewer','--database',str(self.store.path),
                                     '--platform','twitch','--name','Kay'])
        self.assertEqual(json.loads(out),dict(matched=True,viewer_key=a.viewer))

    def test_missing_name_uses_prior_provider_name_but_does_not_copy_it(self):
        prior=self.post()
        now=self.post(name='');self.bind(now)
        self.assertEqual(self.context(now)['display_name'],'Mia')
        self.assertEqual(self.inbox.display_comments(now.receipt)[-1][1],'Mia')
        self.inbox.remove(prior.source,prior.stream_id,prior.message_id)
        self.assertNotIn('display_name',self.context(now))
        self.assertEqual(self.inbox.display_comments(now.receipt)[-1][1],'Viewer')

    def test_redelivery_can_fill_missing_metadata_without_counting_another_comment(self):
        first=self.post(name=''); self.bind(first)
        revision=self.inbox.display_revision
        self.assertEqual(self.inbox.put(replace(first,author_id='other',display_name='Wrong')),'duplicate')
        self.assertEqual(self.inbox.display_revision,revision)
        self.assertEqual(self.inbox.put(replace(first,display_name='Mia')),'duplicate')
        self.assertEqual(self.inbox.display_revision,revision+1)
        self.assertEqual(self.context(first)['display_name'],'Mia')
        self.assertEqual(self.store.relationship_snapshot(first.viewer,scope='cloud')['posted_comments'],1)
        self.inbox.remove(first.source,first.stream_id,first.message_id)
        self.inbox.put(replace(first,display_name='Mia'))
        self.assertIsNone(self.lookup(name='Mia'))

    @unittest.skipUnless(TwitchReader, 'youtube-live extra required')
    def test_provider_fallbacks_and_missing_id(self):
        reader=TwitchReader(self.inbox,self.root/'unused')
        stamp=datetime.fromtimestamp(self.now,timezone.utc).isoformat()
        meta={'subscription_type':MESSAGE,'message_timestamp':stamp}
        event={'broadcaster_user_id':'42','message_id':'e','chatter_user_id':'333',
               'chatter_user_login':'login_name','message':{'text':'Hello'}}
        reader._notification(meta,{'event':event},'42')
        self.assertEqual(self.lookup(name='login_name'),self.lookup(author_id='333'))
        self.assertIsNotNone(self.lookup(author_id='333'))
        reader._notification(meta,{'event':dict(event,message_id='no-id',chatter_user_id='')},'42')
        self.assertEqual(len(self.inbox.display_comments(None)),1)
        item={'id':'yt','snippet':{'liveChatId':'chat','type':'textMessageEvent','publishedAt':stamp,
                                  'authorChannelId':'yt-id','textMessageDetails':{'messageText':'Hello'}}}
        comment=normalize_youtube(item,'chat')
        self.assertEqual((comment.author_id,comment.display_name),('yt-id',''))
        self.inbox.put(comment)
        self.assertEqual(self.store.find_viewer('youtube',scope='cloud',author_id='yt-id'),comment.viewer)

    def test_name_request_opt_out_ttl_and_source_delete(self):
        alias=self.post(words='私を「ミア」と呼んで');self.bind(alias)
        self.assertEqual(self.context(alias)['preferred_name'],'ミア')
        self.now+=86401
        later=self.post(name='');self.bind(later)
        self.assertNotIn('display_name',self.context(later))
        self.assertEqual(self.context(later)['preferred_name'],'ミア')
        self.inbox.remove(alias.source,alias.stream_id,alias.message_id)
        self.assertNotIn('preferred_name',self.context(later))
        alias=self.post(words='call me Moon');self.bind(alias)
        self.post(words='名前を保存しないで')
        self.assertIsNone(self.lookup(name='Moon'))
        later=self.post();self.bind(later)
        self.assertNotIn('display_name',self.context(later))
        self.assertIsNone(self.lookup(name='Mia'))
        self.assertIsNotNone(self.lookup(author_id='100'))
        again=self.post(words='call me Kay');self.bind(again)
        self.assertEqual(self.context(again)['preferred_name'],'Kay')
        self.assertIsNone(self.lookup(name='Moon'))
        self.inbox.put(replace(later,display_name='Obsolete'))
        self.assertIsNone(self.lookup(name='Obsolete'))

    def test_same_slot_restart_other_slot_and_ack_order(self):
        sessions=('original','resumed','next')
        with self.store.connect() as db:
            for session in sessions:
                db.execute("""INSERT INTO studio_sessions(session,platform,mode,sample_rate,fps,prefetch,chat_source,started)
                              VALUES(?,'twitch','cloud',48000,30,1,'twitch',?)""",(session,self.now))
            db.executemany("INSERT INTO twitch_schedule_runs(scheduled_start,arm,claimed_at,result,session) VALUES(?,'a',0,'ok',?)",
                           [(10,'original'),(20,'next')])
            db.execute("INSERT INTO twitch_slot_restarts VALUES(10,1,'resumed')")
        first=self.post();tid=self.bind(first)
        self.store.finish(tid,'Hello','completed',{})
        second=self.post(inbox=self.make_inbox('resumed'));self.bind(second)
        self.assertEqual(self.context(first)['prior_contact'],'none_recorded')
        self.assertEqual(self.context(second)['prior_contact'],'same_broadcast')
        self.assertEqual(self.context(second)['prior_spoken_contact'],'none_recorded')
        self.assertTrue(self.store.played(tid,scope='cloud',memory_enabled=False))
        self.now=time.time()+1
        third=self.post(inbox=self.make_inbox('next'));self.bind(third)
        self.assertEqual(self.context(third)['prior_contact'],'another_broadcast_today')
        self.assertEqual(self.context(third)['prior_spoken_contact'],'previously_played')
        self.assertEqual(self.context(second)['prior_spoken_contact'],'none_recorded')
        self.assertEqual(self.store.history('cloud:'+second.viewer,4,turn_id='turn-'+second.message_id),[])
        self.inbox.remove(first.source,first.stream_id,first.message_id)
        self.assertEqual(self.context(third)['prior_spoken_contact'],'none_recorded')

    def test_long_gap_and_prior_answer_evidence(self):
        first=self.post(); self.bind(first)
        self.now += 20*86400
        next_post=self.post(stream='another',source='twitch');self.bind(next_post)
        self.assertEqual(self.context(next_post)['recent_pattern'],'reconnecting_after_a_gap')
        self.assertEqual(self.context(next_post)['prior_spoken_contact'],'none_recorded')
        other=self.post(author='999');self.bind(other)
        self.assertEqual(self.context(other)['prior_contact'],'none_recorded')

    def test_schema27_upgrade_registers_only_available_names_and_preserves_contact(self):
        legacy=self.root/'migrations'
        legacy.mkdir()
        for p in MIGRATIONS.glob('*.sql'):
            if int(p.name[:3])<=27:(legacy/p.name).symlink_to(p)
        path=self.root/'old.sqlite3'
        old=MemoryStore(path,migrations=legacy)
        inbox=CommentInbox(old,scope='cloud',clock=lambda:self.now)
        post=self.post(inbox=inbox); removed=self.post(name='Removed',author='101',inbox=inbox)
        inbox.remove(removed.source,removed.stream_id,removed.message_id)
        # Recreate the v27 state where repeats raised an operator's global interest.
        from ravora.ray_memory import CharacterMemory
        key=CharacterMemory(old,'cloud').set_preference('music',.6,kind='interest')
        with old.connect() as db:
            db.execute('UPDATE comment_inbox SET turn_id=? WHERE receipt=?',('old-turn',post.receipt))
        old.start('old-turn','cloud:'+post.viewer,post.text)
        with old.connect() as db:
            db.execute("UPDATE character_memories SET strength=.62 WHERE memory_id=?",(key,))
            db.execute('''INSERT INTO character_experiences(event_id,turn_id,memory_id,owner_hash,day_jst,reason,contribution,created_at) VALUES(?, 'old-turn', ?, 'owner', '2026-11-01','repeated_topic',.02,?)''',(post.receipt,key,self.now))
        for _ in range(2):
            upgraded=MemoryStore(path)
            self.assertEqual(upgraded.find_viewer('twitch',scope='cloud',name='Mia'),post.viewer)
            self.assertIsNone(upgraded.find_viewer('twitch',scope='cloud',name='Removed'))
            self.assertEqual(upgraded.relationship_snapshot(post.viewer,scope='cloud')['posted_comments'],1)
            with upgraded.connect() as db:
                self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(),[])
                self.assertAlmostEqual(db.execute('SELECT strength FROM character_memories WHERE memory_id=?',(key,)).fetchone()[0],.6)
                self.assertEqual(db.execute('SELECT contribution FROM character_experiences WHERE memory_id=?',(key,)).fetchone()[0],0)

    def test_future_or_mixed_source_notes_cannot_supply_a_memory(self):
        a=self.post(); b=self.post(); turn=self.bind(b); c=self.post()
        items=[]
        for sources in ([a],[a,c],[b]):
            key=self.store.candidate_memory(a.viewer,'Enjoys puzzles.',[item.receipt for item in sources],scope='cloud')
            items.append(dict(memory_id=key))
        self.assertEqual(self.store.prior_viewer_notes(items,b.viewer,'cloud',turn),items[:1])


class CharacterTests(unittest.TestCase):
    def test_weights_change_delivery_and_remain_private(self):
        self.assertEqual(astuple(RAY_PERSONALITY),(.9,.7,.8,.75,.85,.65,.8,.5))
        def plan(text,personality=RAY_PERSONALITY):
            ray=CharacterRuntime(personality=personality)
            ray.begin('one',text)
            return ray.plan(text)
        self.assertIn('their setback',plan('I lost the game.').delivery)
        self.assertIn('your setback',plan('You lost the game.').delivery)
        invited="Let's try a challenge."
        self.assertNotEqual(plan(invited).delivery,plan(invited,replace(RAY_PERSONALITY,accommodation=.95)).delivery)
        self.assertNotIn('0.85',plan(invited).instruction())
        self.assertNotIn('your setback',plan('What if you lost?').delivery)
        self.assertIn('correction',plan('That answer was incorrect.').delivery)

    def test_storage_explained_only_for_explicit_questions(self):
        for text in ('私のこと覚えてる？','Do you remember my name?', 'How long is a song?'):
            self.assertEqual(storage_direction(text,12),'')
        for text in ('コメントは何日で消える？','How long are my messages stored?'):
            policy=storage_direction(text,12)
            for part in ('24 hours','12 days','no automatic expiry','next successful','unavailable for conversational recall'):
                self.assertIn(part,policy)
