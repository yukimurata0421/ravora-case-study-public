"""Twitch channel evidence and metadata for the scheduled Ray broadcaster."""
import re
import time
from urllib.parse import urlencode

import requests

from ravora.twitch_identity import channel_login
from ravora.settings import read_env
from ravora.twitch_chat import OAUTH, TwitchError, TwitchOAuth, cli_config_path
from ravora.youtube_auth import read_private, save_private

HELIX = 'https://api.twitch.tv/helix/'
METADATA_SCOPE = 'channel:manage:broadcast'
PIN_SCOPES = ('user:write:chat', 'moderator:manage:chat_messages')


def authorize_metadata(directory, *, announce=print):
    authorize_channel(directory/'schedule-oauth.json', (METADATA_SCOPE,), announce=announce)


def authorize_pin(directory):
    authorize_channel(directory/'pin-oauth.json', PIN_SCOPES)


def authorize_channel(path, scopes, *, announce=print):
    expected_channel = channel_login()
    requested = ' '.join(scopes)
    client = read_env(cli_config_path())['CLIENTID']
    with requests.Session() as http:
        http.trust_env = False
        def post(endpoint, fields):
            return http.post(OAUTH+endpoint, data=fields, allow_redirects=False, timeout=10)
        response = post('device', {'client_id':client, 'scopes':requested})
        if response.status_code != 200:
            raise TwitchError('TWITCH_DEVICE_START_FAILED')
        device = response.json()
        code = device['user_code']
        if not re.fullmatch('[A-Z0-9-]{4,20}', code):
            raise TwitchError('TWITCH_DEVICE_CODE_INVALID')
        announce('https://www.twitch.tv/activate?' + urlencode({'public':'true','device-code':code}), flush=True)
        wait = max(5, int(device['interval']))
        deadline = time.monotonic() + int(device['expires_in'])
        while time.monotonic() + wait < deadline:
            time.sleep(wait)
            response = post('token', {'client_id':client,'scopes':requested,'device_code':device['device_code'],
                                      'grant_type':'urn:ietf:params:oauth:grant-type:device_code'})
            values = response.json()
            if response.status_code == 200:
                access = TwitchOAuth._token(values, 'access_token')
                refresh = TwitchOAuth._token(values, 'refresh_token')
                check = http.get(OAUTH+'validate', headers={'Authorization':'OAuth '+access},
                                 timeout=6, allow_redirects=False)
                identity = check.json()
                if (check.status_code != 200 or identity.get('client_id') != client
                        or identity.get('login') != expected_channel or not set(scopes).issubset(identity.get('scopes', []))
                        or not re.fullmatch(r'[0-9]{1,32}', str(identity.get('user_id','')))):
                    raise TwitchError('TWITCH_SCHEDULE_AUTHORIZATION_MISMATCH')
                path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
                save_private(path, {'client_id':client,'user_id':identity['user_id'],
                             'access_token':access,'refresh_token':refresh})
                return
            reason = values.get('message') or values.get('error')
            if reason == 'slow_down':
                wait += 5
            elif reason != 'authorization_pending':
                raise TwitchError('TWITCH_DEVICE_AUTHORIZATION_FAILED')
    raise TwitchError('TWITCH_DEVICE_AUTHORIZATION_EXPIRED')


def query(http, headers, endpoint, params):
    response = http.get(HELIX+endpoint, params=params, headers=headers, timeout=6, allow_redirects=False)
    if response.status_code != 200:
        raise TwitchError(f'TWITCH_HELIX_HTTP_{response.status_code}')
    data = response.json()
    if not isinstance(data, dict) or not isinstance(data.get('data'), list):
        raise TwitchError('TWITCH_HELIX_INVALID_DATA')
    return data


def headers(oauth):
    return {'Client-Id':oauth.client_id, 'Authorization':'Bearer '+oauth.access}


def metadata_grant(directory, http):
    return channel_grant(directory/'schedule-oauth.json', http, (METADATA_SCOPE,))


