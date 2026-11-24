"""Sample Helix Get Streams independently of chat receive and encoder pacing."""
from contextlib import closing
import logging
import re
from types import SimpleNamespace
import sqlite3
import threading
import time


from ravora.comments import timestamp
from ravora.settings import SettingsError
from ravora.twitch_identity import TwitchError
from ravora.studio_observations import stream_key
from ravora.youtube_auth import read_private


def sample(http, oauth, *, app_grant=None):
    access = oauth.access
    if app_grant is not None:
        try:
            cached = read_private(app_grant)
        except (OSError, SettingsError):
            cached = {}
        token = cached.get('access_token')
        expires = cached.get('expires')
        if (cached.get('client_id') == oauth.client_id and isinstance(token, str) and token and token.isascii()
                and len(token)<=4096 and re.fullmatch(r'[A-Za-z0-9._~+/-]+=*',token) and type(expires) in (int, float) and expires > time.time()):
            access = token
    def get(bearer):
        return http.get('https://api.twitch.tv/helix/streams', params={'user_id':oauth.user_id},
                        headers={'Client-Id':oauth.client_id,'Authorization':'Bearer '+bearer},
                        allow_redirects=False, timeout=5)
    if not access:
        return 'auth_unavailable', None, None, None
    response = get(access)
    if response.status_code == 401 and oauth.access and access != oauth.access:
        response = get(oauth.access)
    if response.status_code != 200:
        return 'http_' + str(response.status_code), None, None, None
    rows = response.json()['data']
    if not isinstance(rows, list):
        raise ValueError('INVALID_HELIX_STREAMS')
    if not rows:
        return 'offline', None, None, None
    if len(rows) != 1 or rows[0]['user_id'] != oauth.user_id or rows[0]['type'] != 'live':
        raise ValueError('HELIX_STREAM_MISMATCH')
    viewers = rows[0]['viewer_count']
    if type(viewers) is not int or viewers < 0:
        raise ValueError('INVALID_VIEWER_COUNT')
    started = timestamp(rows[0]['started_at']) if 'started_at' in rows[0] else None
    return 'live', stream_key(rows[0]['id']), viewers, started


class TwitchAudience:
    def __init__(self, directory, record, *, user_id=None, client_id=None, config=None):
        self.directory = directory
        self.user_id = user_id
        self.client_id = client_id
        self.config = config
        self.record = record
        self.stop = threading.Event()
        self.worker = threading.Thread(target=self._run, name='ray-helix-viewers', daemon=True)

    def __enter__(self):
        self.worker.start()
        return self

    def __exit__(self, *_):
        self.close()

    def poll(self, http):
        from ravora.twitch_chat import OAUTH, TwitchOAuth, cli_config_path
        if self.client_id:
            # The scheduler independently verified this account via channel metadata.
            try:
                grant = read_private(self.directory/'schedule-oauth.json')
            except (OSError,SettingsError):
                grant = {}
            access = None
            if grant.get('client_id')==self.client_id and grant.get('user_id')==self.user_id:
                try:
                    access = TwitchOAuth._token(grant,'access_token')
                except TwitchError:
                    pass  # The independent app grant can still observe this channel.
            account = SimpleNamespace(user_id=self.user_id,client_id=self.client_id,access=access)
        else:
            # Manual studios retain their configured Twitch account. Never race the
            # EventSub reader for its single-use refresh token.
            account = TwitchOAuth(self.config or cli_config_path(),self.directory/'oauth.json',http)
            if not account.user_id:
                response = http.get(OAUTH+'validate',headers={'Authorization':'OAuth '+account.access},
                                    allow_redirects=False,timeout=5)
                if response.status_code!=200:
                    return 'http_'+str(response.status_code),None,None,None
                identity = response.json()
                if identity.get('client_id')!=account.client_id:
                    raise ValueError('TWITCH_AUDIENCE_ACCOUNT_MISMATCH')
                account.user_id = identity.get('user_id')
        if not isinstance(account.user_id,str) or not re.fullmatch(r'[0-9]{1,32}',account.user_id):
            raise ValueError('TWITCH_AUDIENCE_ACCOUNT_MISMATCH')
        return sample(http,account,app_grant=self.directory/'market-app.json')

    def _run(self):
        import requests
        with closing(requests.Session()) as http:
            http.trust_env = False
            while not self.stop.is_set():
                try:
                    result = self.poll(http)
                except (requests.RequestException, OSError, SettingsError, TwitchError, KeyError, AttributeError, TypeError, ValueError):
                    result = 'unavailable', None, None, None
                if self.stop.is_set():
                    break
                try:
                    self.record(time.time(), *result)
                except sqlite3.Error:
                    logging.getLogger(__name__).warning('STUDIO_AUDIENCE_WRITE_FAILED')
                self.stop.wait(60)

    def close(self):
        self.stop.set()
        self.worker.join(timeout=6)
