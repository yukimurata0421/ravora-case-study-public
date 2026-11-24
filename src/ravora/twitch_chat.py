"""Twitch EventSub receiver for the broadcaster's own chat."""
from collections import Counter
from contextlib import ExitStack, closing
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import time
from urllib.parse import urlsplit

import requests
import websocket

from ravora.comments import Comment, clean, timestamp
from ravora.twitch_identity import TwitchError
from ravora.settings import SettingsError, read_env
from ravora.youtube_auth import read_private, save_private


LOG = logging.getLogger(__name__)
WS = 'wss://eventsub.wss.twitch.tv/ws'
OAUTH = 'https://id.twitch.tv/oauth2/'
SUBSCRIPTIONS = 'https://api.twitch.tv/helix/eventsub/subscriptions'
SCOPE = 'user:read:chat'
MESSAGE = 'channel.chat.message'
DELETE = 'channel.chat.message_delete'


class TwitchOAuth:
    def __init__(self, config, token_path, http):
        self.token_path = token_path
        self.http = http
        try:
            values = read_env(config)
        except (OSError, SettingsError) as exc:
            raise TwitchError('TWITCH_CHAT_CLI_CONFIG_INVALID') from exc
        self.client_id = values.get('CLIENTID', '')
        self.secret = values.get('CLIENTSECRET', '')
        self.access = values.get('ACCESSTOKEN', '')
        self.refresh = values.get('REFRESHTOKEN', '')
        if not all((self.client_id, self.access, self.refresh)):
            raise TwitchError('TWITCH_CHAT_RUN_CLI_AUTHORIZATION')
        self.cli_grant = hashlib.sha256(self.refresh.encode()).hexdigest()
        self.user_id = None
        if token_path.exists() or token_path.is_symlink():
            try:
                saved = read_private(token_path)
            except (OSError, SettingsError) as exc:
                raise TwitchError('TWITCH_CHAT_TOKEN_FILE_INVALID') from exc
            if saved.get('client_id') == self.client_id and saved.get('cli_grant') == self.cli_grant:
                self.access = self._token(saved, 'access_token')
                self.refresh = self._token(saved, 'refresh_token')
                self.user_id = saved.get('user_id')

    @staticmethod
    def _token(data, key):
        value = data.get(key)
        if not isinstance(value, str) or not 0 < len(value) <= 4096 or any(c.isspace() for c in value):
            raise TwitchError('TWITCH_CHAT_TOKEN_FORMAT_INVALID')
        return value

    def _refresh(self):
        data = {'client_id': self.client_id, 'grant_type': 'refresh_token', 'refresh_token': self.refresh}
        if self.secret:
            data['client_secret'] = self.secret
        try:
            response = self.http.post(OAUTH + 'token', data=data, timeout=5, allow_redirects=False)
        except requests.RequestException as exc:
            # Device-flow refresh tokens can be single-use. The old token may have been consumed.
            raise TwitchError('TWITCH_CHAT_REFRESH_UNCERTAIN_REAUTHORIZE') from exc
        if response.status_code != 200:
            raise TwitchError('TWITCH_CHAT_REFRESH_FAILED_REAUTHORIZE')
        try:
            tokens = response.json()
            access = self._token(tokens, 'access_token')
            refresh = self._token(tokens, 'refresh_token')
            self.token_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            save_private(self.token_path, {'client_id': self.client_id, 'cli_grant': self.cli_grant,
                                          'access_token': access, 'refresh_token': refresh, 'user_id': self.user_id})
        except (OSError, ValueError, AttributeError, TwitchError) as exc:
            raise TwitchError('TWITCH_CHAT_REFRESH_SAVE_FAILED_REAUTHORIZE') from exc
        self.access, self.refresh = access, refresh

    def validate(self, *, scope=SCOPE):
        response = self.http.get(OAUTH + 'validate', headers={'Authorization': 'OAuth ' + self.access},
                                 timeout=5, allow_redirects=False)
        if response.status_code == 401:
            self._refresh()
            response = self.http.get(OAUTH + 'validate', headers={'Authorization': 'OAuth ' + self.access},
                                     timeout=5, allow_redirects=False)
        if response.status_code != 200:
            status = response.status_code
            raise TwitchError(f'TWITCH_CHAT_VALIDATE_HTTP_{status}', retryable=status in (429, 500, 502, 503, 504))
        account = response.json()
        identity = account.get('user_id', '')
        if (account.get('client_id') != self.client_id or not isinstance(identity, str)
                or not re.fullmatch(r'[0-9]{1,32}', identity) or self.user_id not in (None, identity)):
            raise TwitchError('TWITCH_CHAT_ACCOUNT_MISMATCH')
        if scope not in (account.get('scopes') or ()):
            raise TwitchError('TWITCH_CHAT_READ_SCOPE_REQUIRED')
        self.user_id = identity
        return account

    def subscribe(self, kind, session):
        identity = self.user_id
        response = self.http.post(
            SUBSCRIPTIONS,
            headers={'Authorization': 'Bearer ' + self.access, 'Client-Id': self.client_id},
            json={'type': kind, 'version': '1', 'condition': {'broadcaster_user_id': identity, 'user_id': identity},
                  'transport': {'method': 'websocket', 'session_id': session}},
            timeout=5, allow_redirects=False,
        )
        if response.status_code != 202:
            status = response.status_code
            raise TwitchError(f'TWITCH_CHAT_SUBSCRIBE_HTTP_{status}', retryable=status in (409, 429, 500, 502, 503, 504))