def channel_grant(path, http, scopes):
    if not path.is_file():
        raise TwitchError('TWITCH_CHANNEL_AUTHORIZATION_REQUIRED')
    grant = read_private(path)
    oauth = TwitchOAuth(cli_config_path(), path, http)
    if grant.get('client_id') != oauth.client_id or not grant.get('user_id'):
        raise TwitchError('TWITCH_SCHEDULE_AUTHORIZATION_MISMATCH')
    oauth.access = oauth._token(grant, 'access_token')
    oauth.refresh = oauth._token(grant, 'refresh_token')
    oauth.user_id = grant['user_id']
    identity = oauth.validate(scope=scopes[0])
    if identity.get('login') != channel_login() or not set(scopes).issubset(identity.get('scopes', [])):
        raise TwitchError('TWITCH_SCHEDULE_AUTHORIZATION_MISMATCH')
    return oauth


def category_id(http, auth):
    rows = query(http, auth, 'games', {'name':'Just Chatting'})['data']
    if len(rows) != 1 or rows[0].get('name') != 'Just Chatting' or not re.fullmatch(r'[0-9]+',rows[0].get('id','')):
        raise TwitchError('TWITCH_JUST_CHATTING_UNAVAILABLE')
    return rows[0]['id']


def offline(http, auth, identity, previous=None, until=0):
    seen_offline = False
    while True:
        rows = query(http, auth, 'streams', {'user_id':identity})['data']
        if not rows:
            if not previous or seen_offline:
                return
            seen_offline = True
        else:
            if not previous or len(rows) != 1 or rows[0].get('id') != previous or rows[0].get('user_id') != identity:
                raise TwitchError('TWITCH_CHANNEL_ALREADY_LIVE')
            seen_offline = False
        if time.time() + 4 >= until:
            raise TwitchError('TWITCH_SLOT_EXPIRED')
        time.sleep(4)


def channel_data(http, auth, identity):
    rows = query(http, auth, 'channels', {'broadcaster_id':identity})['data']
    if len(rows)!=1 or rows[0].get('broadcaster_id')!=identity or rows[0].get('broadcaster_login')!=channel_login():
        raise TwitchError('TWITCH_SCHEDULE_AUTHORIZATION_MISMATCH')
    return rows[0]


def prepare_channel(directory, language, *, update, previous=None, until=0):
    """Verify both identities and reconcile the one metadata update before publishing."""
    with requests.Session() as http:
        http.trust_env = False
        chat = TwitchOAuth(cli_config_path(), directory/'oauth.json', http)
        if chat.validate().get('login') != channel_login():
            raise TwitchError('TWITCH_CHAT_ACCOUNT_MISMATCH')
        meta = metadata_grant(directory,http)
        if chat.user_id != meta.user_id:
            raise TwitchError('TWITCH_SCHEDULE_AUTHORIZATION_MISMATCH')
        auth = headers(meta)
        offline(http,auth,meta.user_id,previous,until)
        category = category_id(http,auth)
        current = channel_data(http,auth,meta.user_id)
        def matches(row):
            return (row.get('game_id') == category and row.get('broadcaster_language') == language
                    and any(t.casefold() == 'vtuber' for t in row.get('tags',[])))
        if update and not matches(current):
            body = {'game_id':category,'broadcaster_language':language}
            tags = current.get('tags',[])
            if not any(t.casefold() == 'vtuber' for t in tags):
                if len(tags)>=10:
                    raise TwitchError('TWITCH_TAG_SLOTS_FULL')
                body['tags'] = [*tags,'VTuber']
            try:
                response = http.patch(HELIX+'channels',params={'broadcaster_id':meta.user_id},headers=auth,
                                      json=body,timeout=6,allow_redirects=False)
                if response.status_code not in (204,) and response.status_code < 500:
                    raise TwitchError(f'TWITCH_METADATA_HTTP_{response.status_code}')
            except requests.RequestException:
                pass  # Outcome may be unknown. Reconcile with GET, without another PATCH.
            for delay in (0, 1, 2):
                time.sleep(delay)
                current = channel_data(http,auth,meta.user_id)
                if matches(current):
                    break
            else:
                raise TwitchError('TWITCH_CHANNEL_UPDATE_UNCONFIRMED')
        return {'broadcaster':channel_login(), 'offline':True, 'metadata_authorized':True, 'matches_slot':matches(current)}
