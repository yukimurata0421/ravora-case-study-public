"""Small source-backed character memories. Called only by the existing worker."""
import datetime as dt
import hashlib
import json
import logging
import re
import sqlite3
import time

from ravora.gemini_memory import source_block_reason
from ravora.ray_character import INTEREST_DESCRIPTIONS, TOPICS, TRAITS, owner_hash, topics_in
from ravora.dialogue_router import HOOKS, mentions_hook


def character_source_allowed(text):
    return bool(text and not source_block_reason(text) and not re.search(
        r'(?i)\b(private|off[ -]?stream|between us|keep (?:this|it) to yourself)\b|非公開|内緒|配信に出さない', text))


def claim_hook(store, scope, viewer, turn, text, preferred=()):
    """Give ordinary worker commits a short window; skip a busy notebook."""
    if not character_source_allowed(text):
        return ''
    now = time.time()
    first = preferred[0] if preferred else ''
    second = preferred[1] if len(preferred) > 1 else ''
    try:
        with store.connect() as db:
            db.execute('PRAGMA busy_timeout=30')
            row = db.execute("""UPDATE dialogue_hooks SET claimed_turn=?, claim_until=?
                WHERE rowid=(SELECT h.rowid FROM dialogue_hooks h JOIN source_events s ON s.id=h.source_id
                    WHERE h.scope=? AND h.owner_hash=? AND h.status='unspoken' AND s.is_deleted=0
                    AND s.raw_purged_at IS NULL AND s.expires_at>?
                    AND (h.claimed_turn=? OR h.claim_until IS NULL OR h.claim_until<=? OR NOT EXISTS(
                        SELECT 1 FROM conversation_turns t WHERE t.turn_id=h.claimed_turn
                        AND t.status IN ('running','completed') AND t.played=0))
                    ORDER BY (h.claimed_turn IS ?) DESC,(h.hook=?) DESC,(h.hook=?) DESC,h.created_at,h.rowid LIMIT 1)
                AND EXISTS(SELECT 1 FROM conversation_turns WHERE turn_id=? AND viewer=? AND status='running')
                RETURNING hook""", (turn, now+300, scope, owner_hash(scope,viewer), now, turn, now, turn,
                                    first, second, turn, scope+':'+viewer)).fetchone()
        return row[0] if row and row[0] in HOOKS else ''
    except (sqlite3.Error, OSError):
        logging.getLogger(__name__).info('dialogue_hook_lookup_unavailable')
        return ''


def played_hooks(db, turn, source, scope, viewer, text, reply, hooks, now):
    """Part of the existing first-playback transaction. Store only curated subjects."""
    row = db.execute('SELECT hook FROM dialogue_hooks WHERE claimed_turn=?', (turn,)).fetchone()
    if row:
        status = 'used' if mentions_hook(reply, row[0]) else 'unspoken'
        db.execute('UPDATE dialogue_hooks SET status=?,claimed_turn=NULL,claim_until=NULL WHERE claimed_turn=?', (status, turn))
    if not source or not hooks or not character_source_allowed(text):
        return
    owner = owner_hash(scope, viewer)
    for hook in hooks[:2]:
        if hook in HOOKS and not mentions_hook(reply, hook):
            db.execute("""INSERT OR IGNORE INTO dialogue_hooks(scope,owner_hash,hook,source_id,created_at)
                SELECT ?,?,?,id,? FROM source_events WHERE id=? AND viewer_key=? AND platform!='ray-initiative'
                AND is_deleted=0 AND raw_purged_at IS NULL AND expires_at>?""", (scope,owner,hook,now,source,viewer,now))


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
            rows = db.execute("""SELECT m.memory_id,owner_hash,kind,topic,strength,importance,
                    EXISTS(SELECT 1 FROM character_preference_history h WHERE h.memory_id=m.memory_id
                           AND h.reason='operator_update')
                FROM character_memories m WHERE scope=? AND status='active' AND expires_at>?
                ORDER BY importance DESC,updated_at DESC LIMIT 64""", (self.scope, time.time())).fetchall()
        return tuple(dict(zip(('id','owner_hash','kind','topic','strength','importance','configured'), r))
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
            if not row or row[4] == 'ray-initiative' or not character_source_allowed(row[2]): return 0
            event, viewer, text, reply, _ = row
            topics = topics_in(text)
            if not topics: return 0
            owner = owner_hash(self.scope, viewer)
            previous = db.execute("""SELECT t.comment FROM conversation_turns t JOIN source_events s ON s.id=t.source_event_id
                WHERE t.viewer=? AND t.played=1 AND t.status='completed' AND s.is_deleted=0
                    AND s.raw_purged_at IS NULL AND s.expires_at>? ORDER BY t.created_at DESC LIMIT 24""",
                                  (self.scope + ':' + viewer, now)).fetchall()
            for topic in topics[:2]:
                repeated = sum(topic in topics_in(p[0]) and character_source_allowed(p[0]) for p in previous) >= 3
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
                db.execute("UPDATE character_memories SET strength=?,updated_at=?,expires_at=MAX(expires_at,?),status='active' WHERE memory_id=?",
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
            db.execute('DELETE FROM dialogue_hooks WHERE scope=?', (self.scope,))

    def set_preference(self, topic, strength, *, kind='preference'):
        if topic not in TOPICS or kind not in ('preference','value','interest') or type(strength) not in (int,float) or not 0<=strength<=1:
            raise ValueError('CHARACTER_PREFERENCE_INVALID')
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            return self._write_preference(db, topic, strength, kind)

    def seed_initial_interests(self):
        """Initialize this studio's public character profile, preserving prior decisions."""
        try:
            with self.store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                for topic in INTEREST_DESCRIPTIONS:
                    self._write_preference(db, topic, .6, 'interest', only_if_missing=True)
        except sqlite3.Error:
            logging.getLogger(__name__).warning('character_initial_interests_unavailable')

    def _write_preference(self, db, topic, strength, kind, *, only_if_missing=False):
        now=time.time();key=hashlib.sha256((self.scope+'\0\0'+kind+'\0'+topic).encode()).hexdigest()
        old=db.execute('SELECT strength FROM character_memories WHERE memory_id=?',(key,)).fetchone()
        if only_if_missing and old:
            return key
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
