"""Bounded Twitch ingress. A single writer publishes only committed admissions."""
from collections import Counter, deque
from dataclasses import dataclass
import logging
import sqlite3
import threading

from ravora.twitch_language_hint import remember_language
from ravora.comments import Comment, clean, provider_name, timestamp


@dataclass(frozen=True)
class Deletion:
    channel: str
    message_id: str


def chat_event(reader, metadata, payload, identity):
    try:
        event = payload['event']
        if event['broadcaster_user_id'] != identity or event.get('source_broadcaster_user_id') not in (None, identity):
            reader.counts['ignored_channel'] += 1
            return None
        kind = metadata['subscription_type']
        if kind == 'channel.chat.message_delete':
            return Deletion(identity, clean(event['message_id'], 128))
        if kind != 'channel.chat.message':
            return None
        if (reader.is_announcement and event['chatter_user_id'] == identity
                and reader.is_announcement(event['message_id'], event['message']['text'])):
            reader.counts['ignored_welcome'] += 1
            return None
        return Comment('twitch', identity, clean(event['message_id'], 128), clean(event['chatter_user_id'], 128),
                       provider_name(event.get('chatter_user_name'), event.get('chatter_user_login')),
                       clean(event['message']['text'], 1000), timestamp(metadata['message_timestamp']),
                       clean(event['reply']['parent_message_id'], 128) if event.get('reply') else None)
    except (AttributeError, KeyError, TypeError, ValueError):
        reader.counts['invalid'] += 1
        return None


class TwitchIntake:
    def __init__(self, reader, insert):
        self.reader = reader
        self.insert = insert
        self.pending = deque()
        self.changed = threading.Condition()
        self.closing = False
        self.failed = False
        self.committed = threading.Event()
        self.worker = threading.Thread(target=self._run, name='twitch-inbox', daemon=True)

    def notify(self, metadata, payload, identity):
        received = self.reader.inbox.clock()
        event = chat_event(self.reader, metadata, payload, identity)
        if event is None:
            return
        with self.changed:
            self.changed.wait_for(lambda: len(self.pending) < 64 or self.closing or self.failed)
            if self.failed:
                raise sqlite3.OperationalError('TWITCH_CHAT_DB_ERROR')
            if self.closing:
                return
            self.pending.append((received, event))
            self.changed.notify_all()
        if isinstance(event, Comment):
            remember_language(event.text)

    def close(self):
        with self.changed:
            self.closing = True
            self.changed.notify_all()
        self.worker.join()

    def _run(self):
        while True:
            with self.changed:
                self.changed.wait_for(lambda: self.pending or self.closing)
                if not self.pending:
                    return
                # Never wait for a second comment; take only what is already available.
                batch = [self.pending.popleft() for _ in range(min(8, len(self.pending)))]
                self.changed.notify_all()
            committed = False
            try:
                try:
                    removed = self._persist(batch)
                except sqlite3.OperationalError as exc:
                    # A busy/locked transaction has rolled back on connection close.
                    if (getattr(exc, 'sqlite_errorcode', 0) & 255) not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                        raise
                    removed = self._persist(batch)
                committed = True
                if removed and self.reader.on_removed:
                    self.reader.on_removed()
            except Exception:
                with self.changed:
                    self.failed = True
                    self.reader.counts['intake_failed_or_unconfirmed'] += len(self.pending) + (0 if committed else len(batch))
                    self.reader.primary_status, self.reader.code = 'failed', 'TWITCH_CHAT_DB_ERROR'
                    self.changed.notify_all()
                logging.getLogger(__name__).exception('TWITCH_INBOX_WRITE_FAILED')
                return

    def _persist(self, batch):
        reader = self.reader
        inbox = reader.inbox
        counts = Counter()
        removed = {}
        with reader.event_lock:
            with inbox.store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                for received, event in batch:
                    if event.message_id in reader.deleted_ids or event.message_id in removed:
                        continue
                    if isinstance(event, Deletion):
                        inbox._remove(db, 'twitch', event.channel, event.message_id)
                        removed[event.message_id] = None
                    else:
                        counts[self.insert(db, event, received_at=received)] += 1
                display_changed = bool(db.total_changes or removed)
            reader.deleted_ids.update(removed)
            while len(reader.deleted_ids) > 4096:
                reader.deleted_ids.popitem(last=False)
            counts['removed'] += len(removed)
            counts['intake_commits'] = 1
            counts['intake_batched_commits'] = int(len(batch) > 1)
            reader.counts.update(+counts)
            if display_changed:
                inbox.display_revision += 1
            if removed:
                inbox.removal_revision += 1
            if counts['queued']:
                self.committed.set()
        return bool(removed)

    def wake_idle(self, program):
        # Read only on the encoder thread, before the existing admission/priority checks.
        if self.committed.is_set():
            self.committed.clear()
            program.next_check = 0


def attach_intake(program, reader):
    """Install the commit-before-playout writer before starting either reader."""
    if getattr(reader, 'intake', None) is not None:
        return True
    intake = TwitchIntake(reader, reader.inbox._put)
    with reader.event_lock:
        intake.worker.start()
        reader.intake = intake
        reader._notification = intake.notify
        if reader.irc:
            reader.irc.notify = intake.notify
    program.pipeline.twitch_intake = program.twitch_intake = intake
    return True
