from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from importlib.util import find_spec
from unittest.mock import Mock, patch

if find_spec('requests') is None or find_spec('websocket') is None:
    raise unittest.SkipTest('youtube-live extra required')


import websocket

from ravora.comments import CommentInbox
from ravora.conversation import MemoryStore
from ravora.twitch_chat import DELETE, MESSAGE, WS, TwitchError, TwitchReader
from ravora.twitch_irc import ENDPOINT, parse_line
from test_twitch_chat import packet, welcome
from ravora.youtube_auth import save_private


class TwitchIRCTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.inbox = CommentInbox(MemoryStore(self.root/'studio.db'))
        self.removed = Mock()
        self.reader = TwitchReader(self.inbox, self.root/'primary', expected_user_id='42', on_removed=self.removed,
                                   irc_token_path=self.root/'irc.json')
        self.irc = self.reader.irc
        self.socket = Mock()
        self.socket.getstatus.return_value = 101
        self.oauth = Mock(access='offline-fixture', user_id='42')
        self.oauth.validate.return_value = {'login':'ray_test'}
        self.join = (':tmi.twitch.tv CAP * ACK :twitch.tv/tags twitch.tv/commands\r\n'
                     ':tmi.twitch.tv 001 ray_test :Welcome\r\n'
                     '@room-id=42 :tmi.twitch.tv ROOMSTATE #ray_test\r\n')
        self.tags = {'tmi-sent-ts':str(int(time.time()*1000)), 'room-id':'42','user-id':'7',
                     'display-name':r'Viewer\sOne','id':'m1'}

    def run_frames(self, frames):
        def receive():
            try:
                return next(packets)
            except StopIteration:
                self.irc.stop.set()
                return 'PING :local'
        packets = iter(frames)
        self.socket.recv.side_effect = receive
        with patch('websocket.create_connection',return_value=self.socket):
            self.irc._listen(self.oauth, 'ray_test','42', time.monotonic()+5)

    def message(self, text='hello', **extra):
        tags = self.tags | extra
        return '@'+';'.join(k+'='+v for k,v in tags.items())+' :viewer!viewer@viewer.tmi.twitch.tv PRIVMSG #ray_test :'+text

    def test_wire_scope_moderation_and_cross_route_reordering(self):
        before_join = self.message(id='ignored')
        deletion = '@room-id=;target-msg-id=m1 :tmi.twitch.tv CLEARMSG #ray_test :hello'
        self.run_frames([before_join, self.join, 'PING :tmi.twitch.tv', self.message(),
                         self.message('shared', **{'source-room-id':'77','id':'other'}),
                         self.message().replace('#ray_test','#foreign'), deletion, self.message(), self.message(id='m2')])
        self.reader._notification({'subscription_type':DELETE},
                                  {'event':{'broadcaster_user_id':'42','message_id':'m1'}},'42')
        self.assertEqual([row[1:] for row in self.inbox.display_comments(None)], [('Viewer One','hello')])
        self.assertEqual(self.inbox.summary(), {'removed':1, 'queued':1})
        self.removed.assert_called_once()
        self.assertIn('PONG :tmi.twitch.tv\r\n',[c.args[0] for c in self.socket.send.call_args_list])
        self.assertFalse(any(c.args[0].startswith('PRIVMSG') for c in self.socket.send.call_args_list))
        self.socket.shutdown.assert_called_once()

    def test_delete_before_first_delivery_and_requires_verified_room(self):
        self.run_frames([self.join, '@target-msg-id=late :tmi.twitch.tv CLEARMSG #ray_test :gone',
                         self.message('gone', id='late')])
        self.assertFalse(self.inbox.display_comments(None))
        self.assertEqual(self.reader.counts['queued'], 0)
        self.assertEqual(parse_line(r'@name=A\:B\sC :server CMD #c :a :b'),
                         ({'name':'A;B C'},'server',['CMD','#c'],'a :b'))

    def test_action_and_empty_display_name(self):
        self.run_frames([self.join,self.message('\x01ACTION waves\x01', **{'display-name':''})])
        self.assertEqual([row[1:] for row in self.inbox.display_comments(None)],[('viewer','waves')])

    def test_no_credentials_on_redirect_or_mismatched_account(self):
        self.socket.getstatus.return_value = 302
        with self.assertRaisesRegex(TwitchError,'EXPECTED_UPGRADE'):
            self.run_frames([])
        self.socket.send.assert_not_called()
        save_private(self.irc.path, {'client_id':'test'})
        self.oauth.user_id = '99'
        with (patch('ravora.twitch_irc.TwitchOAuth',return_value=self.oauth),
              patch('websocket.create_connection') as connect):
            self.irc._run('42',time.monotonic()+0.02)
        connect.assert_not_called()

    def test_wrong_room_stops_before_chat_is_accepted(self):
        with self.assertRaisesRegex(TwitchError,'ACCOUNT_MISMATCH'):
            self.run_frames([self.join.replace('room-id=42','room-id=77'), self.message()])
        self.assertEqual(self.irc.messages,0)
        self.assertFalse(self.inbox.display_comments(None))

    def test_eventsub_specific_refusal_keeps_verified_irc_until_stop(self):
        stop, ready = threading.Event(), threading.Event()
        def irc_loop(identity, deadline):
            self.assertEqual(identity,'42')
            self.irc.status = 'reading'
            ready.set()
            self.irc.stop.wait(5)
        def reject(*args):
            self.assertTrue(ready.wait(2))
            raise TwitchError('TWITCH_CHAT_SUBSCRIBE_HTTP_403')
        def check_active(timeout):
            self.assertEqual(self.reader.primary_status,'failed')
            self.assertEqual(self.reader.snapshot()['transport'],'twitch_irc')
            self.assertIsNone(self.reader.snapshot()['code'])
            stop.set()
        with (patch.object(self.irc,'_run',side_effect=irc_loop),
              patch('ravora.twitch_chat.TwitchOAuth',return_value=self.oauth),
              patch.object(self.reader,'_listen',side_effect=reject), patch.object(stop,'wait',side_effect=check_active)):
            self.reader.run(stop,seconds=4)
        self.assertFalse(self.irc.thread.is_alive())

    def test_irc_accepts_while_eventsub_is_down_and_primary_recovers(self):
        save_private(self.irc.path, {'client_id':'test'})
        stop, got_irc = threading.Event(), threading.Event()
        primary, first_attempt = Mock(), threading.Event()
        primary.getstatus.return_value = 101
        stamp = datetime.now(timezone.utc).isoformat()
        event = {'broadcaster_user_id':'42','chatter_user_id':'7','chatter_user_name':'Viewer One',
                 'message_id':'m1','message':{'text':'hello'}}
        primary_frames = iter([welcome('fresh'), packet('notification',{'event':event},
                              subscription_type=MESSAGE,message_timestamp=stamp)])
        def primary_recv():
            frame = next(primary_frames, None)
            if frame: return frame
            self.assertEqual(self.reader.snapshot()['transport'],'twitch_eventsub_websocket')
            stop.set()
            return packet('session_keepalive',{})
        primary.recv.side_effect = primary_recv
        irc_frames = iter([self.join, self.message()])
        def irc_recv():
            frame = next(irc_frames,None)
            if frame: return frame
            if not got_irc.is_set():
                if not first_attempt.wait(2): raise AssertionError('missing EventSub attempt')
                self.assertEqual(self.reader.snapshot()['transport'],'twitch_irc')
                self.assertEqual(len(self.inbox.display_comments(None)),1)
                got_irc.set()
            self.irc.stop.wait(.01)
            raise websocket.WebSocketTimeoutException()
        self.socket.recv.side_effect = irc_recv
        def connect(url, **options):
            self.assertEqual(options['redirect_limit'],0)
            if url==ENDPOINT: return self.socket
            self.assertEqual(url,WS)
            if not first_attempt.is_set():
                first_attempt.set()
                raise OSError('offline fixture')
            self.assertTrue(got_irc.wait(1))
            return primary
        with (patch('ravora.twitch_irc.TwitchOAuth',return_value=self.oauth),
              patch('ravora.twitch_chat.TwitchOAuth',return_value=self.oauth),
              patch('websocket.create_connection',side_effect=connect)):
            self.reader.run(stop, seconds=5)
        self.assertTrue(got_irc.is_set())
        self.assertEqual(self.reader.counts['queued'],1)
        self.assertEqual(self.reader.counts['duplicate'],1)
        self.assertEqual(self.reader.status,'stopped')
        self.assertFalse(self.irc.thread.is_alive())
