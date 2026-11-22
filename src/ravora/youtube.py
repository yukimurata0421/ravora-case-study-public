"""Read-only YouTube live-chat adapter. Explicit targets and bounded streamList connections."""
from functools import partial
import json
import math
import time
import urllib.parse
import urllib.request
import urllib.error
import threading

from ravora.providers import ProviderError,NoRedirect
from ravora.youtube_auth import YouTubeError


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

    def _reserve_request(self):
        if not self.s.youtube_read_enabled:raise ProviderError('YOUTUBE_READ_DISABLED')
        if getattr(self,'stop',None) is not None and self.stop.is_set():raise ProviderError('YOUTUBE_READ_CANCELLED')
        if self.requests>=self.s.youtube_max_requests:raise ProviderError('YOUTUBE_REQUEST_LIMIT')
        self.requests+=1

    def resolve(self):
        if not self.s.youtube_video_id and not self.chat_id:raise ProviderError('MISSING_YOUTUBE_TARGET')
        if self.s.youtube_video_id:
            self._reserve_request()
            value=self.http.get('videos',{'part':'liveStreamingDetails','id':self.s.youtube_video_id,'fields':'items(id,liveStreamingDetails(activeLiveChatId))'})
            items=value.get('items',[])
            if len(items)!=1 or items[0].get('id')!=self.s.youtube_video_id:raise ProviderError('YOUTUBE_VIDEO_NOT_AVAILABLE')
            chat=items[0].get('liveStreamingDetails',{}).get('activeLiveChatId')
            if not isinstance(chat,str) or not chat:raise ProviderError('YOUTUBE_NO_ACTIVE_LIVE_CHAT')
            if self.chat_id and chat!=self.chat_id:raise ProviderError('YOUTUBE_TARGET_CHANGED')
            self.chat_id=chat
        return self.chat_id

    def run(self,stop:threading.Event,*,seconds=180,wait_for_chat=False):
        self.stop=stop
        if stop.is_set():self.status='stopped';return
        if not self.s.youtube_read_enabled:self.status='held';self.code='YOUTUBE_READ_DISABLED';return
        if not (self.s.youtube_key or self.access_token):self.status='held';self.code='MISSING_YOUTUBE_API_KEY';return
        started=self.clock();deadline=time.monotonic()+seconds;failures=0
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
                self._reserve_request()
                self.stream_opens += 1
                last_cursor = self.inbox.cursor(self.chat_id,streaming=True)
                self.status = 'connecting'
                try:
                    remaining = deadline - time.monotonic()
                    with self.stream(self.s, self.chat_id, last_cursor, remaining, stop) as pages:
                        for page in pages:
                            if stop.is_set() or time.monotonic() >= deadline:
                                self.status = 'stopped'
                                return
                            self.status = 'reading'
                            report = self.inbox.accept_page(self.chat_id, page, warm_start=warm_start, streaming=True)
                            self.batches += 1
                            refreshed = False
                            if self.on_page:
                                self.on_page()
                            for key, count in report['counts'].items():
                                self.counts[key] = self.counts.get(key, 0) + count
                            if report['ended']:
                                self.status = 'ended'
                                return
                            cursor = page.get('nextPageToken')
                            if cursor and cursor != last_cursor:
                                failures = 0
                                last_cursor = cursor
                    code = 'YOUTUBE_GRPC_STREAM_CLOSED'
                except ProviderError as exc:
                    code = str(exc)
                    if stop.is_set() or time.monotonic() >= deadline:
                        break
                    if code == 'YOUTUBE_GRPC_UNAUTHENTICATED' and self.access_token and not refreshed:
                        self.access_token(force_refresh=True)
                        refreshed = True
                    elif code not in ('YOUTUBE_GRPC_UNAVAILABLE','YOUTUBE_GRPC_DEADLINE_EXCEEDED',
                                    'YOUTUBE_GRPC_INTERNAL'):
                        raise
                if stop.is_set() or time.monotonic() >= deadline:
                    break
                if failures >= 3:
                    raise ProviderError(code)
                failures += 1
                self.status = 'reconnecting'
                stop.wait(min(max(self.s.youtube_poll_seconds, 2**failures), max(0,deadline-time.monotonic())))
            self.status = 'stopped'
        except Exception as exc:
            self.code=str(exc) if isinstance(exc,(ProviderError, YouTubeError)) else 'INVALID_YOUTUBE_RESPONSE'
            self.status='stopped' if stop.is_set() else 'failed'

    def snapshot(self):return {'status':self.status,'code':self.code,'requests':self.requests,'counts':self.counts,'read_only':True,'transport':'grpc_stream_list','stream_opens':self.stream_opens,'batches':self.batches}
