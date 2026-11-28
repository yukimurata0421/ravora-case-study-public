"""Choose a bounded set of speakers for the single studio voice."""
from dataclasses import dataclass
import hashlib
import json
import sqlite3
import unicodedata


VIEWER_CHAT_SOURCES = ('youtube', 'youtube-fixture', 'twitch')
OPPORTUNITIES = ('first', 'continuation', 'first', 'open')
ACTIVE = ('claimed', 'processing', 'generated')
# Only these impersonal, equivalent requests can be answered together.
MUSIC_QUESTIONS = {
    'このbgmは何', 'このbgmはなに', 'この曲は何', 'この曲の名前は', '今の曲名は', 'この曲名は',
    'what is this song', "what's this song", 'what is the name of this song',
    'what song is playing', 'what is the bgm', 'what is this bgm',
}


def music_question(text):
    value = ' '.join(unicodedata.normalize('NFKC', text).casefold().split()).rstrip(' ?!')
    return value in MUSIC_QUESTIONS


def records(db, sql, args):
    cursor = db.cursor()
    cursor.row_factory = sqlite3.Row
    return cursor.execute(sql, args)


@dataclass
class Candidate:
    receipt: str
    viewer: str
    text: str
    published: float
    waiting_since: float
    ticket: str
    reply_parent: str | None = None
    served: bool = False
    question: str | None = None
    busy: bool = False


def waiting(db, session):
    return [Candidate(*row) for row in db.execute("""
        SELECT i.receipt,i.viewer,i.text,i.published,COALESCE(c.waiting_since,i.received),
               COALESCE(c.ticket,i.viewer),c.reply_parent
        FROM comment_inbox i LEFT JOIN chat_candidates c USING(receipt)
        WHERE i.session=? AND i.status='queued' AND i.source IN (?,?,?) AND c.group_of IS NULL
        """, (session,*VIEWER_CHAT_SOURCES))]


def broadcast_sessions(db, session):
    rows = db.execute("""SELECT b.session FROM studio_sessions a JOIN studio_sessions b
        ON b.platform=a.platform AND b.stream_id=a.stream_id
        WHERE a.session=? AND a.identity_conflict=0 AND b.identity_conflict=0""", (session,))
    return json.dumps([r[0] for r in rows] or [session])


def classify(db, session, sessions, candidates, now, ttl):
    viewers = json.dumps([c.viewer for c in candidates])
    served = {r[0] for r in db.execute("""SELECT DISTINCT viewer FROM studio_inputs
        WHERE session IN (SELECT value FROM json_each(?)) AND viewer IN (SELECT value FROM json_each(?))
        AND played_at IS NOT NULL AND status!='removed' AND COALESCE(reply_kind,'individual')='individual'""",
        (sessions, viewers))}
    busy = {r[0] for r in db.execute("""SELECT DISTINCT i.viewer FROM comment_inbox i
        LEFT JOIN chat_candidates c USING(receipt) WHERE i.session=?
        AND (i.status IN (?,?,?) OR (i.status='queued' AND c.group_of IS NOT NULL))""", (session,*ACTIVE))}
    # The last individually played turn, even if it superseded an earlier question.
    recent = {}
    for row in records(db, """SELECT i.viewer,i.message_id,c.receipt,c.played_at,c.asked,
            EXISTS(SELECT 1 FROM chat_candidates child JOIN comment_inbox ci ON ci.receipt=child.receipt
                   WHERE child.parent_question=c.receipt AND (child.played_at IS NOT NULL OR
                         (ci.session=? AND ci.status IN (?,?,?)))) AS used
        FROM comment_inbox i JOIN chat_candidates c USING(receipt)
        WHERE i.session IN (SELECT value FROM json_each(?)) AND i.viewer IN (SELECT value FROM json_each(?))
          AND c.played_at IS NOT NULL AND c.reply_kind='individual' AND i.status!='removed'
        ORDER BY c.played_at DESC,c.receipt""", (session,*ACTIVE,sessions,viewers)):
        recent.setdefault(row['viewer'],row)
    for candidate in candidates:
        last = recent.get(candidate.viewer)
        candidate.served = candidate.viewer in served or last is not None
        candidate.busy = candidate.viewer in busy
        if (last and last['asked'] and not last['used'] and now-last['played_at'] < ttl
                and candidate.published >= last['played_at']
                and (candidate.reply_parent == last['message_id'] if candidate.reply_parent
                     else not candidate.text.rstrip().endswith(('?', '？')))):
            candidate.question = last['receipt']