def cli_config_path():
    directory = Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config')
    return directory / 'twitch-cli/.twitch-cli.env'


def _session_url(url):
    address = urlsplit(url)
    if (address.scheme, address.netloc, address.path, address.fragment) != ('wss', 'eventsub.wss.twitch.tv', '/ws', ''):
        raise TwitchError('TWITCH_CHAT_UNEXPECTED_WEBSOCKET_URL')
    return url


class TwitchReader:
    def __init__(self, inbox, token_path, *, config=None, on_removed=None, on_audience=None, is_announcement=None):
        self.inbox = inbox
        self.token_path = token_path
        self.config = config or cli_config_path()
        self.is_announcement = is_announcement
        self.on_audience = on_audience
        self.on_removed = on_removed
        self.status = 'connecting'
        self.code = None
        self.counts = Counter()

    def snapshot(self):
        return {'status': self.status, 'code': self.code, 'transport': 'twitch_eventsub_websocket',
                'counts': dict(self.counts)}

    @staticmethod
    def _message(ws):
        raw = ws.recv()
        if not raw:
            raise TwitchError('TWITCH_CHAT_SOCKET_CLOSED', retryable=True)
        if not isinstance(raw, str) or len(raw) > 65536:
            raise TwitchError('TWITCH_CHAT_INVALID_WEBSOCKET_MESSAGE')
        return json.loads(raw)

    def _open(self, url):
        ws = websocket.create_connection(_session_url(url), timeout=5, suppress_origin=True)
        try:
            welcome = self._message(ws)
            if welcome['metadata']['message_type'] != 'session_welcome':
                raise TwitchError('TWITCH_CHAT_EXPECTED_WELCOME', retryable=True)
            session = welcome['payload']['session']
            timeout = session.get('keepalive_timeout_seconds')
            if not isinstance(session.get('id'), str) or not session['id'] or type(timeout) is not int or not 10 <= timeout <= 600:
                raise TwitchError('TWITCH_CHAT_INVALID_SESSION')
            ws.settimeout(1)
            return ws, session['id'], timeout
        except BaseException:
            ws.shutdown()
            raise

    def _notification(self, metadata, payload, identity):
        try:
            event = payload['event']
            if event['broadcaster_user_id'] != identity or event.get('source_broadcaster_user_id') not in (None, identity):
                self.counts['ignored_channel'] += 1
                return
            kind = metadata['subscription_type']
            if kind == MESSAGE:
                if (self.is_announcement and event['chatter_user_id'] == identity
                        and self.is_announcement(event['message_id'], event['message']['text'])):
                    self.counts['ignored_welcome'] += 1
                    return
                comment = Comment('twitch', identity, clean(event['message_id'], 128),
                                  clean(event['chatter_user_id'], 128), clean(event['chatter_user_name'], 100),
                                  clean(event['message']['text'], 1000), timestamp(metadata['message_timestamp']))
                if not comment.text:
                    raise ValueError('EMPTY_TWITCH_COMMENT')
            elif kind == DELETE:
                removed_id = clean(event['message_id'], 128)
            else:
                return
        except (AttributeError, KeyError, TypeError, ValueError):
            self.counts['invalid'] += 1
            return
        if kind == MESSAGE:
            self.counts[self.inbox.put(comment)] += 1
        else:
            self.inbox.remove('twitch', identity, removed_id)
            self.counts['removed'] += 1
            if self.on_removed:
                self.on_removed()

    def _listen(self, oauth, stop, deadline):
        ws, session, keepalive = self._open(WS)
        try:
            for kind in (MESSAGE, DELETE):
                if stop.is_set():
                    return
                oauth.subscribe(kind, session)
            self.status, self.code = 'reading', None
            LOG.info('TWITCH_CHAT_CONNECTED')
            last = time.monotonic()
            check_at = last + 3590
            while not stop.is_set() and time.monotonic() < deadline:
                if time.monotonic() >= check_at:
                    oauth.validate()
                    check_at = time.monotonic() + 3590
                try:
                    message = self._message(ws)
                except websocket.WebSocketTimeoutException:
                    if time.monotonic() - last >= keepalive + 2:
                        raise TwitchError('TWITCH_CHAT_KEEPALIVE_TIMEOUT', retryable=True) from None
                    continue
                last = time.monotonic()
                metadata, payload = message['metadata'], message['payload']
                kind = metadata['message_type']
                if kind == 'notification' and not stop.is_set():
                    self._notification(metadata, payload, oauth.user_id)
                elif kind == 'session_reconnect':
                    replacement, _, keepalive = self._open(payload['session']['reconnect_url'])
                    ws.shutdown()
                    ws = replacement
                    last = time.monotonic()
                elif kind == 'revocation':
                    raise TwitchError('TWITCH_CHAT_SUBSCRIPTION_REVOKED_REAUTHORIZE')
        finally:
            ws.shutdown()

    def run(self, stop, *, seconds=None):
        deadline = time.monotonic() + seconds if seconds is not None else float('inf')
        delay = 2
        with ExitStack() as stack:
            http = stack.enter_context(closing(requests.Session()))
            audience = None
            http.trust_env = False
            oauth = None
            while not stop.is_set() and time.monotonic() < deadline:
                try:
                    if oauth is None:
                        oauth = TwitchOAuth(self.config, self.token_path, http)
                    oauth.validate()
                    if self.on_audience and audience is None:
                        from ravora.twitch_audience import TwitchAudience
                        audience = TwitchAudience(oauth, self.on_audience)
                        audience.worker.start()
                        stack.callback(audience.close)
                    self._listen(oauth, stop, deadline)
                    break
                except TwitchError as exc:
                    self.code = str(exc)
                    if not exc.retryable:
                        self.status = 'failed'
                        LOG.warning('%s', self.code)
                        return
                except (requests.RequestException, websocket.WebSocketException, OSError):
                    self.code = 'TWITCH_CHAT_CONNECTION_FAILED'
                except sqlite3.Error:
                    self.status, self.code = 'failed', 'TWITCH_CHAT_DB_ERROR'
                    LOG.warning('%s', self.code)
                    return
                except (AttributeError, KeyError, TypeError, ValueError):
                    self.status, self.code = 'failed', 'TWITCH_CHAT_RESPONSE_INVALID'
                    LOG.warning('%s', self.code)
                    return
                if self.status == 'reading':
                    delay = 2
                self.status = 'reconnecting'
                LOG.warning('%s; reconnecting in %ss', self.code, delay)
                stop.wait(min(delay, max(0, deadline - time.monotonic())))
                delay = min(delay * 2, 60)
        self.status = 'stopped'
