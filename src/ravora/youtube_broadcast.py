"""Lifecycle of the Ray-owned YouTube broadcast and reusable ingestion stream."""
from datetime import datetime, timedelta, timezone
import re
import time
import uuid

from ravora.live_media import publish_target
from ravora.settings import SettingsError
from ravora.youtube_auth import YouTubeError, read_private, save_private

BROADCAST = 'liveBroadcasts'
STREAM = 'liveStreams'
BROADCAST_PARTS = 'id,snippet,status,contentDetails'
STREAM_PARTS = 'id,snippet,cdn,status'
TERMINAL = {'complete', 'revoked'}
DEFAULT_LATENCY_PREFERENCE = 'ultraLow'


def validate_latency_preference(value):
    if value not in ('normal', 'low', 'ultraLow'):
        raise SettingsError('INVALID_YOUTUBE_LATENCY_PREFERENCE')
    return value


def video_metadata(config, title_override=None):
    """Render the public video snippet once, before creating any YouTube resources."""
    if not isinstance(config, dict) or config.keys() - {'title', 'description', 'default_language', 'tags', 'category_id'}:
        raise SettingsError('INVALID_YOUTUBE_METADATA')
    date = datetime.now(timezone(timedelta(hours=9))).date().isoformat()
    title = config.get('title', 'Ray Live')
    if title_override is not None:
        title = title_override
    elif isinstance(title, str):
        title = title.replace('{date}', date)
    if not isinstance(title, str) or not title.strip() or len(title) > 100 or any(c in title for c in '<>\r\n'):
        raise SettingsError('INVALID_YOUTUBE_TITLE')
    description = config.get('description', '')
    if not isinstance(description, str):
        raise SettingsError('INVALID_YOUTUBE_DESCRIPTION')
    description = description.replace('{date}', date)
    if len(description.encode('utf-8')) > 5000 or any(c in description for c in '<>'):
        raise SettingsError('INVALID_YOUTUBE_DESCRIPTION')
    snippet = {'title': title, 'description': description}
    if 'default_language' in config:
        language = config['default_language']
        if not isinstance(language, str) or not re.fullmatch(r'[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*', language):
            raise SettingsError('INVALID_YOUTUBE_METADATA_LANGUAGE')
        snippet['defaultLanguage'] = language
    if 'tags' in config:
        tags = config['tags']
        if not isinstance(tags, list) or any(not isinstance(tag, str) or not tag.strip() or
                any(c in tag for c in '<>\r\n') for tag in tags):
            raise SettingsError('INVALID_YOUTUBE_TAGS')
        if len(','.join(tags)) + 2 * sum(' ' in tag for tag in tags) > 500:
            raise SettingsError('INVALID_YOUTUBE_TAGS')
        snippet['tags'] = tags
    if 'category_id' in config:
        category = config['category_id']
        if not isinstance(category, str) or not re.fullmatch(r'[1-9][0-9]*', category):
            raise SettingsError('INVALID_YOUTUBE_CATEGORY_ID')
        snippet['categoryId'] = category
    return snippet