def admission(db, inbox, comment, now):
    """Reserve one waiting position per speaker. Replacement keeps its original age and ticket."""
    ticket = hashlib.sha256(f'{inbox.session}/{int(now // inbox.ttl)}/{comment.viewer}'.encode()).hexdigest()
    candidate = Candidate(comment.receipt, comment.viewer, comment.text, comment.published_at,
                          now, ticket, comment.reply_parent)
    # Preserve an unanswered original when the viewer repeats it under a new message ID.
    repeated = db.execute("""SELECT 1 FROM comment_inbox i LEFT JOIN chat_candidates c USING(receipt)
        WHERE i.session=? AND i.source=? AND i.stream_id=? AND i.viewer=? AND i.text=?
        AND c.reply_parent IS ? AND i.status IN ('queued','claimed','processing','generated')
        AND i.received BETWEEN ? AND ? AND i.expires>? LIMIT 1""",
        (inbox.session, comment.source, comment.stream_id, comment.viewer, comment.text,
         comment.reply_parent, now-min(5, inbox.ttl), now, now)).fetchone()
    if repeated:
        return 'dropped', 'repeat_pending_comment', candidate
    pool = waiting(db, inbox.session)
    for other in pool:
        if other.viewer == candidate.viewer:
            if other.published > candidate.published:
                return 'dropped', 'older_update', candidate
            candidate.waiting_since, candidate.ticket = other.waiting_since, other.ticket
            db.execute("UPDATE comment_inbox SET status='dropped',reason='viewer_updated' WHERE receipt=?", (other.receipt,))
            return 'queued', None, candidate
    # Members of a prepared group keep their seats until it plays or is cancelled.
    locked = db.execute("""SELECT count(*) FROM comment_inbox i JOIN chat_candidates c USING(receipt)
        WHERE i.session=? AND i.status='queued' AND c.group_of IS NOT NULL""", (inbox.session,)).fetchone()[0]
    room = max(0, inbox.capacity - locked)
    if len(pool) < room:
        return 'queued', None, candidate
    if not room:
        return 'dropped', 'queue_full', candidate
    pool.append(candidate)
    classify(db, inbox.session, broadcast_sessions(db, inbox.session), pool, now, inbox.ttl)
    # Stable per-speaker lottery within each class. Reposting cannot buy another ticket.
    pool.sort(key=lambda c: (c.busy, c.ticket, c.viewer))
    for index in range(room):
        preferred = OPPORTUNITIES[index % len(OPPORTUNITIES)]
        keep = next((c for c in pool if not c.busy and
                     ((preferred == 'first' and not c.served) or
                      (preferred == 'continuation' and c.question))), pool[0])
        pool.remove(keep)
    loser, = pool
    if loser.receipt == candidate.receipt:
        return 'dropped', 'queue_full', candidate
    db.execute("UPDATE comment_inbox SET status='dropped',reason='queue_replaced' WHERE receipt=?", (loser.receipt,))
    return 'queued', None, candidate


def select(db, inbox, now):
    pool = waiting(db, inbox.session)
    if not pool:
        return None
    sessions = broadcast_sessions(db, inbox.session)
    classify(db, inbox.session, sessions, pool, now, inbox.ttl)
    pool = sorted((c for c in pool if not c.busy), key=lambda c: (c.waiting_since, c.ticket, c.viewer))
    if not pool:
        return None
    recent = list(records(db, """SELECT c.opportunity,c.reply_kind,i.viewer,COUNT(*) OVER() AS turns
        FROM comment_inbox i JOIN chat_candidates c USING(receipt)
        WHERE i.session IN (SELECT value FROM json_each(?)) AND c.claimed_at IS NOT NULL
          AND (c.played_at IS NOT NULL OR (i.session=? AND i.status IN (?,?,?)))
        ORDER BY c.claimed_at DESC,i.rowid DESC LIMIT 4""", (sessions,inbox.session,*ACTIVE)))
    block = recent[:recent[0]['turns'] % len(OPPORTUNITIES)] if recent else []
    unused = list(OPPORTUNITIES)
    for row in block:
        if row['opportunity'] in unused:
            unused.remove(row['opportunity'])
    if len(recent)>=2 and recent[0]['reply_kind']==recent[1]['reply_kind']=='individual' and recent[0]['viewer']==recent[1]['viewer']:
        pool = [c for c in pool if c.viewer!=recent[0]['viewer']] or pool
    followup = next((c for c in pool if c.question), None)
    music = [c for c in pool if music_question(c.text)]
    first = next((c for c in pool if not c.served), None)
    members = []
    if followup and 'continuation' in unused:
        chosen, lane, opportunity = followup, 'continuation', 'continuation'
    elif len(music)>=2 and 'open' in unused:
        chosen, *members = music
        lane = opportunity = 'open'
    elif first and 'first' in unused:
        chosen, lane, opportunity = first, 'first', 'first'
    else:
        chosen, lane = pool[0], 'open'
        opportunity = 'open' if 'open' in unused else unused[0]
    db.execute("""INSERT OR IGNORE INTO chat_candidates(receipt,waiting_since,ticket)
                  VALUES(?,?,?)""", (chosen.receipt,chosen.waiting_since,chosen.ticket))
    db.execute("""UPDATE chat_candidates SET claimed_at=?,opportunity=?,lane=?,reply_kind=?,parent_question=?
                  WHERE receipt=?""", (now,opportunity,lane,'group' if members else 'individual',
                                       chosen.question if lane=='continuation' else None,chosen.receipt))
    db.executemany('UPDATE chat_candidates SET group_of=? WHERE receipt=?',
                   [(chosen.receipt,c.receipt) for c in members])
    return chosen.receipt


def reply_owner(receipt, viewer, kind):
    # Group replies have no individual memory owner, including the representative speaker.
    return 'audience:' + receipt if kind == 'group' else viewer


def reply_text(text, kind):
    if kind != 'group':
        return text
    if text.isascii():
        return 'Several viewers are asking about the background music. Answer the room together. ' + text
    return '複数の視聴者からBGMについて同じ質問です。みんなに向けて一度答えてください。' + text
