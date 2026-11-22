"""Lifecycle of the Ray-owned YouTube broadcast and reusable ingestion stream."""
from datetime import datetime, timedelta, timezone
import time
import uuid

from ravora.live_media import publish_target
from ravora.youtube_auth import YouTubeError, read_private, save_private

BROADCAST = 'liveBroadcasts'
STREAM = 'liveStreams'
BROADCAST_PARTS = 'id,snippet,status,contentDetails'
STREAM_PARTS = 'id,snippet,cdn,status'
TERMINAL = {'complete', 'revoked'}


class Broadcast:
    def __init__(self, api, directory, channel, title, privacy, stop):
        self.api = api
        self.path = directory / 'broadcast.json'
        self.channel = channel
        self.title = title
        self.privacy = privacy
        self.stop = stop
        self.state = read_private(self.path) if self.path.exists() else {'channel': channel}
        if self.state.get('channel') != channel:
            raise YouTubeError('YOUTUBE_PUBLISHER_CHANNEL_MISMATCH')

    def save(self, **changes):
        self.state.update(changes)
        save_private(self.path, self.state)

    def item(self, resource, identifier):
        parts = BROADCAST_PARTS if resource == BROADCAST else STREAM_PARTS
        result = self.api.request('GET', resource, {'id':identifier, 'part':parts})
        rows = result.get('items', [])
        if (len(rows) != 1 or rows[0].get('id') != identifier
                or rows[0].get('snippet', {}).get('channelId') != self.channel):
            raise YouTubeError('YOUTUBE_OWNED_RESOURCE_NOT_FOUND')
        return rows[0]

    def matches(self, resource, params):
        seen = set()
        while True:
            result = self.api.request('GET', resource, params)
            yield from result.get('items', [])
            page = result.get('nextPageToken')
            if not page:
                return
            if page in seen:
                raise YouTubeError('YOUTUBE_INVALID_PAGINATION')
            seen.add(page)
            params = {**params, 'pageToken': page}

    def mutate(self, intent, resource, params, body=None):
        if self.stop.is_set() and intent != 'complete':
            raise YouTubeError('YOUTUBE_START_CANCELLED')
        if self.state.get('pending'):
            raise YouTubeError('YOUTUBE_RECONCILE_PENDING_' + self.state['pending'].upper())
        self.save(pending=intent)
        try:
            return self.api.request('POST', resource, params, body)
        except YouTubeError as exc:
            # A timeout, 5xx or lost success response can hide an accepted change.
            if (400 <= exc.status < 500 and exc.status != 408) or str(exc) == 'YOUTUBE_OAUTH_REFRESH_FAILED_REAUTHORIZE':
                self.save(pending=None)
            raise

    def ensure_resource(self, key, resource, parts, body):
        if self.state.get(key):
            return self.item(resource, self.state[key])
        intent = 'create_' + key
        marker = body['snippet']['description']
        if self.state.get('pending') != intent:
            try:
                row = self.mutate(intent, resource, {'part': parts}, body)
            except YouTubeError:
                if self.state.get('pending') != intent:
                    raise
            else:
                if not row.get('id') or row.get('snippet', {}).get('channelId') != self.channel:
                    raise YouTubeError('YOUTUBE_CREATE_RESPONSE_UNKNOWN')
                self.save(**{key: row['id'], 'pending': None})
                return row
        # Inserts have no idempotency key. Locate this exact attempt on the channel.
        params = {'mine':'true', 'part':'id,snippet', 'maxResults':50}
        rows = [row for row in self.matches(resource, params)
                if row.get('snippet', {}).get('channelId') == self.channel
                and row['snippet'].get('description') == marker]
        if len(rows) != 1:
            raise YouTubeError('YOUTUBE_RECONCILE_PENDING_' + intent.upper())
        row = self.item(resource, rows[0]['id'])
        self.save(**{key: row['id'], 'pending': None})
        return row

    def prepare(self):
        if not self.state.get('stream_marker'):
            self.save(stream_marker='Ray stream ' + uuid.uuid4().hex)
        stream = self.ensure_resource('stream', STREAM, STREAM_PARTS + ',contentDetails', {
            'snippet': {'title':'Ray Live', 'description':self.state['stream_marker']},
            'cdn': {'ingestionType':'rtmp', 'resolution':'1080p', 'frameRate':'30fps'},
            'contentDetails': {'isReusable':True}})
        info = stream['cdn']['ingestionInfo']
        target = publish_target(info['rtmpsIngestionAddress'], info['streamName'])
        if stream['status']['streamStatus'] == 'active':
            raise YouTubeError('YOUTUBE_RAY_STREAM_ALREADY_RECEIVING')
        if self.state.get('broadcast'):
            existing = self.item(BROADCAST, self.state['broadcast'])
            if existing['status']['lifeCycleStatus'] == 'revoked':
                raise YouTubeError('YOUTUBE_BROADCAST_REVOKED_CHECK_STUDIO')
            if existing['status']['lifeCycleStatus'] == 'complete':
                self.save(broadcast=None, event_marker=None, pending=None)
            elif self.state.get('pending') == 'complete':
                raise YouTubeError('YOUTUBE_END_NOT_CONFIRMED_CHECK_STUDIO')
        # Another event can be attached in Studio even when the encoder is idle.
        for status in ('active', 'upcoming'):
            for row in self.matches(BROADCAST, {'broadcastStatus':status, 'broadcastType':'all',
                    'part':'id,contentDetails', 'maxResults':50,
                    'fields':'nextPageToken,items(id,contentDetails/boundStreamId)'}):
                if row['id'] != self.state.get('broadcast') and row.get('contentDetails', {}).get('boundStreamId') == stream['id']:
                    raise YouTubeError('YOUTUBE_STREAM_BOUND_TO_ANOTHER_EVENT')
        if not self.state.get('event_marker'):
            self.save(event_marker='Ray Live ' + uuid.uuid4().hex, privacy=self.privacy, title=self.title)
        if (self.privacy, self.title) != (self.state['privacy'], self.state['title']):
            raise YouTubeError('YOUTUBE_RESUME_TITLE_OR_PRIVACY_MISMATCH')
        broadcast = self.ensure_resource('broadcast', BROADCAST, BROADCAST_PARTS, {
            'snippet': {'title':self.title, 'description':self.state['event_marker'],
                        'scheduledStartTime':(datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat(timespec='seconds')},
            'status': {'privacyStatus':self.privacy},
            'contentDetails': {'enableAutoStart':False, 'enableAutoStop':False,
                               'monitorStream':{'enableMonitorStream':False}}})
        self.validate(broadcast)
        bound = broadcast['contentDetails'].get('boundStreamId')
        if bound == stream['id']:
            if self.state.get('pending') == 'bind':
                self.save(pending=None)
        elif bound:
            raise YouTubeError('YOUTUBE_BROADCAST_STREAM_CHANGED')
        else:
            if self.state.get('pending') != 'bind':
                try:
                    self.mutate('bind', BROADCAST + '/bind', {
                        'id':broadcast['id'], 'streamId':stream['id'], 'part':'id,contentDetails'})
                except YouTubeError:
                    if self.state.get('pending') != 'bind':
                        raise
            deadline = time.monotonic() + 15
            while self.item(BROADCAST, broadcast['id'])['contentDetails'].get('boundStreamId') != stream['id']:
                if time.monotonic() >= deadline or self.stop.wait(1):
                    raise YouTubeError('YOUTUBE_BIND_NOT_CONFIRMED')
            self.save(pending=None)
        return target

    def validate(self, event):
        if (event['status']['privacyStatus'] != self.privacy or event['contentDetails'].get('enableAutoStart')
                or event['contentDetails'].get('enableAutoStop')):
            raise YouTubeError('YOUTUBE_BROADCAST_SETTINGS_CHANGED')

    def current(self):
        event = self.item(BROADCAST, self.state['broadcast'])
        if event['status']['lifeCycleStatus'] in TERMINAL:
            return event
        self.validate(event)
        if event['contentDetails'].get('boundStreamId') != self.state['stream']:
            raise YouTubeError('YOUTUBE_BROADCAST_STREAM_CHANGED')
        return event

    def transition(self, status):
        try:
            self.mutate(status, BROADCAST + '/transition', {'id':self.state['broadcast'],
                        'broadcastStatus':status, 'part':'id,status'})
        except YouTubeError as exc:
            if str(exc).endswith(('_redundantTransition', '_invalidTransition')):
                phase = self.current()['status']['lifeCycleStatus']
                if phase == status or (status == 'live' and phase == 'liveStarting'):
                    self.save(pending=status)
                    return
            if self.state.get('pending') != status:
                raise
        # Keep the intent until a read observes the target state.

    def wait_live(self, *, interval=3, timeout=150):
        deadline = time.monotonic() + timeout
        while not self.stop.is_set() and time.monotonic() < deadline:
            event = self.current()
            phase = event['status']['lifeCycleStatus']
            if phase == 'live':
                if self.state.get('pending') == 'live':
                    self.save(pending=None)
                if self.state.get('pending'):
                    raise YouTubeError('YOUTUBE_RECONCILE_PENDING_' + self.state['pending'].upper())
                if self.item(STREAM, self.state['stream'])['status']['streamStatus'] == 'active':
                    return event
            elif phase in TERMINAL:
                raise YouTubeError('YOUTUBE_BROADCAST_ENDED_BEFORE_START')
            elif phase in ('ready', 'testing') and not self.state.get('pending'):
                stream = self.item(STREAM, self.state['stream'])
                if stream['status']['streamStatus'] == 'active':
                    self.transition('live')
            self.stop.wait(interval)
        if not self.stop.is_set():
            raise YouTubeError('YOUTUBE_START_NOT_CONFIRMED_CHECK_STUDIO')
        return None

    def chat_id(self, event, *, timeout=90, interval=5):
        chat = event['snippet'].get('liveChatId')
        deadline = time.monotonic() + timeout
        while not chat and not self.stop.is_set() and time.monotonic() < deadline:
            videos = self.api.request('GET', 'videos', {'id':event['id'], 'part':'liveStreamingDetails'})
            chat = next((video.get('liveStreamingDetails', {}).get('activeLiveChatId')
                         for video in videos.get('items', []) if video['id'] == event['id']), None)
            if not chat:
                self.stop.wait(interval)
        if not chat and not self.stop.is_set():
            raise YouTubeError('YOUTUBE_LIVE_CHAT_NOT_AVAILABLE')
        return chat

    def finish(self, *, interval=2, timeout=30):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            phase = self.current()['status']['lifeCycleStatus']
            if phase == 'complete':
                self.save(pending=None)
                return
            if phase == 'live':
                if self.state.get('pending') == 'live':
                    self.save(pending=None)
                if not self.state.get('pending'):
                    self.transition('complete')
            elif phase != 'liveStarting' and not self.state.get('pending'):
                return  # A stopped pre-live draft can be resumed.
            time.sleep(interval)
        raise YouTubeError('YOUTUBE_END_NOT_CONFIRMED_CHECK_STUDIO')
