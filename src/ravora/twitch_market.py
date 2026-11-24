"""Sample tagged Just Chatting channels and Ray/vedal987 independently."""
import re
import time
import unicodedata

import requests

from ravora.settings import read_env
from ravora.twitch_chat import OAUTH, TwitchError, cli_config_path
from ravora.twitch_identity import channel_login
from ravora.twitch_schedule_api import category_id, query
from ravora.youtube_auth import read_private, save_private

RIVAL = 'vedal987'
TAG_KEYS = {'vtuber','envtuber','jpvtuber','vtuberen','vtuberjp','新人vtuber','個人vtuber','個人勢vtuber',
            'バーチャルユーチューバー','virtualyoutuber','virtualstreamer','aivtuber','pngtuber'}
MAX_PAGES = 100


def tagged(tags):
    return any(''.join(c for c in unicodedata.normalize('NFKC',t).casefold() if c.isalnum()) in TAG_KEYS for t in tags)


def app_headers(directory, http, *, renew=False):
    credentials = read_env(cli_config_path())
    client = credentials['CLIENTID']
    path = directory/'market-app.json'
    cached = read_private(path) if path.exists() else {}
    token = cached.get('access_token')
    if renew or not token or cached.get('client_id') != client or cached.get('expires',0) <= time.time()+120:
        r = http.post(OAUTH+'token', data={'client_id':client,'client_secret':credentials['CLIENTSECRET'],
                      'grant_type':'client_credentials'},timeout=6,allow_redirects=False)
        if r.status_code!=200:
            raise TwitchError('TWITCH_MARKET_APP_AUTH_FAILED')
        data=r.json()
        token=data['access_token']
        if not isinstance(token,str) or not token or not isinstance(data['expires_in'],int):
            raise TwitchError('TWITCH_MARKET_APP_AUTH_INVALID')
        directory.mkdir(parents=True,exist_ok=True,mode=0o700)
        save_private(path,{'client_id':client,'access_token':token,'expires':time.time()+data['expires_in']})
    return {'Authorization':'Bearer '+token,'Client-Id':client}


def peers(http, auth):
    own_channel = channel_login()
    rows = query(http,auth,'streams',[('user_login',own_channel),('user_login',RIVAL)])['data']
    live = {}
    for row in rows:
        who = row['user_login'].casefold()
        if who not in (own_channel,RIVAL) or who in live or row.get('type')!='live' or not re.fullmatch('[0-9]+',row['id']):
            raise TwitchError('TWITCH_PEER_RESPONSE_INVALID')
        live[who]=row['id']
    return time.time(), live.get(own_channel), live.get(RIVAL)


def market(http, auth, category, language, *, max_pages=MAX_PAGES):
    own_channel = channel_login()
    params={'game_id':category,'language':language,'first':100}
    seen, cursors, counts = set(), set(), []
    end=time.monotonic()+95
    for page in range(1, max_pages+1):
        payload=query(http,auth,'streams',params)
        for row in payload['data']:
            if row.get('type')!='live' or row.get('game_id')!=category or row.get('language')!=language:
                continue
            channel=row['user_id']
            if channel in seen or row['user_login'].casefold()==own_channel:
                continue
            seen.add(channel)
            tags=row.get('tags')
            if not isinstance(tags,list) or any(not isinstance(t,str) for t in tags):
                raise TwitchError('TWITCH_MARKET_TAGS_UNAVAILABLE')
            if tagged(tags):
                viewers=row['viewer_count']
                if type(viewers) is not int or viewers<0:
                    raise TwitchError('TWITCH_MARKET_COUNT_INVALID')
                counts.append(viewers)
        cursor=payload.get('pagination',{}).get('cursor')
        if not cursor:
            return 'complete',page,len(counts),sum(counts),sum(sorted(counts,reverse=True)[:5])
        if cursor in cursors or time.monotonic()>=end:
            break
        cursors.add(cursor)
        params['after']=cursor
    return 'partial',page,len(counts),sum(counts),sum(sorted(counts,reverse=True)[:5])


def sample(directory, slot):
    market_values=('error',0,None,None,None)
    peer_values=(None,None,None)
    errors=(requests.RequestException,TwitchError,ValueError,KeyError,TypeError,OSError)
    with requests.Session() as http:
        http.trust_env=False
        for attempt in range(2):
            unauthorized = False
            try:
                auth = app_headers(directory, http, renew=bool(attempt))
            except errors:
                break
            try:
                peer_values = peers(http, auth)
            except errors as exc:
                unauthorized = str(exc) == 'TWITCH_HELIX_HTTP_401'
            if not unauthorized:
                try:
                    market_values = market(http, auth, category_id(http, auth), slot.language)
                except errors as exc:
                    unauthorized = str(exc) == 'TWITCH_HELIX_HTTP_401'
            if not unauthorized:
                break
    return market_values,peer_values
