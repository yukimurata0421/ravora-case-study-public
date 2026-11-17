"""Read-only YouTube live-chat adapter. Explicit targets, bounded polling, no posting."""
import json
import math
import time
import urllib.parse
import urllib.request
import urllib.error
import threading

from ravora.providers import ProviderError,NoRedirect


class YouTubeHTTP:
    def __init__(self,settings):
        self.s=settings
        self.opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())

    def get(self,resource,params):
        if not self.s.youtube_read_enabled:raise ProviderError('YOUTUBE_READ_DISABLED')
        if not self.s.youtube_key:raise ProviderError('MISSING_YOUTUBE_API_KEY')
        if resource not in ('videos','liveChat/messages'):raise ProviderError('INVALID_YOUTUBE_RESOURCE')
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
            raise error from None
        except (OSError,urllib.error.URLError):raise ProviderError('YOUTUBE_CONNECTION_FAILED') from None
        except (ValueError,UnicodeError):raise ProviderError('INVALID_YOUTUBE_JSON') from None


class YouTubeReader:
    def __init__(self,settings,inbox,*,transport=None,clock=time.time,on_page=None):
        self.s=settings;self.inbox=inbox;self.http=transport or YouTubeHTTP(settings);self.clock=clock
        self.requests=0;self.status='prepared';self.code=None;self.counts={};self.chat_id=settings.youtube_chat_id;self.on_page=on_page

    def _get(self,resource,params):
        if not self.s.youtube_read_enabled:raise ProviderError('YOUTUBE_READ_DISABLED')
        if getattr(self,'stop',None) is not None and self.stop.is_set():raise ProviderError('YOUTUBE_READ_CANCELLED')
        if self.requests>=self.s.youtube_max_requests:raise ProviderError('YOUTUBE_REQUEST_LIMIT')
        self.requests+=1;return self.http.get(resource,params)

    def resolve(self):
        if not self.s.youtube_video_id and not self.chat_id:raise ProviderError('MISSING_YOUTUBE_TARGET')
        if self.s.youtube_video_id:
            value=self._get('videos',{'part':'liveStreamingDetails','id':self.s.youtube_video_id,'fields':'items(id,liveStreamingDetails(activeLiveChatId))'})
            items=value.get('items',[])
            if len(items)!=1 or items[0].get('id')!=self.s.youtube_video_id:raise ProviderError('YOUTUBE_VIDEO_NOT_AVAILABLE')
            chat=items[0].get('liveStreamingDetails',{}).get('activeLiveChatId')
            if not isinstance(chat,str) or not chat:raise ProviderError('YOUTUBE_NO_ACTIVE_LIVE_CHAT')
            if self.chat_id and chat!=self.chat_id:raise ProviderError('YOUTUBE_TARGET_CHANGED')
            self.chat_id=chat
        return self.chat_id

    def run(self,stop:threading.Event,*,seconds=180):
        self.stop=stop
        if stop.is_set():self.status='stopped';return
        if not self.s.youtube_read_enabled:self.status='held';self.code='YOUTUBE_READ_DISABLED';return
        if not self.s.youtube_key:self.status='held';self.code='MISSING_YOUTUBE_API_KEY';return
        started=self.clock();deadline=time.monotonic()+seconds;failures=0
        try:
            self.resolve();self.status='reading'
            while not stop.is_set() and time.monotonic()<deadline:
                token=self.inbox.cursor(self.chat_id)
                params={'liveChatId':self.chat_id,'part':'id,snippet,authorDetails','maxResults':200,
                        'fields':'nextPageToken,pollingIntervalMillis,offlineAt,items(id,snippet(type,liveChatId,publishedAt,authorChannelId,textMessageDetails,userBannedDetails),authorDetails(channelId,displayName))'}
                if token:params['pageToken']=token
                try:
                    page=self._get('liveChat/messages',params)
                    if stop.is_set():self.status='stopped';break
                    report=self.inbox.accept_page(self.chat_id,page,warm_start=started if token is None else None)
                    failures=0
                    if self.on_page:self.on_page()
                except ProviderError as exc:
                    code=str(exc)
                    transient=code in ('YOUTUBE_CONNECTION_FAILED','YOUTUBE_HTTP_403:rateLimitExceeded') or code.startswith(('YOUTUBE_HTTP_429:','YOUTUBE_HTTP_500:','YOUTUBE_HTTP_502:','YOUTUBE_HTTP_503:','YOUTUBE_HTTP_504:'))
                    if not transient or failures>=3:raise
                    failures+=1
                    # Only idempotent GETs retry, at least as slowly as the last server interval.
                    delay=max(self.s.youtube_poll_seconds,getattr(self,'last_interval',0),getattr(exc,'retry_after',0),2**failures)
                    stop.wait(min(delay,max(0,deadline-time.monotonic())));continue
                for key,count in report['counts'].items():self.counts[key]=self.counts.get(key,0)+count
                if report['ended']:self.status='ended';break
                self.last_interval=max(self.s.youtube_poll_seconds,report['poll_after_seconds'])
                stop.wait(min(self.last_interval,max(0,deadline-time.monotonic())))
            if self.status=='reading':self.status='stopped'
        except Exception as exc:
            self.code=str(exc) if isinstance(exc,ProviderError) else 'INVALID_YOUTUBE_RESPONSE'
            self.status='ended' if self.code.endswith(':liveChatEnded') else 'stopped' if self.code=='YOUTUBE_READ_CANCELLED' else 'failed'

    def snapshot(self):return {'status':self.status,'code':self.code,'requests':self.requests,'counts':self.counts,'read_only':True}
