import concurrent.futures
from contextlib import closing
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest

from ravora.comments import Comment, CommentInbox
from ravora.conversation import MemoryStore, SpeechController
from ravora.database import Database, MIGRATIONS, SCHEMA_VERSION
from ravora.settings import ProviderSettings
from support import Text, Reaction, Speech, HTTP
from ravora.providers import GeminiText


def copy_schema(source,destination):
    # Schema tests need SQL contents, not an installed directory's permission bits.
    destination.mkdir()
    for path in source.glob('*.sql'):shutil.copyfile(path,destination/path.name)


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.path=self.root/'db.sqlite3'

    def source(self,store,*,name='one',author='viewer',platform='youtube-fixture',text='I enjoy coffee.'):
        comment=Comment(platform,'chat',name,author,'Same name',text,time.time())
        inbox=CommentInbox(store);inbox.put(comment)
        return comment,inbox

    def approved(self,store,comment,**kwargs):
        key=store.candidate_memory(comment.viewer,'Viewer said they enjoy coffee.',[comment.receipt],tags=['coffee'],**kwargs)
        store.approve_memory(key,reviewed_non_sensitive=True);return key

    def test_populated_legacy_database_is_migrated_without_changing_original_ledger(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.executescript('''CREATE TABLE conversation_turns(id TEXT PRIMARY KEY,viewer TEXT NOT NULL,comment TEXT NOT NULL,reply TEXT NOT NULL DEFAULT '',status TEXT NOT NULL,played INTEGER NOT NULL DEFAULT 0,started TEXT NOT NULL,metrics TEXT NOT NULL DEFAULT '{}');
                CREATE TABLE speech_usage(id TEXT PRIMARY KEY,month TEXT NOT NULL,characters INTEGER NOT NULL,status TEXT NOT NULL);
                CREATE TABLE provider_calls(id TEXT PRIMARY KEY,turn_id TEXT NOT NULL,provider TEXT NOT NULL,operation TEXT NOT NULL,state TEXT NOT NULL,attempted INTEGER NOT NULL DEFAULT 0,prepared REAL NOT NULL,updated REAL NOT NULL,summary TEXT NOT NULL,code TEXT);''')
            db.execute('INSERT INTO conversation_turns VALUES(?,?,?,?,?,?,?,?)',('old','offline:v','comment','reply','completed',1,'2026-11-15T00:00:00+00:00','{"mode":"offline"}'))
            db.execute('INSERT INTO speech_usage VALUES(?,?,?,?)',('reservation','2026-11',540,'reserved_or_unknown'))
            db.execute('INSERT INTO provider_calls VALUES(?,?,?,?,?,?,?,?,?,?)',('call','old','gemini','generate','outcome_unknown',1,10,11,'{}','PROVIDER_CONNECTION_FAILED'))
        store=MemoryStore(self.path);MemoryStore(self.path)
        with store.connect() as db:
            self.assertEqual(db.execute('SELECT id,comment,reply,status,played FROM conversation_turns').fetchall(),[('old','comment','reply','completed',1)])
            self.assertEqual(db.execute('SELECT SUM(characters),status FROM speech_usage').fetchone(),(540,'reserved_or_unknown'))
            self.assertEqual(db.execute('SELECT attempted,state FROM provider_calls').fetchone(),(1,'outcome_unknown'))
            self.assertEqual(db.execute('SELECT turn_id,closed_at,state FROM conversation_turns').fetchone(),('old',None,'COMPLETED'))
            self.assertEqual(db.execute('SELECT model,input_tokens,cost_estimate,latency_ms,status FROM api_usage').fetchone(),(None,None,None,None,'outcome_unknown'))
            self.assertEqual(db.execute('SELECT COUNT(*) FROM schema_migrations').fetchone()[0],SCHEMA_VERSION)

    def test_connections_enforce_design_pragmas_and_reject_orphans(self):
        store=MemoryStore(self.path)
        status=store.status()
        self.assertEqual([status[x] for x in ('journal_mode','foreign_keys','busy_timeout','synchronous')],['wal',1,5000,2])
        self.assertEqual(status['integrity_check'],['ok']);self.assertEqual(status['foreign_key_errors'],0)
        with self.assertRaises(sqlite3.IntegrityError),store.connect() as db:
            db.execute("INSERT INTO memory_sources(memory_id,event_id) VALUES('missing','missing')")
        with self.assertRaises(sqlite3.IntegrityError),store.connect() as db:
            db.execute("INSERT INTO utterances(utterance_id,turn_id,chunk_index,text,status,created_at) VALUES('u','missing',0,'text','generated',1)")

    def test_failed_migration_rolls_back_schema_and_preserves_version_and_rows(self):
        store=MemoryStore(self.path);store.start('turn','viewer','comment')
        directory=self.root/'migrations';copy_schema(MIGRATIONS,directory)
        (directory/f'{SCHEMA_VERSION+1:03d}_invalid.sql').write_text('CREATE TABLE should_rollback(id INTEGER);\nTHIS IS INVALID;\n')
        with self.assertRaises(sqlite3.OperationalError):Database(self.path,migrations=directory)
        with store.connect() as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],SCHEMA_VERSION)
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='should_rollback'").fetchone())
            self.assertEqual(db.execute('SELECT comment FROM conversation_turns').fetchone()[0],'comment')

    def test_applied_migration_checksum_and_newer_database_are_not_silently_reset(self):
        store=MemoryStore(self.path)
        directory=self.root/'migrations';copy_schema(MIGRATIONS,directory)
        with (directory/'001_memory.sql').open('a') as f:f.write('\n-- changed\n')
        with self.assertRaisesRegex(ValueError,'APPLIED_MIGRATION_CHANGED'):Database(self.path,migrations=directory)
        with store.connect() as db:db.execute('PRAGMA user_version=99')
        with self.assertRaisesRegex(ValueError,'SCHEMA_NEWER'):MemoryStore(self.path)
        with store.connect() as db:self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],99)

    def test_two_initializers_serialize_and_do_not_double_apply_migration(self):
        barrier=threading.Barrier(2)
        def init(_):barrier.wait();return MemoryStore(self.path).status()['user_version']
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as workers:self.assertEqual(list(workers.map(init,range(2))),[SCHEMA_VERSION,SCHEMA_VERSION])
        with MemoryStore(self.path).connect() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM schema_migrations').fetchone()[0],SCHEMA_VERSION)

    def test_candidates_need_review_and_never_leak_across_viewer_platform_or_mode(self):
        store=MemoryStore(self.path);comment,_=self.source(store)
        key=store.candidate_memory(comment.viewer,'Viewer said they enjoy coffee.',[comment.receipt])
        self.assertEqual(store.search_memory(comment.viewer,'coffee'),[])
        with self.assertRaisesRegex(ValueError,'REQUIRES_SENSITIVITY_REVIEW'):store.approve_memory(key)
        store.approve_memory(key,reviewed_non_sensitive=True)
        result=store.search_memory(comment.viewer,'coffee')
        self.assertEqual(result[0]['claim_kind'],'viewer_report');self.assertEqual(result[0]['sources'][0]['event_id'],comment.receipt)
        other,_=self.source(store,name='two',author='other')
        self.assertEqual(store.search_memory(other.viewer,'coffee'),[])
        same_name,_=self.source(store,name='three',platform='twitch-fixture')
        self.assertEqual(store.search_memory(same_name.viewer,'coffee'),[])
        self.assertEqual(store.search_memory(comment.viewer,'coffee',scope='cloud'),[])
        with self.assertRaisesRegex(ValueError,'SOURCE_UNAVAILABLE'):store.candidate_memory(other.viewer,'Likes coffee',[comment.receipt])

    def test_sensitive_values_are_rejected_before_memory_is_saved(self):
        store=MemoryStore(self.path);comment,_=self.source(store)
        for text in ['My email is hello@example.test','My password is abc','Call +1 555 123 4567']:
            with self.assertRaisesRegex(ValueError,'SENSITIVE_MEMORY'):store.candidate_memory(comment.viewer,text,[comment.receipt])
        with store.connect() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM memory_items').fetchone()[0],0)

    def test_returning_channel_can_retrieve_memory_across_streams(self):
        store=MemoryStore(self.path);first,_=self.source(store);key=self.approved(store,first)
        next_stream=Comment(first.source,'another-chat','new-event',first.author_id,'Renamed',first.text,time.time())
        inbox=CommentInbox(store);inbox.put(next_stream)
        self.assertEqual(store.search_memory(next_stream.viewer,'coffee')[0]['memory_id'],key)

    def test_fts_quotes_input_and_handles_updates_deletes_tags_dates_and_expiry(self):
        store=MemoryStore(self.path);comment,_=self.source(store);key=self.approved(store,comment)
        self.assertEqual(len(store.search_memory(comment.viewer,'coffee OR "broken"',tags=['coffee'])),1)
        self.assertEqual(store.search_memory(comment.viewer,'coffee',tags=['music']),[])
        self.assertEqual(store.search_memory(comment.viewer,'coffee',since=time.time()+1),[])
        with store.connect() as db:db.execute('UPDATE memory_items SET summary=?,tags=? WHERE memory_id=?',('Viewer said they like piano.','["music"]',key))
        self.assertEqual(store.search_memory(comment.viewer,'coffee'),[])
        self.assertEqual(len(store.search_memory(comment.viewer,'piano',tags=['music'])),1)
        self.assertEqual(store.search_memory(comment.viewer,'piano',now=time.time()+31*86400),[])
        store.delete_memory(key)
        self.assertEqual(store.search_memory(comment.viewer,'piano'),[])
        with store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM memory_fts WHERE memory_fts MATCH 'piano'").fetchone()[0],0)
            db.execute("INSERT INTO memory_fts(memory_fts,rank) VALUES('integrity-check',1)")

    def test_source_deletion_removes_memory_history_and_late_audio_content(self):
        store=MemoryStore(self.path);comment,inbox=self.source(store);key=self.approved(store,comment)
        receipt=inbox.take()['receipt'];inbox.bind(receipt,'turn');store.start('turn','offline:'+comment.viewer,comment.text)
        audio=self.root/'audio.wav';audio.write_bytes(b'local-owned-audio')
        store.record_utterance('turn',0,'A reply.',audio,1)
        with store.connect() as db:store.remove_source(db,comment.receipt,time.time())
        store.finish('turn','late reply','completed',{});store.record_utterance('turn',1,'late audio',audio,1)
        self.assertEqual(store.search_memory(comment.viewer,'coffee'),[])
        with store.connect() as db:
            self.assertEqual(db.execute('SELECT text,viewer_id,is_deleted FROM source_events').fetchone(),('',None,1))
            self.assertEqual(db.execute('SELECT comment,reply,status FROM conversation_turns').fetchone(),('','','source_removed'))
            self.assertEqual(db.execute('SELECT text,audio_path,status FROM utterances').fetchall(),[('',None,'source_removed')])
            self.assertEqual(db.execute('SELECT summary,tags,review_state FROM memory_items WHERE memory_id=?',(key,)).fetchone(),('','[]','deleted'))

    def test_retention_purges_raw_content_and_keeps_reviewed_source_provenance(self):
        store=MemoryStore(self.path);comment,_=self.source(store);self.approved(store,comment)
        future=time.time()+90000;store.expire(now=future)
        result=store.search_memory(comment.viewer,'coffee',now=future)
        self.assertEqual(len(result),1);self.assertFalse(result[0]['sources'][0]['raw_available'])
        with store.connect() as db:self.assertEqual(db.execute('SELECT text,viewer_id FROM source_events').fetchone(),('',None))
        store.expire(now=time.time()+31*86400)
        with store.connect() as db:self.assertEqual(db.execute('SELECT summary,deletion_reason FROM memory_items').fetchone(),('','expired'))

    def test_online_backup_restores_rows_sources_and_fts_and_refuses_overwrite(self):
        store=MemoryStore(self.path);comment,_=self.source(store);self.approved(store,comment)
        backup=self.root/'backup.sqlite3';store.backup(backup)
        self.assertEqual(backup.stat().st_mode & 0o777,0o600)
        with self.assertRaises(FileExistsError):store.backup(backup)
        restored=MemoryStore(backup);self.assertEqual(restored.status()['integrity_check'],['ok'])
        self.assertEqual(len(restored.search_memory(comment.viewer,'coffee')),1)

    def test_inspection_and_backup_are_read_only_and_do_not_migrate_legacy_schema(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('CREATE TABLE old_data(id TEXT PRIMARY KEY,text TEXT)')
            db.execute("INSERT INTO old_data VALUES('one','preserve me')")
        self.path.chmod(0o600)
        existing=Database.existing(self.path)
        self.assertEqual(existing.status()['user_version'],0)
        self.assertIsNone(existing.status()['tables']['source_events'])
        with self.assertRaises(sqlite3.OperationalError),existing.connect() as db:db.execute("DELETE FROM old_data")
        destination=self.root/'before-migration.sqlite3';existing.backup(destination)
        with closing(sqlite3.connect(self.path)) as source, closing(sqlite3.connect(destination)) as restored:
            self.assertEqual(source.execute('PRAGMA user_version').fetchone()[0],0)
            self.assertEqual(restored.execute('PRAGMA user_version').fetchone()[0],0)
            self.assertEqual(restored.execute('SELECT * FROM old_data').fetchall(),[('one','preserve me')])
        self.assertEqual(self.path.stat().st_mode & 0o777,0o600)

    def test_runtime_writes_utterances_actions_and_usage_without_inventing_measured_times(self):
        store=MemoryStore(self.path);events=[]
        SpeechController(ProviderSettings(),store,self.root,providers=(Text(),Reaction(),Speech())).run('Hi','v','turn',events.append,threading.Event())
        store.chunk_claimed('turn',0);store.playback_scheduled('turn',0,3.25);store.played('turn')
        with store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM utterances').fetchone()[0],2)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM api_usage WHERE mode=? AND attempted=0',('offline',)).fetchone()[0],4)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM api_usage WHERE input_tokens IS NOT NULL OR cost_estimate IS NOT NULL').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT started_at,ended_at,playback_ack_at IS NOT NULL FROM utterances WHERE chunk_index=0').fetchone(),(None,None,1))
            self.assertEqual(db.execute("SELECT rendered_at,playout_at,planned_playout_at,clock_domain FROM action_events WHERE action='speech_chunk_scheduled'").fetchone(),(None,None,3.25,'browser_audio_context_seconds'))

    def test_gemini_usage_is_provider_reported_and_retrieved_claims_are_marked_untrusted(self):
        stream=b'data: {"modelVersion":"gemini-test","usageMetadata":{"promptTokenCount":40,"candidatesTokenCount":10,"thoughtsTokenCount":3,"totalTokenCount":53},"candidates":[{"content":{"parts":[{"text":"Hi."}]},"finishReason":"STOP"}]}\n\n'
        http=HTTP(stream=stream);adapter=GeminiText(ProviderSettings(),http)
        adapter.set_memories([{'claim_kind':'viewer_report','summary':'Viewer said they enjoy coffee.'}])
        self.assertEqual(list(adapter.stream('Hello',[])),['Hi.'])
        self.assertEqual(adapter.usage['input_tokens'],40);self.assertEqual(adapter.usage['total_tokens'],53)
        payload=http.calls[0][2];self.assertIn('untrusted quotations',payload['systemInstruction']['parts'][0]['text'])
        self.assertIn('viewer_report',payload['contents'][-1]['parts'][0]['text'])
        store=MemoryStore(self.path);store.start('t','v','hi')
        key=store.prepare_call('t','gemini','generate',{},model='configured',mode='cloud');store.call_usage(key,adapter.usage)
        with store.connect() as db:self.assertEqual(db.execute('SELECT model,response_model,input_tokens,total_tokens,cost_estimate FROM api_usage').fetchone(),('configured','gemini-test',40,53,None))


if __name__=='__main__':unittest.main()
