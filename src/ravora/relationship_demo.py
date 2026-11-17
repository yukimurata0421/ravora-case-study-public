"""Reproduce contact-state inputs without asking a model to invent responses."""
from pathlib import Path
import socket
import tempfile
from unittest.mock import patch
import uuid

from ravora.comments import Comment, CommentInbox
from ravora.conversation import MemoryStore
from ravora.providers import Transport


def relationship_replay():
    # Fixed fixture clock: dates describe synthetic history, not real viewers.
    today = 1780000000.0
    now = today - 65 * 86400
    serial = 0
    with tempfile.TemporaryDirectory(prefix='ravora-relationships-') as directory, \
            patch.object(Transport, 'open', side_effect=AssertionError('LIVE_PROVIDER_FORBIDDEN')), \
            patch.object(socket, 'create_connection', side_effect=AssertionError('NETWORK_FORBIDDEN')):
        store = MemoryStore(Path(directory) / 'conversation.sqlite3')
        inbox = CommentInbox(store, clock=lambda: now)

        def post(author, day, text=None, *, play=False, generate=False, source='youtube-fixture'):
            nonlocal now, serial
            now = today + day * 86400
            serial += 1
            comment = Comment(source, 'fictional-chat', str(serial), author,
                              'Fictional viewer', text or f'Fictional topic {serial}.', now)
            assert inbox.put(comment) == 'queued'
            assert inbox.take()['receipt'] == comment.receipt
            if play or generate:
                turn = uuid.uuid4().hex
                assert inbox.bind(comment.receipt, turn)
                with patch('ravora.conversation.time.time', return_value=now):
                    store.start(turn, 'offline:' + comment.viewer, comment.text)
                    store.finish(turn, 'A synthetic reply.', 'completed', {})
                    if play:
                        assert store.played(turn, scope='offline', memory_enabled=False)
                        before = store.relationship_snapshot(comment.viewer, scope='offline', now=now)
                        assert store.played(turn, scope='offline', memory_enabled=False)
                        assert store.relationship_snapshot(comment.viewer, scope='offline', now=now) == before
            inbox.finish(comment.receipt, 'played' if play else 'dropped')
            return comment

        try:
            for author, first in (('regular', -20), ('returning', -65)):
                for day in range(first, first + 20):
                    post(author, day, play=True)
                    post(author, day)
            profiles = {}
            for author in ('new', 'regular', 'returning'):
                comment = post(author, 0, 'Do we know each other?')
                snapshot = store.relationship_snapshot(comment.viewer, scope='offline', now=today)
                profiles[author] = store.relationship_context(comment.viewer, scope='offline', now=today)
                if author == 'returning':
                    assert snapshot['return_gap_days'] == 46
                assert inbox.put(comment) == 'duplicate'
                assert store.relationship_snapshot(comment.viewer, scope='offline', now=today) == snapshot
            assert profiles['new']['prior_contact'] == 'none_recorded'
            assert profiles['regular']['recent_pattern'] == 'ongoing_contact'
            assert profiles['returning']['recent_pattern'] == 'reconnecting_after_a_gap'
            assert all(isinstance(value, str) for profile in profiles.values() for value in profile.values())

            for index in range(100):
                burst = post('one-day', 0)
                distributed = post('twenty-days', -19 + index // 5)
            a = store.relationship_snapshot(burst.viewer, scope='offline', now=today)
            b = store.relationship_snapshot(distributed.viewer, scope='offline', now=today)
            assert a['posted_comments'] == b['posted_comments'] == 100
            assert (a['familiarity_units'], b['familiarity_units']) == (3, 60)

            unplayed = post('unplayed', 0, generate=True)
            assert store.relationship_snapshot(unplayed.viewer, scope='offline', now=today)['played_exchanges'] == 0
            private = post('developer', 0, source='developer')
            assert store.relationship_context(private.viewer, scope='offline', now=today) is None
            assert store.relationship_context(burst.viewer, scope='cloud', now=today) is None
            with store.connect() as db:
                assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
                assert not db.execute('PRAGMA foreign_key_check').fetchall()
                assert db.execute('SELECT COUNT(*) FROM provider_calls').fetchone()[0] == 0
            result = {
                'status': 'PASS_RELATIONSHIP_REPLAY', 'synthetic_data': True,
                'schema_version': store.status()['user_version'], 'backgrounds': profiles,
                'same_post_count': {'comments_each': 100, 'one_day_units': a['familiarity_units'],
                                    'twenty_day_units': b['familiarity_units']},
                'duplicate_receipts_and_playback_acks_idempotent': True,
                'generated_but_unplayed_excluded': True, 'developer_and_scope_isolation': True,
                'external_api_calls': 0, 'playback_acknowledgement': 'simulated',
                'generated_response_quality_evaluated': False, 'latency_measurement': False,
                'temporary_database_removed_on_exit': True,
            }
        finally:
            inbox.close()
    return result
