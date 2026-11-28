"""Source-backed context ordered developer > comment exchanges > spoken answers."""
import hashlib
import json
import re
import time
import uuid

from ravora.memory import safe_summary
from ravora.relationships import VIEWER_PLATFORMS
from ravora.viewer_recognition import name_command
from ravora.context_prompt import context_text
from ravora.memory_extraction import dialogue_terms, explicit_memory_request, historical_reference, session_recap
from ravora.developer_identity import DEVELOPER_PLATFORMS, developer_keys

KINDS={1:'developer_dialogue',2:'comment_exchange',3:'assistant_answer'}


def sensitive(text):
    if re.search(r"(?i)\b(?:off the record|forget this|do not remember|don't remember|do not store|don't store)\b",text):return True
    try:
        for offset in range(0,len(text),600):safe_summary(text[offset:offset+800])
        return False
    except ValueError:return True


def fingerprint(viewer,scope,summary,sources):
    return hashlib.sha256(json.dumps([viewer,scope,summary,sorted(sources)],ensure_ascii=False).encode()).hexdigest()


class ConversationContext:
    def share_turn_context(self,turn_id,summary,*,developer_id,reviewed_non_sensitive=False,ttl_days=30):
        """Explicit human summary; never automatically publish a private transcript."""
        if reviewed_non_sensitive is not True:raise ValueError('CONTEXT_SHARING_REQUIRES_REVIEW')
        if type(ttl_days) is not int or not 1<=ttl_days<=90:raise ValueError('INVALID_MEMORY_TTL')
        summary=safe_summary(summary);now=time.time();owners=developer_keys(developer_id)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('''SELECT s.id,s.viewer_key,t.viewer,s.platform FROM conversation_turns t
                JOIN source_events s ON s.id=t.source_event_id WHERE t.turn_id=? AND t.status='completed'
                AND t.played=1 AND s.is_deleted=0 AND s.raw_purged_at IS NULL AND s.expires_at>?''',(turn_id,now)).fetchone()
            if not row or row[1] not in owners or row[3] not in DEVELOPER_PLATFORMS:raise ValueError('SHARE_REQUIRES_PLAYED_DEVELOPER_TURN')
            scope=row[2].split(':',1)[0]
            if scope not in ('offline','cloud') or row[2]!=scope+':'+row[1]:raise ValueError('INVALID_MEMORY_SCOPE')
            key=uuid.uuid4().hex;share=uuid.uuid4().hex
            db.execute('''INSERT INTO memory_items(memory_id,viewer_key,scope,summary,tags,confidence,
                review_state,sensitivity,created_at,expires_at) VALUES(?,?,?,?,?,1,'approved','non_sensitive',?,?)''',
                (key,row[1],scope,summary,'["developer-shared"]',now,now+ttl_days*86400))
            db.execute('INSERT INTO memory_sources(memory_id,event_id) VALUES(?,?)',(key,row[0]))
            db.execute('INSERT INTO context_shares VALUES(?,?,?,?,?,?,NULL)',
                (share,key,scope,row[1],fingerprint(row[1],scope,summary,[row[0]]),now))
        return {'memory_id':key,'share_id':share}

    def share_context_memory(self,memory_id,*,developer_id,reviewed_non_sensitive=False):
        if reviewed_non_sensitive is not True:raise ValueError('CONTEXT_SHARING_REQUIRES_REVIEW')
        owners=developer_keys(developer_id);now=time.time()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute("SELECT viewer_key,scope,summary FROM memory_items WHERE memory_id=? AND review_state='approved' AND sensitivity='non_sensitive' AND deleted_at IS NULL AND expires_at>?",(memory_id,now)).fetchone()
            if not row or row[0] not in owners:raise ValueError('SHARE_REQUIRES_APPROVED_DEVELOPER_MEMORY')
            safe_summary(row[2])
            sources=db.execute('SELECT s.id,s.viewer_key,s.platform,s.is_deleted FROM source_events s JOIN memory_sources x ON x.event_id=s.id WHERE x.memory_id=?',(memory_id,)).fetchall()
            if not sources or any(s[1]!=row[0] or s[2] not in DEVELOPER_PLATFORMS or s[3] for s in sources):raise ValueError('CONTEXT_SOURCE_UNAVAILABLE')
            digest=fingerprint(*row,[s[0] for s in sources])
            old=db.execute('SELECT id,content_sha256 FROM context_shares WHERE memory_id=? AND revoked_at IS NULL',(memory_id,)).fetchone()
            if old and old[1]==digest:return old[0]
            if old:db.execute('UPDATE context_shares SET revoked_at=? WHERE id=?',(now,old[0]))
            key=uuid.uuid4().hex
            db.execute('INSERT INTO context_shares VALUES(?,?,?,?,?,?,NULL)',(key,memory_id,row[1],row[0],digest,now))
        return key

    def revoke_context_share(self,memory_id):
        with self.connect() as db:
            return db.execute('UPDATE context_shares SET revoked_at=? WHERE memory_id=? AND revoked_at IS NULL',(time.time(),memory_id)).rowcount

    def _shared_context(self,owners,scope,query):
        with self.connect() as db:
            rows=db.execute("""SELECT r.id,r.memory_id,r.owner_key,r.content_sha256,m.summary
                FROM context_shares r JOIN memory_items m ON m.memory_id=r.memory_id
                WHERE r.scope=? AND m.scope=r.scope AND m.viewer_key=r.owner_key AND r.revoked_at IS NULL
                  AND r.owner_key IN (?,?,?) AND m.review_state='approved' AND m.sensitivity='non_sensitive'
                  AND m.deleted_at IS NULL AND m.expires_at>? ORDER BY r.approved_at DESC LIMIT 32""",(scope,*owners,time.time())).fetchall()
            found=[];words=set(dialogue_terms(query))
            for share,key,viewer,digest,summary in rows:
                sources=db.execute('SELECT s.id,s.viewer_key,s.platform,s.is_deleted FROM source_events s JOIN memory_sources x ON x.event_id=s.id WHERE x.memory_id=?',(key,)).fetchall()
                if not sources or any(s[1]!=viewer or s[2] not in DEVELOPER_PLATFORMS or s[3] for s in sources):continue
                if sensitive(summary) or digest!=fingerprint(viewer,scope,summary,[s[0] for s in sources]):continue
                entry={'context_id':'memory:'+key,'tier':1,'kind':'shared_developer_memory','text':summary,
                    'memory_id':key,'share_id':share,'source_event_id':sources[0][0]}
                overlap = words.intersection(dialogue_terms(summary))
                if overlap:found.append((len(overlap),entry))
        return [e for _,e in sorted(found,key=lambda pair:pair[0],reverse=True)]

    def recall_context(self,viewer,query,*,scope,developer_id,limit=8,max_chars=3600,turn_id=None):
        if scope not in ('offline','cloud') or type(limit) is not int or not 3<=limit<=12 or type(max_chars) is not int or not 1200<=max_chars<=8000:raise ValueError('INVALID_CONTEXT_LIMITS')
        owners=developer_keys(developer_id);private=viewer in owners
        words=dialogue_terms(query[:1000])[:16]
        if not words or session_recap(query):return []
        match=' OR '.join('"'+word+'"' for word in words)
        # unicode61 indexes an unspaced Japanese phrase as one token. For an
        # explicit recall, also check topical nouns in the owner's recent eligible turns.
        jp_subjects = set(re.findall(r'[ァ-ヺー]{2,}|[一-龯々]{2,}', query)) if explicit_memory_request(query) else set()
        jp_subjects.difference_update(('前回','以前','先週','昨日','何度','前日','会話','質問','名前','話題'))
        groups={1:self._shared_context(owners,scope,query),2:[],3:[]}
        with self.connect() as db:
            stream = None
            if turn_id and not historical_reference(query):
                # Once a conversation is underway, search its sources first. An explicit
                # request for past material may cross sessions under the normal owner rules.
                stream = db.execute('''SELECT s.stream_id FROM conversation_turns t
                    JOIN source_events s ON s.id=t.source_event_id
                    WHERE t.turn_id=? AND t.viewer=? AND EXISTS(
                        SELECT 1 FROM source_events p JOIN conversation_turns prior ON prior.source_event_id=p.id
                        WHERE p.viewer_key=s.viewer_key AND p.stream_id=s.stream_id AND prior.viewer=t.viewer
                          AND prior.played=1 AND prior.status='completed' AND p.is_deleted=0
                          AND p.raw_purged_at IS NULL AND p.expires_at>?)''',
                    (turn_id,scope+':'+viewer,time.time())).fetchone()
            for tier in (1,2,3):
                if tier==1 and not private:continue
                where='v.scope=? AND v.tier=?';params=[scope,tier]
                if turn_id and not private and viewer.partition(':')[0] in VIEWER_PLATFORMS:
                    where += """ AND EXISTS(SELECT 1 FROM source_events prior JOIN conversation_turns active ON active.turn_id=?
                        JOIN source_events current ON current.id=active.source_event_id WHERE prior.id=v.source_event_id
                        AND active.viewer=? AND (prior.received_at,prior.rowid)<(current.received_at,current.rowid)
                        AND NOT EXISTS(SELECT 1 FROM relationship_events r WHERE r.source_event_id=prior.id
                                       AND r.played_at>current.received_at))"""
                    params.extend((turn_id,scope+':'+viewer))
                if stream:
                    where+=' AND EXISTS(SELECT 1 FROM source_events s WHERE s.id=v.source_event_id AND s.stream_id=?)'
                    params.append(stream[0])
                if tier==1:
                    where+=''' AND v.viewer_key IN (?,?,?) AND EXISTS(SELECT 1 FROM developer_memory_sources d
                        WHERE d.event_id=v.source_event_id AND d.owner_key=v.viewer_key)'''
                    params.extend(owners)
                elif not private:where+=' AND v.viewer_key=?';params.append(viewer)
                elif tier==3:
                    where+=' AND (v.platform NOT IN (?,?,?) OR v.viewer_key IN (?,?,?))';params.extend((*DEVELOPER_PLATFORMS,*owners))
                # Even answer-only retrieval observes privacy/opt-out markers in
                # the original input; omitting that check would revive a refused turn.
                base='SELECT v.turn_id,v.source_event_id,t.comment,v.answer FROM conversation_context v JOIN conversation_turns t ON t.turn_id=v.turn_id'
                rows=db.execute(base+' JOIN conversation_fts ON conversation_fts.rowid=v.source_rowid WHERE '+where+' AND conversation_fts MATCH ? ORDER BY bm25(conversation_fts),v.closed_at DESC LIMIT 24',(*params,match)).fetchall()
                if jp_subjects and tier != 3:
                    recent = db.execute(base+' WHERE '+where+' AND v.viewer_key=? ORDER BY v.closed_at DESC LIMIT 24',
                                        (*params,viewer)).fetchall()
                    rows.extend(r for r in recent if any(term in r[2] for term in jp_subjects))
                for turn,event,question,answer in dict.fromkeys(rows):
                    # A character's own guess cannot make a viewer report relevant.
                    evidence = answer if tier == 3 else question
                    if not (any(word in words for word in dialogue_terms(evidence))
                            or any(term in evidence for term in jp_subjects)):continue
                    if sensitive(question) or sensitive(answer) or (not private and name_command(question)[0]):continue
                    text=('Developer: ' if tier==1 else 'Viewer: ')+question[:300]+'\nAssistant: '+answer[:600] if tier!=3 else answer[:800]
                    groups[tier].append({'context_id':f'turn:{tier}:{turn}','tier':tier,'kind':KINDS[tier],
                        'text':text,'source_turn_id':turn,'source_event_id':event})
        # Reserve room for every tier; higher tiers get more space. Bound the
        # actual serialized provider payload, including provenance and labels.
        limits={1:(limit-1)//2,2:max(1,(limit-(limit-1)//2)//2)};limits[3]=limit-limits[1]-limits[2]
        budgets={1:max_chars//2,2:max_chars*3//10,3:max_chars-max_chars//2-max_chars*3//10}
        selected=[];turns=set()
        for tier in (1,2,3):
            used=0;count=0
            for original in groups[tier]:
                if count>=limits[tier]:break
                if tier==3 and original.get('source_turn_id') in turns:continue
                entry=dict(original);entry['text']=''
                overhead=len(json.dumps(entry,ensure_ascii=False,separators=(',',':')))+1
                room=min(800,budgets[tier]-used-overhead)
                if room<40:break
                entry['text']=original['text'][:room]
                if len(context_text(selected+[entry]))>max_chars:break
                selected.append(entry);count+=1;used+=len(json.dumps(entry,ensure_ascii=False,separators=(',',':')))+1
                if entry.get('source_turn_id'):turns.add(entry['source_turn_id'])
        return selected

    def record_context_retrievals(self,turn,entries,*,call_id,prepared):
        with self.connect() as db:
            db.executemany('''INSERT INTO context_retrievals
                (turn_id,context_id,tier,source_turn_id,source_event_id,memory_id,share_id,position,context_chars,call_id,context_prepared,selected_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(turn_id,context_id) DO UPDATE SET
                call_id=COALESCE(excluded.call_id,call_id),context_prepared=MAX(context_prepared,excluded.context_prepared),
                position=excluded.position,context_chars=excluded.context_chars''',
                [(turn,e['context_id'],e['tier'],e.get('source_turn_id'),e['source_event_id'],e.get('memory_id'),e.get('share_id'),
                    i,len(e['text']),call_id,int(prepared),time.time()) for i,e in enumerate(entries)])
