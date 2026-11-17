"""Source-backed, reviewed memory. Viewer reports are never promoted to facts."""
import hashlib
import json
import math
from pathlib import Path
import re
import time
import uuid

from ravora.database import Database
from ravora.memory_extraction import POLICY, Extraction, LocalPreferenceExtractor, retrieval_query
from ravora.gemini_memory import GEMINI_POLICY,validate_extraction


def safe_summary(text):
    # A denylist is an aid to review, not proof that text is non-sensitive.
    if not isinstance(text,str) or not 1<=len(text.strip())<=800:raise ValueError('INVALID_MEMORY_SUMMARY')
    if re.search(r'(?i)(password|passwd|api[_ -]?key|secret|bearer|private[_ -]?key|-----BEGIN)|[\w.+-]+@[\w.-]+\.[a-z]{2,}|(?:\+?\d[\d ()-]{7,}\d)',text):
        raise ValueError('SENSITIVE_MEMORY_REJECTED')
    return text.strip()


class MemoryRepository(Database):
    @staticmethod
    def record_source(db,comment,now):
        db.execute('''INSERT OR IGNORE INTO source_events
            (id,platform,external_event_id,stream_id,viewer_id,viewer_key,text,published_at,received_at,expires_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)''',
            (comment.receipt,comment.source,comment.message_id,comment.stream_id,
             comment.author_id,comment.viewer,comment.text,comment.published_at,now,now+86400))

    @staticmethod
    def remove_source(db,receipt,now):
        db.execute('UPDATE source_events SET is_deleted=1,deleted_at=? WHERE id=? AND is_deleted=0',(now,receipt))

    @staticmethod
    def purge_expired(db,now):
        db.execute("""UPDATE memory_jobs SET status='cancelled',reason='SOURCE_EXPIRED',completed_at=?,
            claim_token=NULL,lease_until=NULL WHERE status IN ('pending','running') AND source_event_id IN
            (SELECT id FROM source_events WHERE expires_at<=? OR raw_purged_at IS NOT NULL OR is_deleted=1)""",(now,now))
        db.execute("""UPDATE memory_items SET summary='',tags='[]',review_state='deleted',
            deleted_at=?,deletion_reason='expired' WHERE deleted_at IS NULL AND expires_at<=?""",(now,now))
        db.execute("""UPDATE conversation_turns SET comment='',reply='',played=0,status='source_expired',
            state='CANCELLED',reaction=NULL,closed_at=COALESCE(closed_at,?)
            WHERE source_event_id IN (SELECT id FROM source_events WHERE expires_at<=? AND raw_purged_at IS NULL)
              AND status!='source_removed'""",(now,now))
        db.execute("""UPDATE utterances SET text='',audio_path=NULL,audio_sha256=NULL,status='source_expired'
            WHERE turn_id IN (SELECT turn_id FROM conversation_turns WHERE status='source_expired')""")
        db.execute("UPDATE source_events SET text='',viewer_id=NULL,raw_purged_at=? WHERE expires_at<=? AND raw_purged_at IS NULL",(now,now))

    def expire(self,*,now=None):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE');self.purge_expired(db,time.time() if now is None else now)

    def candidate_memory(self,viewer_key,summary,source_ids,*,scope='offline',tags=(),confidence=.5,ttl_seconds=30*86400):
        summary=safe_summary(summary);source_ids=list(dict.fromkeys(source_ids));tags=list(tags)
        if scope not in ('offline','cloud') or not 1<=len(source_ids)<=10:raise ValueError('INVALID_MEMORY_SCOPE_OR_SOURCES')
        if len(tags)>8 or any(not isinstance(x,str) or not re.fullmatch(r'[\w -]{1,40}',x) for x in tags):raise ValueError('INVALID_MEMORY_TAGS')
        for tag in tags:safe_summary(tag)
        if isinstance(confidence,bool) or not isinstance(confidence,(int,float)) or not math.isfinite(confidence) or not 0<=confidence<=1:raise ValueError('INVALID_MEMORY_CONFIDENCE')
        if not isinstance(ttl_seconds,(int,float)) or not math.isfinite(ttl_seconds) or not 0<ttl_seconds<=90*86400:raise ValueError('INVALID_MEMORY_TTL')
        key=uuid.uuid4().hex;now=time.time()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            for event in source_ids:
                row=db.execute('SELECT viewer_key,is_deleted,raw_purged_at,expires_at FROM source_events WHERE id=?',(event,)).fetchone()
                if not row or row[0]!=viewer_key or row[1] or row[2] is not None or row[3]<=now:raise ValueError('MEMORY_SOURCE_UNAVAILABLE')
            db.execute('''INSERT INTO memory_items
                (memory_id,viewer_key,scope,summary,tags,confidence,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?)''',
                (key,viewer_key,scope,summary,json.dumps(tags),confidence,now,now+ttl_seconds))
            db.executemany('INSERT INTO memory_sources(memory_id,event_id) VALUES(?,?)',[(key,e) for e in source_ids])
        return key

    def approve_memory(self,key,*,reviewed_non_sensitive=False):
        if reviewed_non_sensitive is not True:raise ValueError('MEMORY_REQUIRES_SENSITIVITY_REVIEW')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute("SELECT summary FROM memory_items WHERE memory_id=? AND review_state='candidate' AND deleted_at IS NULL AND expires_at>?",(key,time.time())).fetchone()
            if not row:raise ValueError('MEMORY_NOT_REVIEWABLE')
            safe_summary(row[0])
            if not db.execute('SELECT 1 FROM memory_sources WHERE memory_id=?',(key,)).fetchone():raise ValueError('MEMORY_HAS_NO_SOURCE')
            if db.execute('SELECT 1 FROM memory_sources m JOIN source_events s ON s.id=m.event_id WHERE m.memory_id=? AND (s.is_deleted=1 OR s.raw_purged_at IS NOT NULL OR s.expires_at<=?)',(key,time.time())).fetchone():raise ValueError('MEMORY_SOURCE_UNAVAILABLE')
            db.execute("UPDATE memory_items SET review_state='approved',sensitivity='non_sensitive' WHERE memory_id=?",(key,))

    def delete_memory(self,key,*,reason='user_request'):
        if reason not in ('user_request','expired','source_removed'):raise ValueError('INVALID_DELETION_REASON')
        with self.connect() as db:
            db.execute("UPDATE memory_items SET summary='',tags='[]',review_state='deleted',deleted_at=?,deletion_reason=? WHERE memory_id=? AND deleted_at IS NULL",(time.time(),reason,key))

    def search_memory(self,viewer_key,query='',*,scope='offline',tags=(),since=None,limit=6,now=None):
        if scope not in ('offline','cloud') or not 3<=limit<=10:raise ValueError('INVALID_MEMORY_SEARCH')
        now=time.time() if now is None else now
        # Quote tokens, so viewer text cannot inject FTS operators or SQL.
        words=re.findall(r'[^\W_]+',query[:1000],re.UNICODE)[:16]
        match=' OR '.join('"'+word+'"' for word in words)
        join=' JOIN memory_fts ON memory_fts.rowid=m.rowid' if match else ''
        params=[viewer_key,scope,now]
        where="""m.viewer_key=? AND m.scope=? AND m.expires_at>? AND m.review_state='approved'
            AND m.sensitivity='non_sensitive' AND m.deleted_at IS NULL
            AND EXISTS(SELECT 1 FROM memory_sources x WHERE x.memory_id=m.memory_id)
            AND NOT EXISTS(SELECT 1 FROM memory_sources x JOIN source_events s ON s.id=x.event_id
                WHERE x.memory_id=m.memory_id AND (s.is_deleted=1 OR s.viewer_key!=m.viewer_key))"""
        if match:where+=' AND memory_fts MATCH ?';params.append(match)
        if since is not None:where+=' AND m.created_at>=?';params.append(since)
        for tag in list(tags)[:8]:
            where+=' AND EXISTS(SELECT 1 FROM json_each(m.tags) WHERE value=?)';params.append(tag)
        rank='bm25(memory_fts),m.confidence DESC,m.created_at DESC' if match else 'm.confidence DESC,m.created_at DESC'
        with self.connect() as db:
            rows=db.execute(f'SELECT m.memory_id,m.summary,m.tags,m.confidence,m.created_at,m.expires_at FROM memory_items m{join} WHERE {where} ORDER BY {rank} LIMIT ?',(*params,limit)).fetchall()
            result=[]
            for key,summary,tag_json,confidence,created,expires in rows:
                sources=[{'event_id':e,'platform':p,'received_at':t,'raw_available':purged is None and exp>now}
                         for e,p,t,purged,exp in db.execute('''SELECT s.id,s.platform,s.received_at,s.raw_purged_at,s.expires_at
                         FROM source_events s JOIN memory_sources x ON x.event_id=s.id WHERE x.memory_id=?''',(key,))]
                result.append({'memory_id':key,'summary':summary,'tags':json.loads(tag_json),'confidence':confidence,
                    'claim_kind':'viewer_report','created_at':created,'expires_at':expires,'sources':sources})
        return result

    def recall_memory(self,viewer_key,comment,*,scope):
        query,wants_recall=retrieval_query(comment)
        found=self.search_memory(viewer_key,query,scope=scope) if query else []
        strategy='topic_fts'
        if not found:
            found=self.search_memory(viewer_key,'',scope=scope,limit=6 if wants_recall else 3)
            strategy='viewer_recent' if wants_recall else 'viewer_context'
        return found,strategy

    def record_retrievals(self,turn_id,memories,*,context_prepared,strategy,call_id=None):
        with self.connect() as db:
            db.executemany('''INSERT INTO memory_retrievals(turn_id,memory_id,selected_at,context_prepared,strategy,call_id)
                VALUES(?,?,?,?,?,?) ON CONFLICT(turn_id,memory_id) DO UPDATE SET
                context_prepared=MAX(context_prepared,excluded.context_prepared),call_id=COALESCE(excluded.call_id,call_id)''',
                [(turn_id,m['memory_id'],time.time(),int(context_prepared),strategy,call_id) for m in memories])

    @staticmethod
    def enqueue_memory_job(db,turn_id,*,scope,ttl_seconds,capacity,policy=POLICY,model=None,min_confidence=.7):
        if policy==GEMINI_POLICY and (not isinstance(model,str) or not re.fullmatch(r'[a-z0-9][a-z0-9._-]{0,100}',model)):
            raise ValueError('MEMORY_MODEL_REQUIRED')
        now=time.time()
        count=db.execute("SELECT COUNT(*) FROM memory_jobs WHERE scope=? AND status IN ('pending','running')",(scope,)).fetchone()[0]
        status,reason=('skipped','QUEUE_FULL') if count>=capacity else ('pending',None)
        db.execute('''INSERT OR IGNORE INTO memory_jobs
            (turn_id,source_event_id,viewer_key,scope,policy,ttl_seconds,status,enqueued_at,completed_at,reason,model,min_confidence)
            SELECT t.turn_id,s.id,s.viewer_key,?,?,?,?,?,?,?,?,? FROM conversation_turns t
              JOIN source_events s ON s.id=t.source_event_id
             WHERE t.turn_id=? AND t.played=1 AND t.status='completed' AND t.viewer=?||':'||s.viewer_key
               AND s.is_deleted=0 AND s.raw_purged_at IS NULL AND s.expires_at>?''',
            (scope,policy,ttl_seconds,status,now,now if status=='skipped' else None,reason,model,min_confidence,turn_id,scope,now))

    def memory_pending(self,viewer_key,scope,*,policies=None):
        with self.connect() as db:
            clause=' AND policy IN ('+','.join('?' for _ in policies)+')' if policies else ''
            return bool(db.execute("SELECT 1 FROM memory_jobs WHERE viewer_key=? AND scope=? AND status IN ('pending','running')"+clause+' LIMIT 1',
                (viewer_key,scope,*(policies or ()))).fetchone())

    def claim_memory_job(self,scope,*,policy=POLICY,lease_seconds=15):
        now=time.time();token=uuid.uuid4().hex
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            # A durable dispatch may have reached the provider. Never put it back
            # on the queue after a crash or expired lease.
            expired=(scope,now)
            db.execute("""UPDATE provider_calls SET state='outcome_unknown',code='MEMORY_LEASE_EXPIRED',updated=?
                WHERE id IN (SELECT api_call_id FROM memory_jobs WHERE scope=? AND status='running'
                    AND lease_until<=? AND request_attempted=1)""",(now,*expired))
            db.execute("""UPDATE api_usage SET status='outcome_unknown' WHERE id IN
                (SELECT api_call_id FROM memory_jobs WHERE scope=? AND status='running'
                    AND lease_until<=? AND request_attempted=1)""",expired)
            db.execute("""UPDATE memory_jobs SET status='failed',reason='OUTCOME_UNKNOWN',request_state='outcome_unknown',
                completed_at=?,claim_token=NULL,lease_until=NULL
                WHERE scope=? AND status='running' AND lease_until<=? AND request_attempted=1""",(now,*expired))
            db.execute("""UPDATE memory_jobs SET status=CASE WHEN attempts<3 THEN 'pending' ELSE 'failed' END,
                reason='LEASE_EXPIRED',completed_at=CASE WHEN attempts>=3 THEN ? ELSE NULL END,
                claim_token=NULL,lease_until=NULL WHERE scope=? AND status='running' AND lease_until<=?""",(now,scope,now))
            db.execute("""UPDATE memory_jobs SET status='cancelled',reason='SOURCE_UNAVAILABLE',completed_at=?
                WHERE scope=? AND status='pending' AND NOT EXISTS
                (SELECT 1 FROM source_events s JOIN conversation_turns t ON t.source_event_id=s.id
                 WHERE s.id=memory_jobs.source_event_id AND t.turn_id=memory_jobs.turn_id
                   AND t.played=1 AND t.status='completed' AND s.is_deleted=0
                   AND t.viewer=memory_jobs.scope||':'||s.viewer_key
                   AND s.raw_purged_at IS NULL AND s.expires_at>?)""",(now,scope,now))
            row=db.execute("""SELECT j.turn_id,j.source_event_id,j.viewer_key,j.scope,j.policy,j.ttl_seconds,s.text,
                    t.reply,j.model,j.min_confidence
                FROM memory_jobs j JOIN source_events s ON s.id=j.source_event_id
                JOIN conversation_turns t ON t.turn_id=j.turn_id
                WHERE j.scope=? AND j.policy=? AND j.status='pending' AND j.request_attempted=0
                ORDER BY j.enqueued_at,j.rowid LIMIT 1""",(scope,policy)).fetchone()
            if not row:return None
            db.execute("""UPDATE memory_jobs SET status='running',claim_token=?,lease_until=?,claimed_at=?,
                attempts=attempts+1 WHERE turn_id=? AND status='pending'""",(token,now+lease_seconds,now,row[0]))
        return dict(zip(('turn_id','source_event_id','viewer_key','scope','policy','ttl_seconds','text','reply','model','min_confidence'),row),claim_token=token)

    def complete_memory_job(self,job,result):
        if job['policy']==POLICY:
            if not isinstance(result,Extraction) or result!=LocalPreferenceExtractor().extract(job['text']):raise ValueError('INVALID_EXTRACTION_RESULT')
            items=result.preferences
        elif job['policy']==GEMINI_POLICY:
            result=validate_extraction(result,job['text'],job['min_confidence']);items=result.candidates
        else:raise ValueError('INVALID_EXTRACTION_POLICY')
        now=time.time()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute("""SELECT s.text,s.viewer_key,j.ttl_seconds FROM memory_jobs j
                JOIN source_events s ON s.id=j.source_event_id JOIN conversation_turns t ON t.turn_id=j.turn_id
                WHERE j.turn_id=? AND j.status='running' AND j.claim_token=? AND j.lease_until>?
                  AND s.is_deleted=0 AND s.raw_purged_at IS NULL AND s.expires_at>?
                  AND t.played=1 AND t.status='completed' AND t.viewer=j.scope||':'||s.viewer_key""",
                (job['turn_id'],job['claim_token'],now,now)).fetchone()
            if not row:
                db.execute("""UPDATE memory_jobs SET status='cancelled',reason='SOURCE_UNAVAILABLE',completed_at=?,
                    claim_token=NULL,lease_until=NULL WHERE turn_id=? AND status='running' AND claim_token=?""",
                    (now,job['turn_id'],job['claim_token']))
                return False
            if row[0]!=job['text'] or row[1]!=job['viewer_key']:raise ValueError('MEMORY_SOURCE_CHANGED')
            saved=0
            for preference in items:
                content=preference.content();key=uuid.uuid4().hex
                newer=db.execute('''SELECT 1 FROM memory_items m JOIN memory_jobs newer ON newer.turn_id=m.extraction_turn_id
                    JOIN memory_jobs current ON current.turn_id=? WHERE m.viewer_key=? AND m.scope=?
                    AND m.topic_key=? AND m.deleted_at IS NULL
                    AND (newer.enqueued_at>current.enqueued_at OR
                         (newer.enqueued_at=current.enqueued_at AND newer.rowid>current.rowid)) LIMIT 1''',
                    (job['turn_id'],job['viewer_key'],job['scope'],preference.topic)).fetchone()
                if newer:continue
                # A newer explicit statement supersedes a prior automated preference;
                # it does not turn either statement into an independently verified fact.
                db.execute("""UPDATE memory_items SET summary='',tags='[]',review_state='deleted',
                    deleted_at=?,deletion_reason='superseded' WHERE viewer_key=? AND scope=? AND topic_key=?
                    AND extraction_turn_id IS NOT NULL AND deleted_at IS NULL""",(now,job['viewer_key'],job['scope'],preference.topic))
                db.execute('''INSERT INTO memory_items(memory_id,viewer_key,scope,summary,tags,confidence,
                    review_state,sensitivity,created_at,expires_at,extraction_turn_id,topic_key,polarity,approval_method)
                    VALUES(?,?,?,?,?,?,'approved','non_sensitive',?,?,?,?,?,?)''',
                    (key,job['viewer_key'],job['scope'],content['summary'],json.dumps(content['tags']),content['confidence'],
                     now,now+row[2],job['turn_id'],preference.topic,getattr(preference,'polarity',None),job['policy']))
                if job['policy']==GEMINI_POLICY:
                    quote=preference.evidence_quote;offset=job['text'].index(quote)
                    db.execute('''INSERT INTO memory_sources(memory_id,event_id,quote_start,quote_end,quote_sha256)
                        VALUES(?,?,?,?,?)''',(key,job['source_event_id'],offset,offset+len(quote),hashlib.sha256(quote.encode()).hexdigest()))
                else:db.execute('INSERT INTO memory_sources(memory_id,event_id) VALUES(?,?)',(key,job['source_event_id']))
                saved+=1
            status='completed' if saved else 'skipped'
            reason=result.reason if saved or not items else 'SUPERSEDED_BY_NEWER'
            db.execute("""UPDATE memory_jobs SET status=?,completed_at=?,candidate_count=?,reason=?,claim_token=NULL,
                lease_until=NULL WHERE turn_id=? AND status='running' AND claim_token=?""",
                (status,now,saved,reason,job['turn_id'],job['claim_token']))
        return True

    def release_memory_job(self,job):
        with self.connect() as db:
            # Only work that was never sent can be resumed automatically.
            db.execute("""UPDATE memory_jobs SET status='pending',reason='WORKER_STOPPED',claim_token=NULL,
                lease_until=NULL WHERE turn_id=? AND status='running' AND claim_token=? AND request_attempted=0""",(job['turn_id'],job['claim_token']))

    def fail_memory_job(self,job,code,*,status='failed'):
        if status not in ('failed','skipped','cancelled'):raise ValueError('INVALID_JOB_TERMINAL_STATE')
        with self.connect() as db:
            db.execute("""UPDATE memory_jobs SET status=?,reason=?,completed_at=?,claim_token=NULL,lease_until=NULL
                WHERE turn_id=? AND status='running' AND claim_token=?""",(status,code,time.time(),job['turn_id'],job['claim_token']))

    def record_utterance(self,turn_id,index,text,path,duration):
        path=Path(path)
        with self.connect() as db:
            db.execute('''INSERT INTO utterances(utterance_id,turn_id,chunk_index,text,audio_path,audio_sha256,
                duration_seconds,status,created_at)
                SELECT ?,turn_id,?,?,?,?,?,'generated',? FROM conversation_turns
                WHERE turn_id=? AND status NOT IN ('source_removed','source_expired','cancelled')''',
                (turn_id+':'+str(index),index,text,str(path),hashlib.sha256(path.read_bytes()).hexdigest(),duration,time.time(),turn_id))

    def record_action(self,turn_id,action,*,details=None,planned_playout_at=None,clock_domain=None):
        now=time.time();key=uuid.uuid4().hex
        with self.connect() as db:
            db.execute('''INSERT INTO action_events(id,turn_id,action,requested_at,planned_playout_at,clock_domain,details)
                SELECT ?,turn_id,?,?,?,?,? FROM conversation_turns WHERE turn_id=?
                AND status NOT IN ('source_removed','source_expired','cancelled')''',
                (key,action,now,planned_playout_at,clock_domain,json.dumps(details or {}),turn_id))
        return key

    def record_reaction(self,turn_id,value):
        with self.connect() as db:
            db.execute("""UPDATE conversation_turns SET reaction=?,
                state=CASE WHEN status='running' THEN 'REACTING' ELSE state END
                WHERE turn_id=? AND (status='running' OR (status='completed' AND played=0))""",(json.dumps(value),turn_id))
        self.record_action(turn_id,'reaction_requested',details=value)

    def chunk_claimed(self,turn_id,index):
        with self.connect() as db:
            db.execute("UPDATE utterances SET status='claimed' WHERE turn_id=? AND chunk_index=? AND status='generated'",(turn_id,index))
            db.execute("UPDATE conversation_turns SET state='SPEAKING' WHERE turn_id=? AND status NOT IN ('source_removed','source_expired','cancelled')",(turn_id,))

    def playback_scheduled(self,turn_id,index,audio_context_time):
        if type(index) is not int or index<0 or not isinstance(audio_context_time,(float,int)) or not math.isfinite(audio_context_time) or audio_context_time<0:raise ValueError('INVALID_PLAYBACK_SCHEDULE')
        self.record_action(turn_id,'speech_chunk_scheduled',details={'chunk':index},
                           planned_playout_at=audio_context_time,clock_domain='browser_audio_context_seconds')
