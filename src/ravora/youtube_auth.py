"""Channel-bound OAuth for the YouTube publisher. Credentials stay in this process."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import tempfile
import threading

from ravora.settings import SettingsError

SCOPE = 'https://www.googleapis.com/auth/youtube.force-ssl'
TOKEN_URL = 'https://oauth2.googleapis.com/token'
API = 'https://www.googleapis.com/youtube/v3/'


class YouTubeError(RuntimeError):
    def __init__(self, code, status=0):
        super().__init__(code)
        self.status = status


def read_private(path):
    if not path.exists():
        raise SettingsError('YOUTUBE_MISSING_' + path.stem.upper())
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
        raise SettingsError('YOUTUBE_FILE_REQUIRES_0600')
    try:
        value = json.loads(path.read_text())
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (ValueError, UnicodeError):
        raise SettingsError('INVALID_YOUTUBE_' + path.stem.upper()) from None


def save_private(path, value):
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as tmp:
        try:
            json.dump(value, tmp)
            tmp.flush()
            os.fsync(tmp.fileno())
            os.replace(tmp.name, path)
            fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        finally:
            Path(tmp.name).unlink(missing_ok=True)


@contextmanager
def publisher_lock(directory):
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (directory / 'publisher.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise YouTubeError('YOUTUBE_PUBLISHER_ALREADY_RUNNING') from None
        yield


def client_config(directory):
    config = read_private(directory / 'client.json').get('installed', {})
    if not (isinstance(config, dict) and config.get('client_id') and config.get('client_secret')
            and config.get('auth_uri') in ('https://accounts.google.com/o/oauth2/auth',
                                           'https://accounts.google.com/o/oauth2/v2/auth')
            and config.get('token_uri') == TOKEN_URL):
        raise SettingsError('YOUTUBE_DESKTOP_CLIENT_JSON_REQUIRED')
    return config


class ChannelAPI:
    def __init__(self, credentials, *, token_path=None, channel_id=None):
        import requests
        from google.auth.transport.requests import Request
        self.credentials = credentials
        self.token_path = token_path
        self.channel_id = channel_id
        self.http = requests.Session()
        self.http.trust_env = False
        self.refresh_request = Request(session=self.http)
        self.lock = threading.Lock()

    def access_token(self, *, force_refresh=False):
        with self.lock:
            if force_refresh or not self.credentials.valid:
                previous = self.credentials.refresh_token
                try:
                    self.credentials.refresh(self.refresh_request)
                except Exception:
                    raise YouTubeError('YOUTUBE_OAUTH_REFRESH_FAILED_REAUTHORIZE') from None
                if self.token_path and self.credentials.refresh_token != previous:
                    save_private(self.token_path, {'client_id':self.credentials.client_id,
                                 'channel_id':self.channel_id, 'refresh_token':self.credentials.refresh_token})
            return self.credentials.token

    def request(self, method, resource, params, body=None):
        import requests
        token = self.access_token()
        try:
            with self.http.request(method, API + resource, params=params, json=body,
                                   headers={'Authorization': 'Bearer ' + token},
                                   timeout=(4, 8), allow_redirects=False) as response:
                if not 200 <= response.status_code < 300:
                    reason = ''
                    try:
                        code = response.json()['error']['errors'][0]['reason']
                        if code in ('insufficientPermissions', 'liveStreamingNotEnabled', 'livePermissionBlocked',
                                    'quotaExceeded', 'invalidTransition', 'errorStreamInactive',
                                    'redundantTransition', 'liveBroadcastBindingNotAllowed', 'forbidden',
                                    'invalidScheduledStartTime', 'userBroadcastsExceedLimit'):
                            reason = '_' + code
                    except (ValueError, KeyError, IndexError, TypeError):
                        pass
                    raise YouTubeError('YOUTUBE_API_HTTP_' + str(response.status_code) + reason, response.status_code)
                result = response.json()
                if not isinstance(result, dict):
                    raise ValueError()
                return result
        except (requests.RequestException, ValueError):
            raise YouTubeError('YOUTUBE_API_RESPONSE_UNKNOWN') from None

    def channel(self):
        items = self.request('GET', 'channels', {'mine':'true', 'part':'id,snippet',
                                                  'fields':'items(id,snippet/title)'})['items']
        if len(items) != 1 or not items[0].get('id'):
            raise YouTubeError('YOUTUBE_SELECT_ONE_CHANNEL')
        return items[0]

    @classmethod
    def connect(cls, directory):
        from google.oauth2.credentials import Credentials
        client = client_config(directory)
        saved = read_private(directory / 'token.json')
        if saved.get('client_id') != client['client_id'] or not saved.get('refresh_token'):
            raise SettingsError('YOUTUBE_CLIENT_CHANGED_REAUTHORIZE')
        credentials = Credentials(None, refresh_token=saved['refresh_token'], token_uri=TOKEN_URL,
                                  client_id=client['client_id'], client_secret=client['client_secret'], scopes=[SCOPE])
        api = cls(credentials, token_path=directory/'token.json', channel_id=saved.get('channel_id'))
        try:
            channel_id = api.channel()['id']
            if channel_id != saved.get('channel_id'):
                raise YouTubeError('YOUTUBE_AUTHORIZED_CHANNEL_CHANGED')
            return api, channel_id
        except BaseException:
            api.http.close()
            raise


def authorize(directory, *, port=8791):
    from google_auth_oauthlib.flow import InstalledAppFlow
    with publisher_lock(directory):
        client = client_config(directory)
        flow = InstalledAppFlow.from_client_config({'installed': client}, scopes=[SCOPE],
                                                   autogenerate_code_verifier=True)
        try:
            credentials = flow.run_local_server(host='127.0.0.1', port=port, open_browser=False,
                authorization_prompt_message='Windows のブラウザで開いてください。\n{url}',
                success_message='Authorization received. Return to the Ray console.',
                prompt='consent', timeout_seconds=300)
        except Exception:
            raise YouTubeError('YOUTUBE_OAUTH_AUTHORIZATION_FAILED') from None
        finally:
            flow.oauth2session.close()
        if not credentials.refresh_token:
            raise YouTubeError('YOUTUBE_OAUTH_REFRESH_TOKEN_MISSING')
        api = ChannelAPI(credentials)
        try:
            channel = api.channel()
            saved = directory / 'broadcast.json'
            if saved.exists() and read_private(saved).get('channel') != channel['id']:
                raise YouTubeError('YOUTUBE_PUBLISHER_CHANNEL_MISMATCH')
            save_private(directory / 'token.json', {'client_id': client['client_id'],
                         'channel_id': channel['id'], 'refresh_token': credentials.refresh_token})
            print('YouTube channel:', channel['snippet']['title'], channel['id'])
        finally:
            api.http.close()
