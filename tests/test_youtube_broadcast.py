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
import tomllib
import unittest
import urllib.parse
import urllib.request
from unittest.mock import Mock, patch

from ravora.youtube_auth import (ChannelAPI, SCOPE, YouTubeError, authorize, publisher_lock,
                                 read_private, save_private)
from ravora.settings import SettingsError
if find_spec('av') and find_spec('numpy') and find_spec('PIL'):
    from ravora.youtube_broadcast import BROADCAST, STREAM, Broadcast, video_metadata
else:
    raise unittest.SkipTest('youtube-live extra required')


class FakeChannel:
    def __init__(self):
        self.rows = {BROADCAST:{}, STREAM:{}, 'videos':{}}
        self.writes = Counter()
        self.lose_response = set()
        self.hidden = False
        self.receiving = False
        self.serve_chat = True
        self.http = Mock()

    def access_token(self):
        return 'mock-channel-access'

    def request(self, method, resource, params, body=None, *, if_match=None):
        if resource == 'videos' and params['part'] == 'liveStreamingDetails':
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
                snippet = {key:row['snippet'][key] for key in ('channelId', 'title', 'description')}
                self.rows['videos'][ident] = {'id':ident, 'etag':'v1', 'snippet':{**snippet, 'categoryId':'22',
                                                                     'defaultLanguage':'ja'}}
            self.rows[resource][ident] = row
        elif resource == 'videos':
            assert method == 'PUT' and params == {'part':'snippet'} and set(body) == {'id', 'snippet'}
            assert set(body['snippet']) <= {'title', 'categoryId', 'description', 'tags', 'defaultLanguage'}
            assert body['snippet']['title'] and body['snippet']['categoryId']
            row = self.rows['videos'][body['id']]
            if if_match != row['etag']:
                raise YouTubeError('YOUTUBE_API_HTTP_412', 412)
            row['etag'] = 'v2'
            row['snippet'] = {**deepcopy(body['snippet']), 'channelId':row['snippet']['channelId']}
            if 'tags' in row['snippet']:
                row['snippet']['tags'].sort()  # YouTube can return the saved tags in a different order.
            self.rows[BROADCAST][body['id']]['snippet'].update({k:row['snippet'][k] for k in ('title','description')})
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

    def test_pinned_live_resume_waits_for_the_ingest_and_preserves_studio_visibility(self):
        b = self.manager()
        target = b.prepare()
        identifier = b.state['broadcast']
        row = self.api.rows[BROADCAST][identifier]
        row['status'].update(lifeCycleStatus='live', privacyStatus='public')
        writes = self.api.writes.copy()
        self.api.receiving = True
        with self.assertRaisesRegex(YouTubeError, 'SETTINGS_CHANGED'):
            b.resume_live(identifier)
        resumed = self.manager(privacy='public')
        with self.assertRaisesRegex(YouTubeError, 'INGEST_NOT_IDLE'):
            resumed.resume_live(identifier, timeout=0)
        with patch.object(resumed.stop, 'wait', side_effect=lambda _: setattr(self.api, 'receiving', False)):
            self.assertEqual(resumed.resume_live(identifier), target)
        with self.assertRaisesRegex(YouTubeError, 'TARGET_MISMATCH'):
            resumed.resume_live('another-event')
        row['status']['lifeCycleStatus'] = 'complete'
        with self.assertRaisesRegex(YouTubeError, 'NOT_LIVE'):
            resumed.resume_live(identifier)
        row['status']['lifeCycleStatus'] = 'ready'
        self.api.receiving = True
        with self.assertRaisesRegex(YouTubeError, 'ENDED_BEFORE_START'):
            resumed.wait_live(resume_only=True)
        self.assertEqual(self.api.writes, writes)

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

    def test_metadata_on_new_event_resumes_once_and_preserves_studio_edits(self):
        first = video_metadata({'title':'初回', 'description':'一緒に雑談しましょう。', 'tags':['Ray', 'AI VTuber']})
        next_edition = video_metadata({'title':'次回', 'description':'次の回', 'category_id':'24'})
        b = self.manager(title=None, metadata=first)
        self.api.lose_response.update([BROADCAST, 'videos'])
        b.prepare()
        original = b.state['broadcast']
        snippet = self.api.rows['videos'][original]['snippet']
        self.assertEqual(snippet, {**first, 'tags':sorted(first['tags']), 'categoryId':'22',
                                   'defaultLanguage':'ja', 'channelId':'owner'})
        self.assertEqual(self.api.writes['videos'], 1)
        snippet['description'] = 'Studioで編集'
        b = self.manager(title=None, metadata=next_edition)
        b.prepare()
        self.assertEqual(b.state['broadcast'], original)
        self.assertEqual(snippet['description'], 'Studioで編集')
        self.assertEqual(self.api.writes['videos'], 1)
        self.api.rows[BROADCAST][original]['status']['lifeCycleStatus'] = 'complete'
        b.prepare()
        self.assertNotEqual(b.state['broadcast'], original)
        self.assertEqual(self.api.rows['videos'][b.state['broadcast']]['snippet']['categoryId'], '24')
        self.assertEqual((self.api.writes[BROADCAST], self.api.writes['videos']), (2, 2))

    def test_studio_defaults_publish_english_text_even_when_channel_defaults_to_japanese(self):
        config = Path(__file__).resolve().parents[1] / 'examples/studio-nvenc.toml'
        desired = video_metadata(tomllib.loads(config.read_text())['youtube_live']['metadata'])
        broadcast = self.manager(title=None, metadata=desired)
        broadcast.prepare()
        video = self.api.rows['videos'][broadcast.state['broadcast']]['snippet']
        self.assertEqual(video['defaultLanguage'], 'en')
        self.assertRegex(video['title'], r'Live Chat with Ray.*\d{4}-\d{2}-\d{2}$')
        self.assertEqual(video['description'], desired['description'])
        self.assertIn('live chat', video['description'])
        self.assertEqual(self.api.writes['videos'], 1)

    def test_latency_default_and_override_apply_only_to_new_events(self):
        b = self.manager()
        b.prepare()
        first = self.api.rows[BROADCAST][b.state['broadcast']]
        self.assertEqual(first['contentDetails']['latencyPreference'], 'ultraLow')
        self.assertEqual(first['contentDetails']['closedCaptionsType'], 'closedCaptionsDisabled')

        first['contentDetails']['latencyPreference'] = 'normal'  # operator edit in Studio
        b = self.manager(latency_preference='low')
        b.prepare()
        self.assertEqual(first['contentDetails']['latencyPreference'], 'normal')
        self.assertEqual(b.state['broadcast'], first['id'])
        first['status']['lifeCycleStatus'] = 'complete'
        b.prepare()
        second = self.api.rows[BROADCAST][b.state['broadcast']]
        self.assertEqual(second['contentDetails']['latencyPreference'], 'low')
        self.assertNotIn('closedCaptionsType', second['contentDetails'])
        self.assertEqual(self.api.writes[BROADCAST], 2)

    def test_lost_insert_checks_latency_before_binding_the_recovered_event(self):
        b = self.manager()
        self.api.hidden = True
        self.api.lose_response.add(BROADCAST)
        with self.assertRaisesRegex(YouTubeError, 'RECONCILE_PENDING_CREATE_BROADCAST'):
            b.prepare()
        event, = self.api.rows[BROADCAST].values()
        self.api.hidden = False
        del event['contentDetails']['latencyPreference']
        for reported in (None, 'low'):
            if reported:
                event['contentDetails']['latencyPreference'] = reported
            with self.subTest(reported=reported), self.assertRaisesRegex(YouTubeError, 'LATENCY_NOT_CONFIRMED'):
                self.manager(latency_preference='low').prepare()
            self.assertEqual(self.api.writes[BROADCAST + '/bind'], 0)
        event['contentDetails']['latencyPreference'] = 'ultraLow'
        self.manager(latency_preference='low').prepare()
        self.assertEqual((self.api.writes[BROADCAST], self.api.writes[BROADCAST + '/bind']), (1, 1))

    def test_audience_is_declared_only_on_new_inserts(self):
        manager = self.manager()
        manager.prepare()
        event = self.api.rows[BROADCAST][manager.state['broadcast']]
        self.assertNotIn('selfDeclaredMadeForKids', event['status'])
        manager = self.manager(made_for_kids=False)
        manager.prepare()
        self.assertNotIn('selfDeclaredMadeForKids', event['status'])

        event['status']['lifeCycleStatus'] = 'complete'
        manager.prepare()
        event = self.api.rows[BROADCAST][manager.state['broadcast']]
        self.assertIs(event['status']['selfDeclaredMadeForKids'], False)
        self.manager(made_for_kids=True).prepare()
        self.assertIs(event['status']['selfDeclaredMadeForKids'], False)
        self.assertEqual(self.api.writes[BROADCAST], 2)

    def test_resume_with_reordered_tags_needs_no_writes_and_detects_changed_tags(self):
        desired = video_metadata({'title':'Ray', 'description':'雑談', 'tags':['Ray', 'AI VTuber', '雑談']})
        b = self.manager(title=None, metadata=desired)
        b.prepare()
        identifier = b.state['broadcast']
        row = self.api.rows['videos'][identifier]['snippet']
        self.assertNotEqual(row['tags'], desired['tags'])
        writes = self.api.writes.copy()
        # Disk state left by the old launcher after the PUT had already succeeded.
        b.save(video_metadata=desired)
        resumed = self.manager(title=None, metadata=desired)
        resumed.prepare()
        self.assertEqual((resumed.state['broadcast'], resumed.state['video_metadata']), (identifier, None))
        self.assertEqual(self.api.writes, writes)

        resumed.save(video_metadata=desired)
        for changed in (['AI VTuber', 'Ray'], ['AI VTuber', 'Ray', '他のタグ'], [None], 'Ray'):
            with self.subTest(changed=changed):
                row['tags'] = changed
                with self.assertRaisesRegex(YouTubeError, 'METADATA_CHANGED'):
                    resumed.prepare()
                self.assertEqual(self.api.writes, writes)

    def test_metadata_failure_then_resume_uses_frozen_values_and_exact_video(self):
        first = video_metadata({'title':'Day 1', 'description':'First', 'tags':['Ray']})
        b = self.manager(title=None, metadata=first)
        api_request = self.api.request
        def unavailable(method, resource, params, body=None, **kwargs):
            if method == 'PUT':
                raise YouTubeError('YOUTUBE_API_HTTP_403_forbidden', 403)
            return api_request(method, resource, params, body, **kwargs)
        with patch.object(self.api, 'request', side_effect=unavailable), self.assertRaisesRegex(YouTubeError, '403'):
            b.prepare()
        identifier = b.state['broadcast']
        self.assertEqual(self.api.rows[BROADCAST][identifier]['status']['lifeCycleStatus'], 'ready')
        b = self.manager(title=None, metadata=video_metadata({'title':'Day 2'}))
        row = self.api.rows['videos'][identifier]
        row['snippet']['channelId'] = 'other'
        with self.assertRaisesRegex(YouTubeError, 'OWNED_RESOURCE'):
            b.prepare()
        row['snippet']['channelId'] = 'owner'
        row['snippet']['description'] = 'studio edit'
        with self.assertRaisesRegex(YouTubeError, 'METADATA_CHANGED'):
            b.prepare()
        self.assertEqual(self.api.writes['videos'], 0)
        row['snippet']['description'] = b.state['event_marker']
        b.prepare()
        self.assertEqual(b.state['broadcast'], identifier)
        self.assertEqual(row['snippet']['title'], 'Day 1')
        self.assertEqual(row['snippet']['description'], 'First')
        self.assertEqual((self.api.writes[BROADCAST], self.api.writes['videos']), (1, 1))

    def test_legacy_state_resumes_without_editing_its_video(self):
        b = self.manager()
        b.prepare()
        state = read_private(b.path)
        state.pop('video_metadata')
        state.pop('pending_latency_preference')
        self.api.rows[BROADCAST][state['broadcast']]['contentDetails'].pop('latencyPreference')
        save_private(b.path, state)
        restored = self.manager(title=None, metadata=video_metadata({'title':'New title'}))
        restored.prepare()
        self.assertEqual(restored.state['broadcast'], state['broadcast'])
        self.assertEqual(self.api.writes['videos'], 0)

    def test_concurrent_studio_change_is_not_overwritten(self):
        b = self.manager(title=None, metadata=video_metadata({'title':'Ray', 'description':'公開用の概要'}))
        request = self.api.request
        def studio_edit(method, resource, params, body=None, **kwargs):
            if method == 'PUT':
                row = self.api.rows['videos'][body['id']]
                row['etag'] = 'studio-version'
                row['snippet']['description'] = 'Studioで指定した概要'
            return request(method, resource, params, body, **kwargs)
        with patch.object(self.api, 'request', side_effect=studio_edit), self.assertRaisesRegex(YouTubeError, 'METADATA_CHANGED'):
            b.prepare()
        self.assertEqual(self.api.rows['videos'][b.state['broadcast']]['snippet']['description'], 'Studioで指定した概要')
        self.assertFalse(self.api.writes[BROADCAST+'/transition'])

    def test_channel_api_sends_conditional_snippet_put(self):
        api = ChannelAPI(Mock(valid=True, token='test-access'))
        self.addCleanup(api.http.close)
        with patch.object(api.http, 'request') as request:
            response = request.return_value.__enter__.return_value
            response.status_code = 200
            response.json.return_value = {'id':'owned'}
            body = {'id':'owned', 'snippet':{'title':'Ray', 'categoryId':'24'}}
            self.assertEqual(api.request('PUT', 'videos', {'part':'snippet'}, body, if_match='version-1'),
                             {'id':'owned'})
            args, kwargs = request.call_args
            self.assertEqual(args, ('PUT', 'https://www.googleapis.com/youtube/v3/videos'))
            self.assertEqual(kwargs['params'], {'part':'snippet'})
            self.assertEqual(kwargs['json'], body)
            self.assertEqual(kwargs['headers'], {'Authorization':'Bearer test-access', 'If-Match':'"version-1"'})

    def test_metadata_limits_and_jst_template(self):
        with patch('ravora.youtube_broadcast.datetime', wraps=datetime) as clock:
            clock.now.side_effect = lambda tz: datetime(2026, 11, 22, 15, 1, tzinfo=timezone.utc).astimezone(tz)
            meta = video_metadata({'title':'Ray｜{date}', 'description':'{date} 雑談'})
            self.assertEqual(meta, {'title':'Ray｜2026-11-23', 'description':'2026-11-23 雑談'})
            self.assertEqual(video_metadata({'title':'template'}, '手動タイトル')['title'], '手動タイトル')
        for bad in ({'title':'a'*101}, {'title':42}, {'description':'あ'*1667}, {'tags':'Ray'},
                    {'tags':['aa', 'a '*248]}, {'category_id':'0'}, {'tags':[' ']},
                    {'default_language':42}, {'default_language':'en US'}):
            with self.subTest(config=bad), self.assertRaises(SettingsError):
                video_metadata(bad)

    @unittest.skipUnless(find_spec('google_auth_oauthlib'), 'Google OAuth client required')
    def test_google_pkce_callback_channel_binding_and_private_refresh_token(self):
        import requests
        client = {'client_id':'test.apps.googleusercontent.com', 'client_secret':'mock-secret',
                  'auth_uri':'https://accounts.google.com/o/oauth2/auth',
                  'token_uri':'https://oauth2.googleapis.com/token'}
        save_private(self.directory/'client.json', {'installed':client})
        grant = {}
        callbacks = []
        callback_done = threading.Event()

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
                    callback_done.set()
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
        self.assertTrue(callback_done.wait(3))
        self.assertEqual(callbacks, [200])
        self.assertEqual(grant['access_type'], ['offline'])
        self.assertEqual(grant['scope'], [SCOPE])
        saved = self.directory/'token.json'
        self.assertEqual(saved.stat().st_mode & 0o777, 0o600)
        self.assertNotIn('test-access', saved.read_text())