class Broadcast:
    def __init__(self, api, directory, channel, title, privacy, stop, *, metadata=None, made_for_kids=None,
                 latency_preference=DEFAULT_LATENCY_PREFERENCE):
        self.api = api
        self.path = directory / 'broadcast.json'
        self.channel = channel
        self.title = title
        self.metadata = metadata
        self.made_for_kids = made_for_kids
        self.latency_preference = latency_preference
        self.privacy = privacy
        self.stop = stop
        self.state = read_private(self.path) if self.path.exists() else {'channel': channel}
        if self.state.get('channel') != channel:
            raise YouTubeError('YOUTUBE_PUBLISHER_CHANNEL_MISMATCH')

    def save(self, **changes):
        self.state.update(changes)
        save_private(self.path, self.state)

    def item(self, resource, identifier):
        parts = {BROADCAST: BROADCAST_PARTS, STREAM: STREAM_PARTS, 'videos': 'id,snippet'}[resource]
        # A newly inserted liveBroadcast can briefly be absent from videos.list.
        for attempt in range(4 if resource == 'videos' else 1):
            if attempt and self.stop.wait(1):
                raise YouTubeError('YOUTUBE_START_CANCELLED')
            result = self.api.request('GET', resource, {'id':identifier, 'part':parts})
            rows = result.get('items', [])
            if rows:
                break
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

    def check_other_bindings(self, stream_id):
        # Another event can be attached in Studio even when the encoder is idle.
        for status in ('active', 'upcoming'):
            for row in self.matches(BROADCAST, {'broadcastStatus':status, 'broadcastType':'all',
                    'part':'id,contentDetails', 'maxResults':50,
                    'fields':'nextPageToken,items(id,contentDetails/boundStreamId)'}):
                if row['id'] != self.state.get('broadcast') and row.get('contentDetails', {}).get('boundStreamId') == stream_id:
                    raise YouTubeError('YOUTUBE_STREAM_BOUND_TO_ANOTHER_EVENT')

    def resume_live(self, video_id, *, timeout=120, interval=3):
        """Reattach an encoder to the specified already-live event without changing YouTube resources."""
        if (not video_id or self.state.get('broadcast') != video_id or not self.state.get('stream')
                or self.state.get('pending')):
            raise YouTubeError('YOUTUBE_LIVE_RESUME_TARGET_MISMATCH')
        deadline = time.monotonic() + timeout
        while not self.stop.is_set():
            stream = self.item(STREAM, self.state['stream'])
            event = self.current()
            if event['status']['lifeCycleStatus'] != 'live':
                raise YouTubeError('YOUTUBE_LIVE_RESUME_EVENT_NOT_LIVE')
            status = stream['status']['streamStatus']
            if status in ('ready', 'inactive'):
                self.check_other_bindings(stream['id'])
                info = stream['cdn']['ingestionInfo']
                return publish_target(info['rtmpsIngestionAddress'], info['streamName'])
            if status != 'active' or time.monotonic() >= deadline:
                raise YouTubeError('YOUTUBE_LIVE_RESUME_INGEST_NOT_IDLE')
            self.stop.wait(interval)
        raise YouTubeError('YOUTUBE_START_CANCELLED')

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
        self.check_other_bindings(stream['id'])
        if not self.state.get('event_marker'):
            title = self.title if self.metadata is None else self.metadata['title']
            self.save(event_marker='Ray Live ' + uuid.uuid4().hex, privacy=self.privacy, title=title,
                      video_metadata=self.metadata, made_for_kids=self.made_for_kids,
                      pending_latency_preference=self.latency_preference)
        if self.privacy != self.state['privacy'] or (self.title is not None and self.title != self.state['title']):
            raise YouTubeError('YOUTUBE_RESUME_TITLE_OR_PRIVACY_MISMATCH')
        status = {'privacyStatus': self.privacy}
        if self.state.get('made_for_kids') is not None:
            status['selfDeclaredMadeForKids'] = self.state['made_for_kids']
        details = {'enableAutoStart': False, 'enableAutoStop': False, 'monitorStream': {'enableMonitorStream': False}}
        latency = self.state.get('pending_latency_preference')
        if latency is not None:
            details['latencyPreference'] = latency
            if latency == 'ultraLow':
                # Ray draws its own subtitles into the video frames.
                details['closedCaptionsType'] = 'closedCaptionsDisabled'
        broadcast = self.ensure_resource('broadcast', BROADCAST, BROADCAST_PARTS, {
            'snippet': {'title':self.state['title'], 'description':self.state['event_marker'],
                        'scheduledStartTime':(datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat(timespec='seconds')},
            'status': status,
            'contentDetails': details})
        self.validate(broadcast)
        if latency is not None:
            if broadcast['contentDetails'].get('latencyPreference') != latency:
                raise YouTubeError('YOUTUBE_LATENCY_NOT_CONFIRMED_CHECK_STUDIO')
            self.save(pending_latency_preference=None)
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
        if self.state.get('video_metadata'):
            self.apply_video_metadata()
        return target

    def apply_video_metadata(self):
        identifier = self.state['broadcast']
        desired = self.state['video_metadata']
        video = self.item('videos', identifier)
        current = video['snippet']

        def matches(snippet):
            if 'tags' in desired:
                tags = snippet.get('tags', [])
                if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
                    return False
                if sorted(tags) != sorted(desired['tags']):
                    return False
            return all(snippet.get(key, '') == value for key, value in desired.items() if key != 'tags')

        if not matches(current):
            # Only replace our own temporary creation marker, including after a lost PUT response.
            if current.get('description') != self.state['event_marker'] or current.get('title') != desired['title']:
                raise YouTubeError('YOUTUBE_VIDEO_METADATA_CHANGED_CHECK_STUDIO')
            snippet = {key: current[key] for key in ('categoryId', 'defaultLanguage', 'tags') if key in current}
            snippet.update(desired)
            if not snippet.get('categoryId'):
                raise YouTubeError('YOUTUBE_VIDEO_CATEGORY_MISSING')
            if not video.get('etag'):
                raise YouTubeError('YOUTUBE_VIDEO_ETAG_MISSING')
            if self.stop.is_set():
                raise YouTubeError('YOUTUBE_START_CANCELLED')
            try:
                video = self.api.request('PUT', 'videos', {'part':'snippet'}, {'id':identifier, 'snippet':snippet},
                                         if_match=video['etag'])
            except YouTubeError as exc:
                if exc.status == 412:
                    raise YouTubeError('YOUTUBE_VIDEO_METADATA_CHANGED_CHECK_STUDIO', 412) from None
                if str(exc) != 'YOUTUBE_API_RESPONSE_UNKNOWN' and exc.status < 500 and exc.status != 408:
                    raise
                video = self.item('videos', identifier)
            returned = video.get('snippet', {})
            if video.get('id') != identifier or returned.get('channelId') != self.channel or not matches(returned):
                raise YouTubeError('YOUTUBE_VIDEO_METADATA_NOT_CONFIRMED')
        self.save(video_metadata=None)

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

    def wait_live(self, *, interval=3, timeout=150, resume_only=False):
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
            elif resume_only or phase in TERMINAL:
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
                return 'complete'
            if phase == 'live':
                if self.state.get('pending') == 'live':
                    self.save(pending=None)
                if not self.state.get('pending'):
                    self.transition('complete')
            elif phase != 'liveStarting' and not self.state.get('pending'):
                return  # A stopped pre-live draft can be resumed.
            time.sleep(interval)
        raise YouTubeError('YOUTUBE_END_NOT_CONFIRMED_CHECK_STUDIO')
