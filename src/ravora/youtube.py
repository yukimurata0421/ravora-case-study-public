"""Read-only YouTube live-chat adapter. Explicit targets and bounded streamList connections."""
from functools import partial
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import json
import logging
import math
import time
import urllib.parse
import urllib.request
import urllib.error
import threading

from ravora.providers import ProviderError,NoRedirect
from ravora.youtube_auth import YouTubeError

LOG = logging.getLogger(__name__)
GRPC_CODES = frozenset('YOUTUBE_GRPC_' + code for code in (
    'CANCELLED', 'UNKNOWN', 'INVALID_ARGUMENT', 'DEADLINE_EXCEEDED', 'NOT_FOUND', 'ALREADY_EXISTS',
    'PERMISSION_DENIED', 'RESOURCE_EXHAUSTED', 'FAILED_PRECONDITION', 'ABORTED', 'OUT_OF_RANGE',
    'UNIMPLEMENTED', 'INTERNAL', 'UNAVAILABLE', 'DATA_LOSS', 'UNAUTHENTICATED'))
TRANSIENT = {'YOUTUBE_GRPC_UNAVAILABLE', 'YOUTUBE_GRPC_DEADLINE_EXCEEDED', 'YOUTUBE_GRPC_INTERNAL'}
QUOTA_ZONE = ZoneInfo('America/Los_Angeles')
STREAM_UNITS = 5


class YouTubeHTTP:
    def __init__(self,settings):
        self.s=settings
        self.opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())

    def get(self,resource,params):
        if not self.s.youtube_read_enabled:raise ProviderError('YOUTUBE_READ_DISABLED')
        if not self.s.youtube_key:raise ProviderError('MISSING_YOUTUBE_API_KEY')
        if resource!='videos':raise ProviderError('INVALID_YOUTUBE_RESOURCE')
        # Never log this URL: Google's supported API-key query contains a credential.
        url='https://www.googleapis.com/youtube/v3/'+resource+'?'+urllib.parse.urlencode({**params,'key':self.s.youtube_key})
        try:
            with self.opener.open(urllib.request.Request(url),timeout=self.s.timeout) as response:
                data=response.read(2*1024*1024+1)
            if len(data)>2*1024*1024:raise ProviderError('YOUTUBE_RESPONSE_TOO_LARGE')
            return json.loads(data)
        except urllib.error.HTTPError as exc:
            try:
                value=json.loads(exc.read(65536));reason=value.get('error',{}).get('errors',[{}])[0].get('reason','unknown')
            except (ValueError,IndexError,TypeError):reason='unknown'
            # Allow only stable known codes, never provider messages/URLs.
            known={'liveChatEnded','liveChatDisabled','liveChatNotFound','quotaExceeded','dailyLimitExceeded','rateLimitExceeded','forbidden','invalidPageToken','keyInvalid','accessNotConfigured'}
            error=ProviderError('YOUTUBE_HTTP_'+str(exc.code)+':'+(reason if reason in known else 'unknown'))
            try:
                retry_after=float(exc.headers.get('Retry-After','0'))
                error.retry_after=retry_after if math.isfinite(retry_after) and retry_after>=0 else 0
            except (ValueError,TypeError):error.retry_after=0
            finally:exc.close()
            raise error from None
        except (OSError,urllib.error.URLError):raise ProviderError('YOUTUBE_CONNECTION_FAILED') from None
        except (ValueError,UnicodeError):raise ProviderError('INVALID_YOUTUBE_JSON') from None


