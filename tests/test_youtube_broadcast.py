from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
from importlib.util import find_spec
from pathlib import Path
import base64
import hashlib
import json
import tempfile
import threading
import unittest
import urllib.parse
import urllib.request
from unittest.mock import Mock, patch

from ravora.youtube_auth import (ChannelAPI, SCOPE, YouTubeError, authorize, publisher_lock,
                                 read_private, save_private)
if find_spec('av') and find_spec('numpy') and find_spec('PIL'):
    from ravora.youtube_broadcast import BROADCAST, STREAM, Broadcast
else:
    raise unittest.SkipTest('youtube-live extra required')


class FakeChannel:
    def __init__(self):
        self.rows = {BROADCAST:{}, STREAM:{}}
        self.writes = Counter()
        self.lose_response = set()
        self.hidden = False
        self.receiving = False
        self.serve_chat = True
        self.http = Mock()

    def access_token(self):
        return 'mock-channel-access'

    def request(self, method, resource, params, body=None):
        if resource == 'videos':
            return {'items':[{'id':params['id'], 'liveStreamingDetails':{'activeLiveChatId':'test-chat'}}]}
        if method == 'GET':
            rows = self.rows[resource]
            if 'id' in params:
                rows = [rows[params['id']]] if params['id'] in rows else []
            else:
                rows = [] if self.hidden else rows.values()
            out = deepcopy(list(rows))
            if params.get('broadcastStatus'):
                phases = ('live', 'testing', 'liveStarting') if params['broadcastStatus'] == 'active' else ('created', 'ready')
                out = [row for row in out if row.get('status', {}).get('lifeCycleStatus', 'ready') in phases]
            if resource == STREAM:
                for row in out:
                    row['status'] = {'streamStatus':'active' if self.receiving else 'inactive'}
            return {'items':out}
        self.writes[resource] += 1
        if resource in (STREAM, BROADCAST):
            ident = resource[:1] + str(self.writes[resource]).zfill(10)
            row = deepcopy(body)
            row.update(id=ident)
            row['snippet']['channelId'] = 'owner'
            if resource == STREAM:
                row['cdn']['ingestionInfo'] = {'rtmpsIngestionAddress':'rtmps://a.rtmps.youtube.com/live2',
                                               'streamName':'fake-test-key'}
                row['status'] = {'streamStatus':'inactive'}
            else:
                assert datetime.fromisoformat(row['snippet']['scheduledStartTime']) > datetime.now(timezone.utc)
                row['status']['lifeCycleStatus'] = 'created'
            self.rows[resource][ident] = row
        else:
            row = self.rows[BROADCAST][params['id']]
            if resource.endswith('/bind'):
                row['contentDetails']['boundStreamId'] = params['streamId']
                row['status']['lifeCycleStatus'] = 'ready'
            elif resource.endswith('/transition'):
                phase = params['broadcastStatus']
                if phase == 'live':
                    assert self.receiving
                    if self.serve_chat:
                        row['snippet']['liveChatId'] = 'test-chat'
                row['status']['lifeCycleStatus'] = phase
            else:
                raise AssertionError(resource)
        if resource in self.lose_response:
            self.lose_response.remove(resource)
            raise YouTubeError('YOUTUBE_API_RESPONSE_UNKNOWN')
        return deepcopy(row)


class BroadcastTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        self.api = FakeChannel()

    def manager(self, **changes):
        args = {'title':'Ray Live', 'privacy':'unlisted', 'stop':threading.Event(), **changes}
        return Broadcast(self.api, self.directory, 'owner', **args)

    def test_resume_and_two_consecutive_events_only_one_ingest_stream(self):
        b = self.manager()
        target = b.prepare()
        self.assertTrue(target.startswith('rtmps:'))
        self.assertNotIn('fake-test-key', b.path.read_text())
        first = b.state['broadcast']
        b = self.manager()
        self.assertEqual(b.prepare(), target)
        self.assertEqual(b.state['broadcast'], first)
        self.api.receiving = True
        self.api.serve_chat = False
        self.assertEqual(b.chat_id(b.wait_live(interval=.001)), 'test-chat')
        b.stop.set()
        b.finish(interval=.001)
        self.api.receiving = False
        again = self.manager()
        again.prepare()
        self.assertNotEqual(again.state['broadcast'], first)
        self.assertEqual((self.api.writes[STREAM], self.api.writes[BROADCAST]), (1, 2))

    def test_lost_insert_bind_transition_are_reconciled_without_resending(self):
        b = self.manager()
        self.api.lose_response.update([STREAM, BROADCAST, BROADCAST+'/bind', BROADCAST+'/transition'])
        self.api.hidden = True
        with self.assertRaisesRegex(YouTubeError, 'RECONCILE_PENDING_CREATE_STREAM'):
            b.prepare()
        b = self.manager()
        with self.assertRaisesRegex(YouTubeError, 'RECONCILE_PENDING_CREATE_STREAM'):
            b.prepare()
        self.api.hidden = False
        b.prepare()
        self.api.receiving = True
        self.assertEqual(b.chat_id(b.wait_live(interval=.001)), 'test-chat')
        self.assertTrue(all(count == 1 for count in self.api.writes.values()))
        self.api.lose_response.add(BROADCAST+'/transition')
        b.finish(interval=.001)
        self.assertFalse(read_private(b.path)['pending'])

    def test_no_transition_without_ingest_and_no_adoption_of_other_streams(self):
        b = self.manager()
        b.prepare()
        with self.assertRaisesRegex(YouTubeError, 'START_NOT_CONFIRMED'):
            b.wait_live(timeout=.01, interval=.002)
        self.assertFalse(self.api.writes[BROADCAST+'/transition'])
        with self.assertRaisesRegex(YouTubeError, 'PRIVACY_MISMATCH'):
            self.manager(privacy='public').prepare()
        self.api.rows[BROADCAST]['other'] = {'id':'other', 'contentDetails':{'boundStreamId':b.state['stream']}}
        with self.assertRaisesRegex(YouTubeError, 'ANOTHER_EVENT'):
            self.manager().prepare()
        with publisher_lock(self.directory), self.assertRaisesRegex(YouTubeError, 'ALREADY_RUNNING'), publisher_lock(self.directory):
            self.fail('second publisher accepted')

    @unittest.skipUnless(find_spec('google_auth_oauthlib'), 'Google OAuth client required')
    def test_google_pkce_callback_channel_binding_and_private_refresh_token(self):
        import requests
        client = {'client_id':'test.apps.googleusercontent.com', 'client_secret':'mock-secret',
                  'auth_uri':'https://accounts.google.com/o/oauth2/auth',
                  'token_uri':'https://oauth2.googleapis.com/token'}
        save_private(self.directory/'client.json', {'installed':client})
        grant = {}
        callbacks = []

        def browser(prompt, *args, **kwargs):
            if 'accounts.google.com/' not in prompt:
                return
            url = urllib.parse.urlsplit(prompt.splitlines()[-1])
            grant.update(urllib.parse.parse_qs(url.query))
            callback = grant['redirect_uri'][0] + '?' + urllib.parse.urlencode({
                'code':'unit-code', 'state':grant['state'][0]})
            def visit():
                with urllib.request.urlopen(callback, timeout=3) as reply:
                    callbacks.append(reply.status)
            threading.Thread(target=visit, daemon=True).start()

        def google(session, request, **kwargs):
            if request.url == client['token_uri']:
                body = request.body.decode() if isinstance(request.body, bytes) else request.body
                fields = urllib.parse.parse_qs(body)
                if 'code' in fields:
                    digest = hashlib.sha256(fields['code_verifier'][0].encode()).digest()
                    self.assertEqual(base64.urlsafe_b64encode(digest).rstrip(b'=').decode(), grant['code_challenge'][0])
                else:
                    self.assertEqual(fields['refresh_token'], ['test-refresh'])
                payload = {'access_token':'test-access', 'refresh_token':'test-refresh',
                           'expires_in':3600, 'token_type':'Bearer', 'scope':SCOPE}
            else:
                self.assertTrue(request.url.startswith('https://www.googleapis.com/youtube/v3/channels?'))
                self.assertEqual(request.headers['Authorization'], 'Bearer test-access')
                payload = {'items':[{'id':'owner', 'snippet':{'title':'Test channel'}}]}
            response = requests.Response()
            response.status_code = 200
            response.url = request.url
            response.request = request
            response._content_consumed = True
            response._content = json.dumps(payload).encode()
            return response

        with patch('builtins.print', side_effect=browser), patch.object(requests.Session, 'send', google):
            authorize(self.directory, port=0)
            api, owner = ChannelAPI.connect(self.directory)
            api.http.close()
        self.assertEqual(owner, 'owner')
        self.assertEqual(callbacks, [200])
        self.assertEqual(grant['access_type'], ['offline'])
        self.assertEqual(grant['scope'], [SCOPE])
        saved = self.directory/'token.json'
        self.assertEqual(saved.stat().st_mode & 0o777, 0o600)
        self.assertNotIn('test-access', saved.read_text())
