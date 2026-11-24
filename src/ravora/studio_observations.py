"""Durable studio observations. No provider credentials or audience content."""
import logging
import math
import queue
import re
import sqlite3
import threading
import time

from ravora.studio_report import LIVE_EVIDENCE_SECONDS


def stream_key(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', value):
        raise ValueError('INVALID_BROADCAST_ID')
    return value


class StudioObservations:
    def __init__(self, inbox, platform, prefetch, chat_source, stream_id=None, binding_source=None, *, sample_rate, fps):
        self.store = inbox.store
        self.session = inbox.session
        self.events = queue.Queue(maxsize=256)
        self.dropped = self.failed = 0
        self._audience = (float('-inf'), None)
        self.chat_matches = chat_source == platform
        self.chat_since = self.chat_last = self.chat_saved = None
        with self.store.connect() as db:
            db.execute("""INSERT INTO studio_sessions(session,platform,mode,sample_rate,fps,prefetch,chat_source,started,stream_id,binding_source,chat_tracked)
                VALUES(?,?,?,?,?,?,?,?,?,?,1)""", (self.session, platform, inbox.scope, sample_rate, fps, int(prefetch), chat_source,
                                          time.time(), stream_key(stream_id) if stream_id else None,binding_source))
            if inbox.speaker_rotation and chat_source in ('twitch','youtube','youtube-fixture'):
                db.execute("UPDATE studio_sessions SET selection_policy='speaker_rotation_v1' WHERE session=?",(self.session,))

        self.writer = threading.Thread(target=self._write, name='ray-studio-timing', daemon=True)
        self.writer.start()

    def _enqueue(self, sql, args):
        # Never stall the encoder or TTS receive callback behind a SQLite writer.
        try:
            self.events.put_nowait((sql, args))
        except queue.Full:
            self.dropped += 1

    def _write(self):
        while (event := self.events.get()) is not None:
            try:
                with self.store.connect() as db:
                    db.execute('PRAGMA busy_timeout=100')
                    db.execute(*event)
            except sqlite3.Error:
                self.failed += 1
                logging.getLogger(__name__).warning('STUDIO_TIMING_WRITE_FAILED')

    def speech_policy(self, frame, version):
        with self.store.connect() as db:
            db.execute("""INSERT INTO studio_speech_policies(session,first_frame,activated,policy)
                          VALUES(?,?,?,?) ON CONFLICT(session,first_frame) DO UPDATE
                          SET activated=excluded.activated,policy=excluded.policy""",
                       (self.session,frame,time.time(),version))

    def has_audience(self):
        return (self.audience_size() or 0) > 0

    def audience_size(self):
        observed, viewers = self._audience
        return viewers if 0 <= time.time()-observed <= LIVE_EVIDENCE_SECONDS else None

    def audience(self, observed, status, stream_id=None, viewers=None, started=None):
        known_viewers = None
        conflict = False
        try:
            with self.store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                if not db.execute('SELECT 1 FROM studio_sessions WHERE session=? AND stopped IS NULL',
                                  (self.session,)).fetchone():
                    return
                if status == 'live':
                    stream_key(stream_id)
                    if type(viewers) is not int or viewers < 0:
                        raise ValueError('INVALID_VIEWER_COUNT')
                    conflict, = db.execute("""UPDATE studio_sessions SET
                        identity_conflict=MAX(identity_conflict,stream_id IS NOT NULL AND stream_id!=?),
                        binding_source=COALESCE(binding_source,'twitch_helix'), stream_id=COALESCE(stream_id,?)
                        WHERE session=? RETURNING identity_conflict""", (stream_id, stream_id, self.session)).fetchone()
                db.execute('INSERT INTO studio_audience VALUES(?,?,?,?,?,?)',
                           (self.session, observed, status, stream_id, started, viewers))
            if status=='live' and not conflict:
                known_viewers = viewers
        finally:
            # Only a committed, stream-bound sample can enable an unsolicited turn.
            latest, _ = self._audience
            if observed >= latest or conflict:
                self._audience = (max(observed, latest), known_viewers)

    def _save_chat(self):
        if self.chat_since is not None and self.chat_last > self.chat_since:
            self._enqueue("""INSERT INTO studio_chat_intervals(session,started,ended) VALUES(?,?,?)
                ON CONFLICT(session,started) DO UPDATE SET ended=MAX(ended,excluded.ended)""",
                          (self.session, self.chat_since, self.chat_last))
            self.chat_saved = self.chat_last

    def chat_sample(self, reading, stamp):
        # Only join adjacent program observations. A suspended or crashed program
        # supplies no evidence for the time between its last frame and recovery.
        if self.chat_last is not None and not 0 <= stamp - self.chat_last <= 2:
            self._save_chat()
            self.chat_since = self.chat_last = None
        if reading and self.chat_matches:
            if self.chat_since is None:
                self.chat_since = self.chat_saved = stamp
            self.chat_last = stamp
            if stamp - self.chat_saved >= 5:
                self._save_chat()
        elif self.chat_since is not None:
            self._save_chat()
            self.chat_since = self.chat_last = None

    def provider_event(self, status):
        self._enqueue('INSERT INTO studio_provider_events(session,observed,status) VALUES(?,?,?)',
                      (self.session, time.time(), status))

    def ready(self, turn, stamp):
        self._enqueue('UPDATE studio_inputs SET ready_at=COALESCE(ready_at,?) WHERE turn_id=? AND session=?',
                      (stamp, turn, self.session))

    def first_signal(self, turn, previous_turn, sample, stamp):
        self._enqueue('UPDATE studio_inputs SET previous_turn=?, first_sample=?, first_written=? WHERE turn_id=? AND session=?',
                      (previous_turn, sample, stamp, turn, self.session))

    def end_signal(self, turn, sample, stamp):
        self._enqueue('UPDATE studio_inputs SET end_sample=?, last_written=? WHERE turn_id=? AND session=?',
                      (sample, stamp, turn, self.session))

    def close(self, frames, chat_status):
        stopped = time.time()
        self._save_chat()
        self.events.put(None)
        self.writer.join()
        with self.store.connect() as db:
            db.execute("""UPDATE studio_sessions SET stopped=?, frames=?, chat_status_at_stop=?, timing_write_losses=?
                WHERE session=? AND stopped IS NULL""",
                       (stopped, frames, chat_status, self.failed + self.dropped, self.session))


def import_observation(db, record):
    """Attach an operator's provider summary, test label or waveform match to its exact identity."""
    kind = record.get('kind')
    if kind == 'summary':
        platform, external = record['platform'], stream_key(record['stream_id'])
        if platform not in ('youtube', 'twitch'):
            raise ValueError('INVALID_PLATFORM')
        start, end = record['started_at'], record['ended_at']
        unique, avg, peak = record['unique_viewers'], record.get('average_viewers'), record.get('peak_viewers')
        if (not all(type(x) in (int,float) and math.isfinite(x) for x in (start,end)) or not 0 < start < end
                or type(unique) is not int or unique < 0 or (peak is not None and (type(peak) is not int or peak < 0))
                or (avg is not None and (type(avg) not in (int,float) or not math.isfinite(avg) or avg < 0))):
            raise ValueError('INVALID_STREAM_SUMMARY')
        session = record.get('session')
        if session is not None:
            row = db.execute('SELECT platform,stream_id,identity_conflict FROM studio_sessions WHERE session=?',
                             (session,)).fetchone()
            if not row or row[0] != platform or row[1] not in (None,external) or row[2]:
                raise ValueError('SESSION_BROADCAST_MISMATCH')
            db.execute('UPDATE studio_sessions SET stream_id=?,binding_source=COALESCE(binding_source,?) WHERE session=?', (external,'operator_summary',session))
        values = (platform,external,start,end,unique,avg,peak)
        db.execute('INSERT OR IGNORE INTO studio_stream_summaries VALUES(?,?,?,?,?,?,?)', values)
        old = db.execute('SELECT * FROM studio_stream_summaries WHERE platform=? AND stream_id=?',
                         (platform,external)).fetchone()
        if tuple(old) != values:
            raise ValueError('CONFLICTING_STREAM_SUMMARY')
    elif kind in ('player_signal','test_comment'):
        row = db.execute("""SELECT receipt,published,first_written,player_at,player_capture_sha256,player_correlation FROM studio_inputs
            WHERE session=? AND receipt=? AND source IN ('youtube','twitch','youtube-fixture')""",
                         (record['session'],record['receipt'])).fetchone()
        if not row:
            raise ValueError('UNKNOWN_VIEWER_COMMENT')
        if kind == 'test_comment':
            db.execute('UPDATE studio_inputs SET test=1 WHERE receipt=?', (row[0],))
        else:
            stamp = record['observed_at']
            if (record.get('method') != 'waveform_match' or type(stamp) not in (int,float)
                    or not math.isfinite(stamp) or row[2] is None or stamp < max(row[1],row[2])
                    or row[3] not in (None,stamp)
                    or not db.execute('SELECT 1 FROM studio_inputs WHERE receipt=? AND turn_id=?',
                                      (row[0],record['turn_id'])).fetchone()):
                raise ValueError('INVALID_PLAYER_MATCH')
            digest, score = record.get('capture_sha256'), record.get('correlation')
            if (digest is None) != (score is None) or (digest is not None and (
                    not isinstance(digest,str) or not re.fullmatch('[0-9a-f]{64}',digest)
                    or type(score) not in (int,float) or not .0 <= score <= 1
                    or row[4] not in (None,digest) or row[5] not in (None,score))):
                raise ValueError('INVALID_PLAYER_EVIDENCE')
            db.execute("""UPDATE studio_inputs SET player_at=?,player_capture_sha256=COALESCE(player_capture_sha256,?),
                        player_correlation=COALESCE(player_correlation,?) WHERE receipt=?""",
                       (stamp,digest,score,row[0]))
    else:
        raise ValueError('UNKNOWN_OBSERVATION_KIND')
