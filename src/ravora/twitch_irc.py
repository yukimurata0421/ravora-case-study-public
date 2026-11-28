"""Read-only, independently authorized Twitch IRC input for the live chat inbox."""
from datetime import datetime, timezone
import logging
import re
import sqlite3
import threading
import time

import requests
import websocket

from ravora.twitch_chat import DELETE, MESSAGE, TwitchError, TwitchOAuth
from ravora.youtube_auth import read_private

ENDPOINT = 'wss://irc-ws.chat.twitch.tv:443'
CAPS = {'twitch.tv/commands', 'twitch.tv/tags'}
LOG = logging.getLogger(__name__)
ESCAPES = {'s': ' ', ':': ';', 'r': '\r', 'n': '\n', '\\': '\\'}


def parse_line(line):
    tags = {}
    prefix = ''
    if line.startswith('@'):
        raw, _, line = line[1:].partition(' ')
        for tag in raw.split(';'):
            key, _, value = tag.partition('=')
            tags[key] = re.sub(r'\\(.)', lambda m: ESCAPES.get(m[1], m[1]), value)
    if line.startswith(':'):
        prefix, _, line = line[1:].partition(' ')
    head, _, body = line.partition(' :')
    return tags, prefix, head.split(), body


class TwitchIRC:
    def __init__(self, path, config, notify):
        self.path, self.config, self.notify = path, config, notify
        self.status, self.code = 'standby', None
        self.stop = threading.Event()
        self.thread = None
        self.messages = self.deletions = 0

    def start(self, identity, deadline):
        if self.thread is None:
            self.thread = threading.Thread(target=self._run, args=(identity, deadline), name='twitch-irc', daemon=True)
            self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join()

    def _run(self, identity, deadline):
        delay = 2
        blocked_grant = None
        with requests.Session() as http:
            http.trust_env = False
            while not self.stop.is_set() and time.monotonic() < deadline:
                try:
                    if not self.path.is_file():
                        self.status, self.code = 'authorization_required', 'TWITCH_IRC_AUTHORIZATION_REQUIRED'
                        self.stop.wait(min(3, max(0, deadline - time.monotonic())))
                        continue
                    stat = self.path.stat()
                    grant = (stat.st_ino, stat.st_mtime_ns)
                    if grant == blocked_grant:
                        self.stop.wait(min(3, max(0, deadline - time.monotonic())))
                        continue
                    oauth = TwitchOAuth(self.config, self.path, http, channel_grant=read_private(self.path))
                    account = oauth.validate(scope='chat:read')
                    login = account.get('login')
                    if oauth.user_id != identity or not isinstance(login, str) or not re.fullmatch('[a-z0-9_]{1,25}',login):
                        raise TwitchError('TWITCH_IRC_ACCOUNT_MISMATCH')
                    self.status, self.code = 'connecting', None
                    self._listen(oauth, login, identity, deadline)
                    break
                except TwitchError as exc:
                    status = 'reconnecting' if exc.retryable else 'authorization_required'
                    code = str(exc)
                    if not exc.retryable:
                        blocked_grant = grant
                except (requests.RequestException, websocket.WebSocketException, OSError):
                    status, code = 'reconnecting', 'TWITCH_IRC_CONNECTION_FAILED'
                except (ValueError, KeyError, TypeError, AttributeError):
                    status, code = 'failed', 'TWITCH_IRC_RESPONSE_INVALID'
                except sqlite3.Error:
                    self.status, self.code = 'failed', 'TWITCH_CHAT_DB_ERROR'
                    return
                if self.status == 'reading':
                    delay = 2
                self.status, self.code = status, code
                self.stop.wait(min(delay, max(0, deadline - time.monotonic())))
                delay = min(delay * 2, 60)
        self.status = 'stopped'

    def _listen(self, oauth, login, identity, deadline):
        ws = websocket.create_connection(ENDPOINT, timeout=5, suppress_origin=True, redirect_limit=0)
        try:
            if ws.getstatus() != 101:
                raise TwitchError('TWITCH_IRC_EXPECTED_UPGRADE', retryable=True)
            ws.settimeout(1)
            ws.send('CAP REQ :twitch.tv/commands twitch.tv/tags\r\n')
            ws.send('PASS oauth:' + oauth.access + '\r\n')
            ws.send('NICK ' + login + '\r\n')
            channel = '#' + login
            caps, authenticated, room = set(), False, False
            last = time.monotonic()
            join_by, validate_at = last + 15, last + 3500
            ping_at = None
            while not self.stop.is_set() and time.monotonic() < deadline:
                now = time.monotonic()
                if self.status != 'reading' and now >= join_by:
                    raise TwitchError('TWITCH_IRC_JOIN_TIMEOUT', retryable=True)
                if ping_at is not None and now - ping_at > 15:
                    raise TwitchError('TWITCH_IRC_KEEPALIVE_TIMEOUT', retryable=True)
                if ping_at is None and now - last > 60:
                    ws.send('PING :ray\r\n')
                    ping_at = now
                if now >= validate_at:
                    oauth.validate(scope='chat:read')
                    validate_at = now + 3500
                try:
                    raw = ws.recv()
                except websocket.WebSocketTimeoutException:
                    continue
                if not isinstance(raw, str) or not raw or len(raw) > 65536:
                    raise TwitchError('TWITCH_IRC_DISCONNECTED', retryable=True)
                last = time.monotonic()
                for line in raw.split('\r\n'):
                    tags, prefix, words, body = parse_line(line)
                    if not words or self.stop.is_set():
                        continue
                    cmd = words[0]
                    if cmd == 'PING':
                        ws.send('PONG :' + (body or 'tmi.twitch.tv') + '\r\n')
                    elif cmd == 'PONG':
                        ping_at = None
                    elif cmd == 'RECONNECT':
                        raise TwitchError('TWITCH_IRC_RECONNECT', retryable=True)
                    elif cmd == '001' and words[1:] == [login] and not authenticated:
                        authenticated = True
                        ws.send('JOIN ' + channel + '\r\n')
                    elif cmd == 'CAP' and words[-1] == 'ACK':
                        caps.update(body.split())
                    elif cmd == 'NOTICE' and not authenticated:
                        raise TwitchError('TWITCH_IRC_LOGIN_REJECTED')
                    elif cmd == 'ROOMSTATE' and words[1:] == [channel]:
                        if tags.get('room-id') != identity:
                            raise TwitchError('TWITCH_IRC_ACCOUNT_MISMATCH')
                        room = True
                    elif self.status == 'reading' and words[1:] == [channel]:
                        self._event(cmd, tags, body, identity, prefix)
                    if self.status != 'reading' and authenticated and room and CAPS <= caps:
                        self.status, self.code = 'reading', None
                        LOG.info('TWITCH_IRC_CONNECTED')
        finally:
            ws.shutdown()

    def _event(self, cmd, tags, body, identity, prefix=''):
        if (tags.get('room-id') not in (None, '', identity)
                or tags.get('source-room-id') not in (None, '', identity)):
            return
        if cmd == 'CLEARMSG' and tags.get('target-msg-id'):
            self.notify({'subscription_type': DELETE}, {'event': {
                'broadcaster_user_id': identity, 'message_id': tags['target-msg-id']}}, identity)
            self.deletions += 1
        elif cmd == 'PRIVMSG' and tags.get('room-id') == identity:
            try:
                milliseconds = tags['tmi-sent-ts']
                if not re.fullmatch(r'[0-9]{1,13}', milliseconds):
                    return
                sent = datetime.fromtimestamp(int(milliseconds) / 1000, timezone.utc)
                if body.startswith('\x01ACTION ') and body.endswith('\x01'):
                    body = body[8:-1]
                nickname = prefix.partition('!')[0]
                name = tags.get('display-name') or (nickname if re.fullmatch('[a-zA-Z0-9_]{1,25}',nickname) else '')
                event = {'broadcaster_user_id': identity, 'message_id': tags['id'],
                         'chatter_user_id': tags['user-id'], 'chatter_user_name': name,
                         'message': {'text': body}}
            except (KeyError, ValueError, OverflowError, OSError):
                return
            if tags.get('reply-parent-msg-id'):
                event['reply'] = {'parent_message_id': tags['reply-parent-msg-id']}
            self.notify({'subscription_type': MESSAGE, 'message_timestamp': sent.isoformat()},
                        {'event': event}, identity)
            self.messages += 1
