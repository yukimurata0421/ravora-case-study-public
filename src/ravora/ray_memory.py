"""Small source-backed character memories. Called only by the existing worker."""
import datetime as dt
import hashlib
import json
import re
import time

from ravora.gemini_memory import source_block_reason
from ravora.ray_character import TOPICS, TRAITS, owner_hash, topics_in


class CharacterMemory:
    def __init__(self, store, scope):
        self.store = store
        self.scope = scope

    def load(self):
        with self.store.connect() as db:
            row = db.execute("SELECT traits,revision FROM character_profiles WHERE character_id='ray'").fetchone()
            traits = json.loads(row[0]) if row else list(TRAITS)
            if not isinstance(traits, list) or set(traits) != set(TRAITS):
                raise ValueError('CHARACTER_PROFILE_INVALID')
            rows = db.execute("""SELECT memory_id,owner_hash,kind,topic,strength,importance
                FROM character_memories WHERE scope=? AND status='active' AND expires_at>?
                ORDER BY importance DESC,updated_at DESC LIMIT 64""", (self.scope, time.time())).fetchall()
        return tuple(dict(zip(('id','owner_hash','kind','topic','strength','importance'), r))
                     for r in rows if r[3] in TOPICS)

    def promote(self, turn_id):
        now = time.time()
        day = dt.datetime.fromtimestamp(now, dt.timezone(dt.timedelta(hours=9))).strftime('%Y-%m-%d')
        promoted = 0
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("""SELECT s.id,s.viewer_key,s.text,t.reply,s.platform FROM conversation_turns t
                JOIN source_events s ON s.id=t.source_event_id
                WHERE t.turn_id=? AND t.played=1 AND t.status='completed' AND s.is_deleted=0
                    AND s.raw_purged_at IS NULL AND s.expires_at>? AND t.viewer=?||':'||s.viewer_key""",
                             (turn_id, now, self.scope)).fetchone()
            def eligible(text):
                return not source_block_reason(text) and not re.search(r'(?i)\b(private|off[ -]?stream|between us|keep (?:this|it) to yourself)\b|非公開|内緒|配信に出さない',text)
            if not row or row[4] == 'ray-initiative' or not eligible(row[2]): return 0
            event, viewer, text, reply, _ = row
            topics = topics_in(text)
            if not topics: return 0
            owner = owner_hash(self.scope, viewer)
            previous = db.execute("""SELECT t.comment FROM conversation_turns t JOIN source_events s ON s.id=t.source_event_id
                WHERE t.viewer=? AND t.played=1 AND t.status='completed' AND s.is_deleted=0
                    AND s.raw_purged_at IS NULL AND s.expires_at>? ORDER BY t.created_at DESC LIMIT 24""",
                                  (self.scope + ':' + viewer, now)).fetchall()
            for topic in topics[:2]:
                repeated = sum(topic in topics_in(p[0]) and eligible(p[0]) for p in previous) >= 3
                future = (re.search(r'(?i)\b(later|next time|another time)\b|今度|後で|次回', text)
                          and re.search(r'(?i)\b(later|next time|remember|continue)\b|今度|後で|続き', reply))
                preference = (re.search(r'(?i)\b(do you (?:like|prefer)|your (?:favorite|opinion))\b|どっちが好き|好み', text)
                              and re.search(r'(?i)\b(i (?:like|prefer|enjoy)|my favorite)\b|私は|好き', reply))
                if future:
                    kind, reason, memory_owner, importance = 'goal', 'shared_future_topic', owner, .85
                elif repeated:
                    kind, reason, memory_owner, importance = ('preference','expressed_preference','',.75) if preference else ('interest','repeated_topic','',.6)
                else: continue
                key = hashlib.sha256((self.scope+'\0'+memory_owner+'\0'+kind+'\0'+topic).encode()).hexdigest()
                old = db.execute('SELECT strength,status FROM character_memories WHERE memory_id=?', (key,)).fetchone()
                # An explicit deletion/reset must not be undone by delivery of an old event.
                if old and old[1] == 'deleted': continue
                db.execute("""INSERT OR IGNORE INTO character_memories
                    (memory_id,scope,owner_hash,kind,topic,strength,importance,created_at,updated_at,expires_at)
                    VALUES(?,?,?,?,?,0.5,?,?,?,?)""",
                           (key,self.scope,memory_owner,kind,topic,importance,now,now,now+30*86400))
                strength = old[0] if old else .5
                updated = min(1., strength + .02)
                added = db.execute("""INSERT OR IGNORE INTO character_experiences
                    (event_id,turn_id,memory_id,owner_hash,day_jst,reason,contribution,created_at) VALUES(?,?,?,?,?,?,?,?)""",
                                   (event,turn_id,key,owner,day,reason,updated-strength,now)).rowcount
                if not added: continue
                db.execute("UPDATE character_memories SET strength=?,updated_at=?,expires_at=?,status='active' WHERE memory_id=?",
                           (updated,now,now+30*86400,key))
                db.execute("""INSERT INTO character_preference_history
                    (memory_id,event_id,previous_strength,strength,reason,created_at) VALUES(?,?,?,?,?,?)""",
                           (key,event,strength,updated,reason,now))
                promoted += 1
        return promoted

    def referenced(self, memory_ids):
        with self.store.connect() as db:
            db.executemany("UPDATE character_memories SET reference_count=reference_count+1,last_referenced_at=? WHERE memory_id=? AND scope=? AND status='active'",
                           [(time.time(), key, self.scope) for key in set(memory_ids) if isinstance(key, str)])

    def delete(self, memory_id):
        with self.store.connect() as db:
            db.execute("UPDATE character_memories SET status='deleted' WHERE memory_id=? AND scope=?", (memory_id,self.scope))

    def reset(self):
        # Tombstones retain provenance and block replay-based resurrection.
        with self.store.connect() as db:
            db.execute("UPDATE character_memories SET status='deleted' WHERE scope=?", (self.scope,))

    def set_preference(self, topic, strength, *, kind='preference'):
        if topic not in TOPICS or kind not in ('preference','value','interest') or type(strength) not in (int,float) or not 0<=strength<=1:
            raise ValueError('CHARACTER_PREFERENCE_INVALID')
        now=time.time();key=hashlib.sha256((self.scope+'\0\0'+kind+'\0'+topic).encode()).hexdigest()
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old=db.execute('SELECT strength FROM character_memories WHERE memory_id=?',(key,)).fetchone()
            # A deliberate operator replacement supersedes earlier numeric contributions.
            db.execute('UPDATE character_experiences SET contribution=0 WHERE memory_id=?',(key,))
            db.execute("""INSERT INTO character_memories
                (memory_id,scope,owner_hash,kind,topic,strength,importance,created_at,updated_at,expires_at)
                VALUES(?,?,'',?,?,?,.9,?,?,?) ON CONFLICT(memory_id) DO UPDATE
                SET strength=excluded.strength,status='active',updated_at=excluded.updated_at,expires_at=excluded.expires_at""",
                       (key,self.scope,kind,topic,strength,now,now,now+90*86400))
            db.execute("INSERT INTO character_preference_history(memory_id,previous_strength,strength,reason,created_at) VALUES(?,?,?,'operator_update',?)",
                       (key,old[0] if old else .5,strength,now))
        return key
