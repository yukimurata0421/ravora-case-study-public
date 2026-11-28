"""Short-lived, acknowledged Twitch exchanges, scoped to one program and viewer."""
from collections import OrderedDict
from dataclasses import dataclass
from threading import RLock, Timer
from time import monotonic, time

from ravora.conversation_context import sensitive
from ravora.gemini_memory import source_block_reason
from ravora.viewer_recognition import name_command


@dataclass(frozen=True)
class Exchange:
    source: str
    turn: str
    played_at: float
    question: str
    answer: str
    revision: int


class SessionWindow:
    def __init__(self, *, idle=600, capacity=128, clock=monotonic):
        self.idle = idle
        self.capacity = capacity
        self.clock = clock
        self.entries = OrderedDict()
        self.lock = RLock()
        self.timer = None
        self.closed = False

    def _expire(self, now):
        while self.entries and next(iter(self.entries.values()))[0] <= now:
            self.entries.popitem(last=False)

    def _schedule(self):
        if self.entries and self.timer is None and not self.closed:
            expires, _ = next(iter(self.entries.values()))
            self.timer = Timer(max(0, expires-self.clock()), self._tick)
            self.timer.daemon = True
            self.timer.start()

    def _tick(self):
        with self.lock:
            self.timer = None
            self._expire(self.clock())
            self._schedule()

    def remember(self, key, item):
        if (not item.question or not item.answer or len(item.question)>1000 or len(item.answer)>1000
                or sensitive(item.question) or sensitive(item.answer) or source_block_reason(item.question)
                or source_block_reason(item.answer) or name_command(item.question)[0]):
            return
        with self.lock:
            if self.closed:
                return
            now = self.clock()
            self._expire(now)
            _, rows = self.entries.pop(key, (0, ()))
            by_id = {line.turn: line for line in (*rows, item)}
            rows = sorted(by_id.values(), key=lambda e: e.played_at)[-4:]
            self.entries[key] = (now + self.idle, tuple(rows))
            if len(self.entries) > self.capacity:
                self.entries.popitem(last=False)
            self._schedule()

    def previous(self, key, as_of, *, touch=False):
        with self.lock:
            now = self.clock()
            self._expire(now)
            entry = self.entries.get(key)
            if not entry:
                return ()
            _, rows = entry
            if touch:
                self.entries.move_to_end(key)
                self.entries[key] = (now + self.idle, rows)
            return tuple(row for row in rows if row.played_at <= as_of)

    def invalidate(self):
        # Replies may paraphrase earlier sources. Drop derived conversation context too.
        with self.lock:
            self.entries.clear()

    def close(self):
        with self.lock:
            self.closed = True
            self.entries.clear()
            if self.timer:
                self.timer.cancel()
                self.timer = None


def validated(store, key, rows):
    """Look up at most four primary keys and detect deletions, expiry or edits by another writer."""
    if not rows:
        return ()
    marks = ','.join('?' for _ in rows)
    with store.connect() as db:
        live = {turn: (source, question, answer) for turn, source, question, answer in db.execute(f"""
            SELECT t.turn_id,s.id,t.comment,t.reply FROM conversation_turns t
            JOIN source_events s ON s.id=t.source_event_id
            WHERE t.turn_id IN ({marks}) AND t.viewer=? AND s.stream_id=? AND s.viewer_key=?
            AND t.played=1 AND t.status='completed' AND s.platform='twitch'
            AND (SELECT version FROM session_evidence_revision WHERE id=1)=?
            AND s.text=t.comment AND s.is_deleted=0 AND s.raw_purged_at IS NULL AND s.expires_at>?""",
            (*(row.turn for row in rows), key[0], key[1], key[0].partition(':')[2], rows[-1].revision, time()))}
    return tuple(row for row in rows if row.revision==rows[-1].revision and live.get(row.turn) == (row.source,row.question,row.answer))


def messages(rows, count):
    return [(role,text) for row in (rows[-count:] if count else ())
            for role,text in (('user',row.question),('model',row.answer))]
