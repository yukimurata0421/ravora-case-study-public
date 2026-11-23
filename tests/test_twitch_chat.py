import datetime as dt
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from ravora.comments import CommentInbox
from ravora.conversation import MemoryStore
try:
    from ravora.twitch_chat import DELETE, MESSAGE, SCOPE, WS, TwitchError, TwitchOAuth, TwitchReader
except ModuleNotFoundError as exc:
    if exc.name not in ('requests', 'websocket'):
        raise
    TwitchReader = None


def packet(kind, payload, **metadata):
    return json.dumps({'metadata': {'message_type': kind, **metadata}, 'payload': payload})


def welcome(session):
    return packet('session_welcome', {'session': {'id': session, 'keepalive_timeout_seconds': 10}})


def response(code, data=None):
    return SimpleNamespace(status_code=code, json=lambda: data)


@unittest.skipUnless(TwitchReader, 'youtube-live extra required')
class TwitchChatTests(unittest.TestCase):
    def setUp(self):
        folder = self.enterContext(TemporaryDirectory())
        self.root = Path(folder)
        self.cli = self.root / '.cli.env'
        self.cli.write_text('CLIENTID=test-app\nCLIENTSECRET=fake-secret\n'
                            'ACCESSTOKEN=old-access\nREFRESHTOKEN=old-refresh\n')
        self.cli.chmod(0o600)
        self.state = self.root / 'twitch/oauth.json'

    def test_initial_subscription_reconnect_and_duplicate_moderation(self):
        stop = Event()
        inbox = CommentInbox(MemoryStore(self.root / 'chat.db'))
        callback = Mock()
        reader = TwitchReader(inbox, self.state, config=self.cli, on_removed=callback)
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        content = {'broadcaster_user_id': '42', 'chatter_user_id': '4242',
                   'chatter_user_name': 'A viewer', 'message_id': 'message-1', 'message': {'text': 'こんにちは'}}
        received = packet('notification', {'event': content}, subscription_type=MESSAGE, message_timestamp=now)
        deletion = packet('notification', {'event': {'broadcaster_user_id': '42', 'message_id': 'message-1'}},
                          subscription_type=DELETE, message_timestamp=now)
        foreign = packet('notification', {'event': content | {'broadcaster_user_id': '99'}},
                         subscription_type=MESSAGE, message_timestamp=now)
        reconnect = WS + '?reconnect=fixture'
        first, second = Mock(), Mock()
        first.recv.side_effect = [welcome('initial'), foreign, received,
                                  packet('session_reconnect', {'session': {'reconnect_url': reconnect}})]
        replacement = iter([welcome('replacement'), received, deletion])
        def receive():
            item = next(replacement, None)
            if item is None:
                stop.set()
            return item
        second.recv.side_effect = receive
        http = Mock()
        http.get.return_value = response(200, {'client_id': 'test-app', 'user_id': '42', 'scopes': [SCOPE]})
        http.post.return_value = response(202)
        with (patch('ravora.twitch_chat.requests.Session', return_value=http),
              patch('ravora.twitch_chat.websocket.create_connection', side_effect=(first, second)) as connect):
            reader.run(stop)
        self.assertEqual([call.args[0] for call in connect.call_args_list], [WS, reconnect])
        subscriptions = [call.kwargs['json'] for call in http.post.call_args_list]
        self.assertEqual([row['type'] for row in subscriptions], [MESSAGE, DELETE])
        self.assertTrue(all(row['transport']['session_id'] == 'initial' for row in subscriptions))
        self.assertTrue(all(row['condition'] == {'broadcaster_user_id': '42', 'user_id': '42'} for row in subscriptions))
        first.shutdown.assert_called_once()
        second.shutdown.assert_called_once()
        callback.assert_called_once()
        self.assertEqual(reader.snapshot()['counts'], {'ignored_channel': 1, 'queued': 1, 'duplicate': 1, 'removed': 1})
        self.assertEqual(inbox.summary(), {'removed': 1})
        self.assertEqual(inbox.display_comments(None), [])

    def test_refresh_uses_atomic_private_cache_and_detects_wrong_account(self):
        http = Mock()
        account = {'client_id': 'test-app', 'user_id': '42', 'scopes': [SCOPE]}
        http.get.side_effect = [response(401), response(200, account)]
        http.post.return_value = response(200, {'access_token': 'new-access', 'refresh_token': 'new-refresh'})
        TwitchOAuth(self.cli, self.state, http).validate()
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o600)
        self.assertEqual(http.post.call_args.kwargs['data']['refresh_token'], 'old-refresh')
        self.assertIn('ACCESSTOKEN=old-access', self.cli.read_text())
        restored = TwitchOAuth(self.cli, self.state, http)
        self.assertEqual(restored.access, 'new-access')
        self.assertEqual(restored.refresh, 'new-refresh')
        http.get.side_effect = [response(200, account), response(200, account | {'user_id': '99'})]
        restored.validate()
        with self.assertRaisesRegex(TwitchError, 'TWITCH_CHAT_ACCOUNT_MISMATCH'):
            restored.validate()
