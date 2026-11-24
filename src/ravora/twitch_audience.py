"""Sample Helix Get Streams independently of chat receive and encoder pacing."""
from contextlib import closing
import logging
import sqlite3
import threading
import time


from ravora.comments import timestamp
from ravora.studio_observations import stream_key


def sample(http, oauth):
    response = http.get('https://api.twitch.tv/helix/streams', params={'user_id':oauth.user_id},
                        headers={'Client-Id':oauth.client_id,'Authorization':'Bearer '+oauth.access},
                        allow_redirects=False, timeout=5)
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
    def __init__(self, oauth, record):
        self.oauth = oauth
        self.record = record
        self.stop = threading.Event()
        self.worker = threading.Thread(target=self._run, name='ray-helix-viewers', daemon=True)

    def _run(self):
        import requests
        with closing(requests.Session()) as http:
            http.trust_env = False
            while not self.stop.is_set():
                try:
                    result = sample(http, self.oauth)
                except (requests.RequestException, KeyError, AttributeError, TypeError, ValueError):
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
