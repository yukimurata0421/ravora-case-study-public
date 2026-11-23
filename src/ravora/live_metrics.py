"""Numeric, session-scoped history for the authenticated studio console."""
from collections import deque
import os
from pathlib import Path
import sqlite3
import threading
import time

from ravora.database import Connection


INTERVAL = 2
WINDOW = 300
PAGE_BYTES = os.sysconf('SC_PAGE_SIZE')
PROVIDERS = ('gemini', 'jev', 'tts')


def usage_totals(inbox, since, now):
    """Count each dispatched cloud request once from its own ledger."""
    counts = dict.fromkeys(PROVIDERS, 0)
    tokens = missing_tokens = characters = failed = unknown = 0
    stt = audio_seconds = 0
    if inbox.scope == 'cloud':
        try:
            with Connection(inbox.store.path, readonly=True) as db:
                db.execute('BEGIN')
                rows = db.execute("""
                    SELECT a.provider, COUNT(*), COUNT(a.total_tokens), SUM(a.total_tokens),
                           COUNT(a.characters), SUM(a.characters),
                           SUM(a.status='failed'), SUM(a.status='outcome_unknown')
                    FROM api_usage a
                    JOIN conversation_turns t ON t.turn_id=a.turn_id
                    WHERE a.provider IN ('gemini','jev','tts') AND a.mode='cloud' AND a.attempted=1
                      AND a.created_at>=? AND a.created_at<=? AND t.usage_session=?
                    GROUP BY a.provider
                """, (since, now, inbox.session)).fetchall()
                stt, timed, seconds = db.execute("""
                    SELECT COUNT(*), COUNT(duration_seconds), SUM(duration_seconds)
                    FROM developer_inputs WHERE session=? AND created_at>=? AND created_at<=?
                      AND mode='cloud' AND kind='audio' AND request_attempted=1
                """, (inbox.session, since, now)).fetchone()
                audio_seconds = (seconds or 0) if timed == stt else None
        except sqlite3.Error:
            return dict.fromkeys((*PROVIDERS, 'stt', 'calls', 'audio_seconds', 'tokens', 'missing_tokens',
                                  'characters', 'api_failed', 'api_unknown'))
        for provider, calls, reported, amount, with_chars, chars, failures, uncertain in rows:
            failed += failures
            unknown += uncertain
            counts[provider] = calls
            if provider == 'tts':
                characters = chars if with_chars == calls else None
            else:
                missing_tokens += calls - reported
                tokens += amount or 0
    if missing_tokens and missing_tokens == counts['gemini'] + counts['jev']:
        tokens = None
    return {**counts, 'stt': stt, 'calls': sum(counts.values()) + stt, 'audio_seconds': audio_seconds,
            'tokens': tokens, 'missing_tokens': missing_tokens, 'characters': characters,
            'api_failed': failed, 'api_unknown': unknown}


def minute_usage(inbox, now):
    return usage_totals(inbox, now - 60, now)


class StudioMetrics:
    def __init__(self, program):
        self.program = program
        self.points = deque(maxlen=WINDOW // INTERVAL + 1)
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.worker = None
        self.previous = None
        self.started_at = time.time()

    def start(self, writer):
        self.worker = threading.Thread(target=self._run, args=(writer,), name='ray-studio-metrics', daemon=True)
        self.worker.start()

    def _run(self, writer):
        while not self.stop.is_set():
            start = time.monotonic()
            self.sample(writer)
            self.stop.wait(max(0.1, INTERVAL - (time.monotonic() - start)))

    def sample(self, writer):
        p = self.program
        with p.lock:
            frame = p.frame
            turns = p.failed
            mic = sum(n for reason, n in p.host_stt_counts.items() if reason != 'accepted')
        transport = writer.transport.status() if writer.transport else {}
        now = time.monotonic()
        totals = {'fps': frame, 'encoded': writer.encoded_bytes, 'sent': transport.get('muxed_bytes'),
                  'cpu': time.process_time(), 'turn_errors': turns, 'mic_errors': mic,
                  'send_errors': transport.get('io_errors')}
        try:
            rss = int(Path('/proc/self/statm').read_text().split()[1]) * PAGE_BYTES / 2**20
        except (OSError, IndexError, ValueError):
            rss = None
        sampled_at = time.time()
        values = {**minute_usage(p.pipeline.inbox, sampled_at),
                  'api_total': usage_totals(p.pipeline.inbox, self.started_at, sampled_at), 'rss': rss,
                  'queued': transport.get('queue_packets'), 't': now}
        for key, value in totals.items():
            delta = None
            if self.previous is not None and value is not None:
                stamp, previous = self.previous
                if previous[key] is not None and now > stamp:
                    delta = value - previous[key]
                    if key in ('fps', 'cpu', 'encoded', 'sent'):
                        scale = {'fps': 1, 'cpu': 100, 'encoded': 8e-6, 'sent': 8e-6}[key]
                        delta = round(delta * scale / (now - stamp), 3)
            values[key] = delta
        self.previous = now, totals
        with self.lock:
            self.points.append(values)

    def snapshot(self):
        with self.lock:
            return {'interval': INTERVAL, 'window': WINDOW, 'points': list(self.points)}

    def close(self):
        self.stop.set()
        if self.worker:
            self.worker.join()