class YouTubeReader:
    def __init__(self,settings,inbox,*,transport=None,clock=time.time,on_page=None,stream=None,access_token=None):
        self.s=settings;self.inbox=inbox;self.http=transport or YouTubeHTTP(settings);self.clock=clock
        from ravora.youtube_stream import stream_list
        self.access_token = access_token
        self.stream=stream or (partial(stream_list, access_token=access_token) if access_token else stream_list);self.stream_opens=0;self.batches=0
        self.requests=0;self.status='prepared';self.code=None;self.counts={};self.chat_id=settings.youtube_chat_id;self.on_page=on_page
        self.hourly = False
        self.last_connection = None
        self.retry_at = None
        self.deadline = math.inf
        self.daily_units = None
        self.quota_day = None
        self.poll_not_before = 0.
        self.last_activity = -math.inf
        self.idle_closures = 0
        self.short_closures = 0
        self.budget_target = ('video:' + settings.youtube_video_id if settings.youtube_video_id else
                              'chat:' + (settings.youtube_chat_id or ''))

    def _request_slot(self, units):
        rate = self.s.youtube_chat_requests_per_hour
        with self.inbox.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            now = self.clock()
            midnight = datetime.fromtimestamp(now, QUOTA_ZONE).replace(hour=0, minute=0, second=0, microsecond=0)
            day = int(midnight.timestamp())
            self.quota_day = midnight.date().isoformat()
            usage = db.execute('SELECT reserved_units FROM youtube_chat_daily_usage WHERE period_start=?', (day,)).fetchone()
            if usage is None:
                # Old builds only retained the last hour. Charge visible, pre-migration attempts conservatively.
                legacy_count, = db.execute('SELECT count(*) FROM youtube_chat_connections WHERE reserved_at>=?', (day,)).fetchone()
                self.daily_units = legacy_count * STREAM_UNITS
                db.execute('INSERT INTO youtube_chat_daily_usage VALUES (?,?)', (day, self.daily_units))
            else:
                self.daily_units = usage[0]
            if self.daily_units + units > self.s.youtube_chat_daily_units:
                return 'waiting_for_daily_budget', (midnight + timedelta(days=1)).timestamp() - now
            db.execute('DELETE FROM youtube_chat_connections WHERE reserved_at <= ?', (now - 3600,))
            if rate or self.hourly:
                count, oldest, latest = db.execute('SELECT count(*), min(reserved_at), max(reserved_at) '
                    'FROM youtube_chat_connections WHERE target=?', (self.budget_target,)).fetchone()
                if count >= (rate or self.s.youtube_max_requests):
                    return 'waiting_for_request_budget', oldest + 3600 - now
                if rate and latest is not None:
                    delay = latest + 3600 / rate - now
                    if delay > 0:
                        return 'pacing_requests', delay
                db.execute('INSERT INTO youtube_chat_connections VALUES (?, ?)', (self.budget_target, now))
            db.execute('UPDATE youtube_chat_daily_usage SET reserved_units=reserved_units+? WHERE period_start=?', (units, day))
            self.daily_units += units
            return None, 0

    def _reserve_request(self, units):
        if not self.s.youtube_read_enabled:raise ProviderError('YOUTUBE_READ_DISABLED')
        if not self.hourly and self.s.youtube_chat_requests_per_hour is None and self.requests>=self.s.youtube_max_requests:
            raise ProviderError('YOUTUBE_REQUEST_LIMIT')
        while True:
            if getattr(self,'stop',None) is not None and self.stop.is_set():
                raise ProviderError('YOUTUBE_READ_CANCELLED')
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise ProviderError('YOUTUBE_READ_WINDOW_ENDED')
            why, delay = self._request_slot(units)
            if not why:
                break
            if self._wait(why, min(delay, remaining)):
                raise ProviderError('YOUTUBE_READ_CANCELLED')
        self.requests+=1

    def _wait(self, status, delay):
        self.status = status
        self.retry_at = self.clock() + delay
        LOG.info('YOUTUBE_CHAT_WAIT reason=%s seconds=%.3f', status, delay)
        try:
            return self.stop.wait(delay)
        finally:
            self.retry_at = None

    def _receive(self, deadline, warm_start):
        cursor = self.inbox.cursor(self.chat_id, streaming=True)
        self.stream_opens += 1
        self.status = 'connecting'
        opened_at, started = self.clock(), time.monotonic()
        first_batch = self.batches
        reason = 'client_error'
        try:
            timeout = None if math.isinf(deadline) else max(0, deadline - started)
            with self.stream(self.s, self.chat_id, cursor, timeout, self.stop) as pages:
                for page in pages:
                    if self.stop.is_set() or time.monotonic() >= deadline:
                        break
                    self.status = 'reading'
                    report = self.inbox.accept_page(self.chat_id, page, warm_start=warm_start, streaming=True)
                    received = time.monotonic()
                    if report['poll_after_seconds'] is not None:
                        self.poll_not_before = max(self.poll_not_before, received + report['poll_after_seconds'])
                    if report['counts'].get('queued'):
                        self.last_activity = received
                    self.batches += 1
                    for key, count in report['counts'].items():
                        self.counts[key] = self.counts.get(key, 0) + count
                    if self.on_page:
                        self.on_page()
                    if report['ended']:
                        reason = 'chat_ended'
                        return reason
            reason = 'eof'
            return reason
        except ProviderError as exc:
            if str(exc) in GRPC_CODES or str(exc) == 'YOUTUBE_DAILY_LIMIT_EXCEEDED':
                reason = str(exc)
            raise
        finally:
            if self.stop.is_set():
                reason = 'local_stop'
            elif time.monotonic() >= deadline and reason in ('eof','YOUTUBE_GRPC_DEADLINE_EXCEEDED'):
                reason = 'session_deadline'
            record = {'attempt': self.stream_opens, 'opened_at': opened_at,
                      'duration_seconds': round(time.monotonic() - started, 3),
                      'batches': self.batches - first_batch, 'reason': reason}
            self.last_connection = record
            LOG.info('YOUTUBE_CHAT_CONNECTION %s', json.dumps(record, separators=(',', ':')))

    def resolve(self):
        if not self.s.youtube_video_id and not self.chat_id:raise ProviderError('MISSING_YOUTUBE_TARGET')
        if self.s.youtube_video_id:
            self._reserve_request(1)
            value=self.http.get('videos',{'part':'liveStreamingDetails','id':self.s.youtube_video_id,'fields':'items(id,liveStreamingDetails(activeLiveChatId))'})
            items=value.get('items',[])
            if len(items)!=1 or items[0].get('id')!=self.s.youtube_video_id:raise ProviderError('YOUTUBE_VIDEO_NOT_AVAILABLE')
            chat=items[0].get('liveStreamingDetails',{}).get('activeLiveChatId')
            if not isinstance(chat,str) or not chat:raise ProviderError('YOUTUBE_NO_ACTIVE_LIVE_CHAT')
            if self.chat_id and chat!=self.chat_id:raise ProviderError('YOUTUBE_TARGET_CHANGED')
            self.chat_id=chat
        return self.chat_id

    def _eof_wait(self):
        minimum = self.s.youtube_poll_seconds
        if self.s.youtube_chat_requests_per_hour is None:
            return 'reconnecting', minimum
        recent_comment = time.monotonic() - self.last_activity < 30
        self.idle_closures = 0 if recent_comment else min(2, self.idle_closures + 1)
        delay = max(minimum, (0, 10, 15)[self.idle_closures])
        if self.last_connection['duration_seconds'] < 3 and not recent_comment:
            self.short_closures += 1
            if self.short_closures >= 3:
                self.short_closures = 0
                return 'short_stream_cooldown', max(delay, 60)
        else:
            self.short_closures = 0
        return 'reconnecting', delay

    def run(self,stop:threading.Event,*,seconds=180,wait_for_chat=False):
        self.stop=stop
        self.hourly = seconds is None
        if stop.is_set():self.status='stopped';return
        if not self.s.youtube_read_enabled:self.status='held';self.code='YOUTUBE_READ_DISABLED';return
        if not (self.s.youtube_key or self.access_token):self.status='held';self.code='MISSING_YOUTUBE_API_KEY';return
        started=self.clock();deadline=time.monotonic()+seconds if seconds is not None else float('inf');failures=0
        self.deadline = deadline
        refreshed = False
        try:
            while True:
                try:
                    self.resolve()
                    break
                except ProviderError as exc:
                    if not wait_for_chat or str(exc) != 'YOUTUBE_NO_ACTIVE_LIVE_CHAT':
                        raise
                    self.status = 'waiting_for_chat'
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or stop.wait(min(remaining, max(self.s.youtube_poll_seconds, 5))):
                        self.status = 'stopped'
                        return
            if stop.is_set():
                self.status = 'stopped'
                return
            warm_start = started if self.inbox.cursor(self.chat_id,streaming=True) is None else None
            while not stop.is_set() and time.monotonic() < deadline:
                self._reserve_request(STREAM_UNITS)
                first_batch = self.batches
                wait_reason = 'reconnecting'
                try:
                    reason = self._receive(deadline, warm_start)
                    if reason == 'chat_ended':
                        self.status = 'ended'
                        return
                    failures = 0
                    if self.batches > first_batch:
                        refreshed = False
                    wait_reason, delay = self._eof_wait()
                except ProviderError as exc:
                    code = str(exc)
                    if stop.is_set() or time.monotonic() >= deadline:
                        break
                    if self.batches > first_batch:
                        refreshed = False
                    self.short_closures = 0
                    live_budget = self.hourly or self.s.youtube_chat_requests_per_hour is not None
                    rate_limited = live_budget and code == 'YOUTUBE_GRPC_RESOURCE_EXHAUSTED'
                    if code == 'YOUTUBE_GRPC_UNAUTHENTICATED' and self.access_token and not refreshed:
                        self.access_token(force_refresh=True)
                        refreshed = True
                    elif code not in TRANSIENT and not rate_limited:
                        raise
                    if not live_budget and failures >= 3:
                        raise
                    base = 60 if rate_limited else max(2, self.s.youtube_poll_seconds)
                    delay = min(300 if rate_limited else 60, base * 2**min(failures, 6))
                    if rate_limited:
                        delay = max(delay, getattr(exc,'retry_after',0))
                    failures += 1
                if stop.is_set() or time.monotonic() >= deadline:
                    break
                delay = max(delay, self.poll_not_before - time.monotonic())
                self._wait(wait_reason, min(delay, max(0,deadline-time.monotonic())))
            self.status = 'stopped'
        except Exception as exc:
            if str(exc) == 'YOUTUBE_READ_WINDOW_ENDED':
                self.status = 'stopped'
            else:
                self.code=str(exc) if isinstance(exc,(ProviderError, YouTubeError)) else 'INVALID_YOUTUBE_RESPONSE'
                self.status='stopped' if stop.is_set() else 'failed'

    def snapshot(self):return {'status':self.status,'code':self.code,'requests':self.requests,'counts':self.counts,'read_only':True,'transport':'grpc_stream_list','stream_opens':self.stream_opens,'batches':self.batches,
                               'last_connection':self.last_connection,'retry_at':self.retry_at,
                               'daily_reserved_units':self.daily_units,'daily_unit_limit':self.s.youtube_chat_daily_units,
                               'quota_day_pacific':self.quota_day,
                               'requests_per_hour':self.s.youtube_chat_requests_per_hour}
