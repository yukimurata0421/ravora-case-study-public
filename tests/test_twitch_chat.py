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

    def test_scheduled_audience_survives_missing_chat_grant_and_expired_fallback(self):
        import time
        import os
        self.enterContext(patch.dict(os.environ, RAVORA_TWITCH_CHANNEL="fixture_channel"))
        channel_name="fixture_channel"
        from ravora.twitch_audience import TwitchAudience
        from ravora.twitch_schedule_api import METADATA_SCOPE, prepare_channel
        from ravora.youtube_auth import save_private
        folder = self.state.parent
        folder.mkdir()
        self.cli.write_text('CLIENTID=test-app\n')
        grant = {'client_id':'test-app','user_id':'42','access_token':'channel','refresh_token':'fixture'}
        save_private(folder/'schedule-oauth.json',grant)
        save_private(folder/'market-app.json',{'client_id':'test-app','access_token':'app.test','expires':time.time()+80})
        http = Mock()
        def get(url,**options):
            if url.endswith('/validate'):
                return response(200,{'client_id':'test-app','user_id':'42','login':channel_name,'scopes':[METADATA_SCOPE]})
            if url.endswith('/games'):
                return response(200,{'data':[{'id':'9','name':'Just Chatting'}]})
            if url.endswith('/channels'):
                return response(200,{'data':[{'broadcaster_id':'42','broadcaster_login':channel_name,'game_id':'9','broadcaster_language':'ja','tags':['VTuber']}]})
            if url.endswith('/streams'):
                self.assertEqual(options['params']['user_id'],'42')
                return response(200,{'data':[]})
            raise AssertionError(url)
        http.get.side_effect = get
        from contextlib import nullcontext
        with patch('ravora.twitch_schedule_api.requests.Session',return_value=nullcontext(http)), patch('ravora.twitch_schedule_api.cli_config_path',return_value=self.cli):
            account = prepare_channel(folder, 'ja', update=False, independent_chat=True)
        self.assertTrue(account['matches_slot'])
        reader = TwitchReader(None,self.state,config=self.cli,expected_user_id=account['user_id'])
        reader.run(Event(),seconds=.2)
        self.assertEqual((reader.status,reader.code),('failed','TWITCH_CHAT_RUN_CLI_AUTHORIZATION'))
        # A malformed scheduled fallback must not shadow a valid app grant.
        save_private(folder/'schedule-oauth.json',dict(grant,access_token='bad token'))
        observer = TwitchAudience(folder,Mock(),user_id=account['user_id'],client_id=account['client_id'])
        self.assertEqual(observer.poll(http),('offline',None,None,None))
        self.assertEqual(http.get.call_args.kwargs['headers']['Authorization'],'Bearer app.test')
        http.post.assert_not_called()

    def test_chat_account_must_match_publisher_before_subscribing(self):
        reader = TwitchReader(None,self.state,config=self.cli,expected_user_id='88')
        http = Mock()
        http.get.return_value = response(200,{'client_id':'test-app','user_id':'42','scopes':[SCOPE]})
        with patch('ravora.twitch_chat.requests.Session',return_value=http), patch.object(reader,'_listen') as listen:
            reader.run(Event(),seconds=.2)
        self.assertEqual(reader.code,'TWITCH_CHAT_ACCOUNT_MISMATCH')
        listen.assert_not_called()

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
        reconnect = 'wss://eventsub.wss.twitch.tv?reconnect=fixture'
        first, second = Mock(), Mock()
        first.getstatus.return_value = second.getstatus.return_value = 101
        during_handoff = packet('notification', {'event': content | {'message_id': 'message-2',
                                'message': {'text': 'during reconnect'}}},
                                subscription_type=MESSAGE, message_timestamp=now)
        first.recv.side_effect = [welcome('initial'), foreign, received,
                                  packet('session_reconnect', {'session': {'reconnect_url': reconnect}}),
                                  during_handoff, '']
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
        self.assertEqual(reader.snapshot()['counts'], {'ignored_channel': 1, 'queued': 2, 'duplicate': 1,
                                                      'removed': 1, 'handoff_completed': 1, 'handoff_failed': 0})
        self.assertEqual(inbox.summary(), {'queued': 1, 'removed': 1})
        self.assertEqual([row[2] for row in inbox.display_comments(None)], ['during reconnect'])

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
