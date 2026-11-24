"""Comment reception -> inbox -> turn preparation -> browser playback acknowledgement."""
from dataclasses import dataclass
from collections import deque
import threading
import time
import uuid

from ravora.comments import Comment,CommentInbox,VIEWER_CHAT_SOURCES,fixture_page
from ravora.youtube import YouTubeReader


@dataclass
class PreparedReply:
    comment: dict
    bridge: object
    started: float
    token: dict
    discarded: bool = False
    prefix: object = None


class InputPipeline:
    def __init__(self,settings,store,bridge,fixtures,*,memory_fixture_comments=None):
        self.s=settings;self.bridge=bridge;self.inbox=CommentInbox(store,capacity=settings.comment_queue_size,ttl=settings.comment_ttl,
            scope=settings.mode,relationships_enabled=settings.memory_enabled and settings.relationship_enabled)
        self.fixtures=fixtures;self.stop=threading.Event();self.reader=None;self.thread=None
        self.last_source=None
        self.audience_present = None
        self.audience_size = None
        self.retiring_initiative = None
        self.prepared = None
        self.preparation_seconds = deque(maxlen=20)
        self.followup_claim=None
        self.active_followup=None
        self.active_receipt=None;self.paused_reason=None;self.lock=threading.RLock();self.source_done=False
        self.memory_fixture_comments=memory_fixture_comments or ('I like coffee and jazz.','Do you remember my favorite drink?')
        if len(self.memory_fixture_comments)!=2 or any(not isinstance(x,str) or not 0<len(x)<=1000 for x in self.memory_fixture_comments):raise ValueError('INVALID_MEMORY_FIXTURE')

    def start(self,seconds,*,wait_for_chat=False,access_token=None,twitch_reader=None):
        self.bridge.controller.memory_worker.wake()
        source=self.s.comment_source
        if source=='developer':return
        if source=='fixture':
            for i,item in enumerate(self.fixtures):self.inbox.put(Comment('fixture',self.inbox.session,str(i),'viewer-'+str(i),'Viewer',item['comment'],time.time()))
            self.source_done=True
        elif source=='memory-fixture':
            self.memory_fixture_stage=0;self._memory_fixture_next()
        elif source=='youtube-fixture':
            chat='fixture-'+self.inbox.session
            self.fixture_result=self.inbox.accept_page(chat,fixture_page(chat,time.time()),source='youtube-fixture')
            self.source_done=True
        elif source in ('youtube', 'twitch'):
            if source == 'twitch':
                if twitch_reader is None:
                    raise ValueError('TWITCH_CHAT_READER_REQUIRED')
                self.reader = twitch_reader
                options = {}
            else:
                self.reader=YouTubeReader(self.s,self.inbox,on_page=self.check_removed,access_token=access_token)
                options = {'wait_for_chat': wait_for_chat}
            def run():
                try:self.reader.run(self.stop,seconds=seconds,**options)
                finally:self.source_done=True
            self.thread=threading.Thread(target=run,daemon=True);self.thread.start()

    def accept_developer(self,text,message_id,author_id,*,mock=False,local=False):
        if self.s.comment_source!='developer':raise ValueError('DEVELOPER_INPUT_NOT_SELECTED')
        from ravora.comments import clean
        comment=Comment('developer-local' if local else 'developer-mock' if mock else 'developer',self.inbox.session,message_id,
                        author_id,'Developer',clean(text,1000),time.time())
        status=self.inbox.put(comment)
        if status not in ('queued','duplicate'):raise ValueError('DEVELOPER_COMMENT_'+status.upper())
        return comment.receipt

    def _memory_fixture_next(self):
        comments=self.memory_fixture_comments
        stage=self.memory_fixture_stage
        if stage>=len(comments):self.source_done=True;return
        self.inbox.put(Comment('memory-fixture',f'{self.inbox.session}-{stage}',str(stage),
            'returning-channel-'+self.inbox.session,'Alex',comments[stage],time.time()))
        self.memory_fixture_stage+=1

    def snapshot(self):
        return {'source':self.s.comment_source,'finished':self.source_done,'paused_reason':self.paused_reason,
                'queue':self.inbox.summary(),'provider_requests_attempted':self.inbox.requests_attempted(),'reader':self.reader.snapshot() if self.reader else None,
                'fixture_page':getattr(self,'fixture_result',None),'memory_worker':self.bridge.controller.memory_worker.snapshot()}

    def next(self, *, allow_host_input=False, allow_initiative=True, prefer_host=False):
        with self.lock:
            if self.paused_reason:return {'available':False,'finished':True,'held':True,'code':self.paused_reason}
            if not allow_host_input and self.reader and self.reader.status=='failed':return {'available':False,'finished':True,'error':self.reader.code}
            if self.prepared or self.active_receipt or (self.bridge.worker and self.bridge.worker.is_alive()):return {'available':False,'finished':False}
            prefer='live-host' if allow_host_input and (prefer_host or self.last_source!='live-host') else None
            comment=self.inbox.take(prefer_source=prefer,playout_delay=self.estimated_preparation())
            if comment:
                self.last_source=comment['source']
                return {'available':True,'finished':False,'comment':comment}
            character=getattr(self.bridge.controller,'character',None)
            live = self.s.comment_source in VIEWER_CHAT_SOURCES or (allow_host_input and self.s.comment_source=='developer')
            if (live and allow_initiative and character and self.s.character_initiative_enabled
                    and not self.retiring_initiative and (self.audience_present is None or self.audience_present())):
                options = {'quiet_single':self.audience_size()==1} if self.audience_size else {}
                thread = character.take_followup(audience_opening=self.audience_present is not None, **options)
                if self.inbox.store.can_initiate(thread,self.s.mode):
                    initiative = Comment('ray-initiative',self.inbox.session,uuid.uuid4().hex,'ray','Ray',
                                         thread.message,time.time())
                    comment = self.inbox.claim_idle_initiative(initiative)
                    if comment:
                        self.followup_claim = (initiative.receipt,thread)
                        return {'available':True,'finished':False,'comment':comment}
            held=bool(self.reader and self.reader.status=='held')
            return {'available':False,'finished':self.source_done,'held':held,'code':self.reader.code if held else None}

    def begin(self,receipt):
        if not isinstance(receipt,str) or len(receipt)!=64:raise ValueError('INVALID_COMMENT_RECEIPT')
        with self.lock:
            if self.paused_reason:raise ValueError('INPUT_PAUSED')
            comment,viewer=self.inbox.claimed(receipt)
            if self.active_receipt or self.prepared:raise ValueError('TURN_BUSY')
            if viewer.startswith('ray-initiative:') and (not self.followup_claim or self.followup_claim[0]!=receipt):
                raise ValueError('COMMENT_NO_LONGER_AVAILABLE')
            self.active_receipt=receipt
            options = {}
            if self.followup_claim and self.followup_claim[0] == receipt:
                options['followup'] = self.followup_claim[1]
            self.followup_claim = None
            self.active_followup = options.get('followup')
            try:
                return self._start(self.bridge, receipt, comment, viewer, **options)
            except Exception:
                self.active_receipt=None
                raise

    def _start(self, bridge, receipt, text, viewer, *, prefetched=False, followup=None):
        def bind(token):
            if not self.inbox.bind(receipt,token['turn_id'],prefetched=prefetched):
                raise ValueError('COMMENT_NO_LONGER_AVAILABLE')

        def terminal(value):
            with self.lock:
                kind = value['kind']
                self.inbox.finish(receipt, {'complete':'generated','error':'failed','held':'held'}[kind],
                                  value.get('code'))
                if kind != 'complete' and self.active_receipt == receipt:
                    self.active_receipt = None
                if kind == 'error' and value.get('code') in (
                        'PROVIDER_HTTP_401','PROVIDER_HTTP_402','PROVIDER_HTTP_403',
                        'PROVIDER_HTTP_429','LOCAL_MONTHLY_TTS_LIMIT'):
                    self.paused_reason = value['code']
        return bridge.begin(text,viewer,on_begin=bind,on_terminal=terminal,
                            **({'followup':followup} if followup else {}))

    def estimated_preparation(self):
        cutoff = time.monotonic() - self.s.comment_ttl
        values = sorted(duration for stamp,duration in self.preparation_seconds if stamp >= cutoff)
        if not values:
            return 1.0
        return values[(9*len(values)-1)//10]

    def prepare_viewer(self, configure, remaining_seconds=0):
        with self.lock:
            if (self.prepared or not self.active_receipt or self.paused_reason or self.stop.is_set()
                    or not self.bridge.finished or self.bridge.worker.is_alive()
                    or not self.bridge.fence.current(self.bridge.token)):
                return
            comment = self.inbox.take(prefer_source='live-host' if self.last_source!='live-host' else None,
                                      viewer_only=True,playout_delay=max(remaining_seconds,self.estimated_preparation()))
            if comment is None:
                return
            receipt = comment['receipt']
            branch = self.bridge.next_studio_turn()
            configure(branch, comment)
            started = time.monotonic()
            try:
                token = self._start(branch, receipt, comment['comment'], comment['viewer'], prefetched=True)
            except ValueError as exc:
                if str(exc) != 'COMMENT_NO_LONGER_AVAILABLE':
                    raise
                self.inbox.finish(receipt, 'expired')
                return
            self.prepared = PreparedReply(comment, branch, started, token)

    def discard_prepared(self, reason):
        with self.lock:
            pending = self.prepared
            if pending and not pending.discarded:
                pending.discarded = True
                pending.prefix = None
                pending.bridge.cancel()
                status = 'expired' if reason == 'queue_expired' else 'cancelled'
                self.inbox.finish(pending.comment['receipt'], status, reason)

    def reap_prepared(self):
        with self.lock:
            pending = self.prepared
            if pending and pending.discarded and not pending.bridge.worker.is_alive():
                for path in pending.bridge.controller.output.glob(pending.token['turn_id'] + '-*'):
                    path.unlink(missing_ok=True)
                self.prepared = None

    def promote(self):
        with self.lock:
            pending = self.prepared
            if pending is None or pending.discarded or self.active_receipt:
                return None
            receipt = pending.comment['receipt']
            if self.paused_reason or not self.inbox.playable(receipt):
                self.discard_prepared('queue_expired' if self.inbox.clock() >= pending.comment['expires']
                                      else 'unavailable_before_playout')
                return None
            with self.bridge.lock:
                self.bridge.fence.cancel()
                self.bridge.controller.program_pcm = None
            self.bridge = pending.bridge
            self.active_receipt = receipt
            self.last_source = pending.comment['source']
            self.prepared = None
            return pending

    def played(self,token,*,program_latency=None):
        with self.lock:
            accepted=self.bridge.mark_played(token, program_latency=program_latency)
            if accepted and self.active_receipt:
                self.inbox.finish(self.active_receipt,'played');self.active_receipt=None
                self.active_followup=None
                if self.s.comment_source=='memory-fixture':self._memory_fixture_next()
            return accepted

    def followup_available(self):
        thread = self.active_followup
        return self.inbox.store.can_initiate(thread,self.s.mode)

    def check_removed(self):
        with self.lock:
            if self.prepared and self.inbox.removed_turn(self.prepared.token['turn_id']):
                self.discard_prepared('source_deleted')
            if self.active_receipt and self.bridge.token and (self.inbox.removed_turn(self.bridge.token['turn_id'])
                    or self.active_followup and not self.followup_available()):
                self.cancel()

    def reap_initiative(self, *, closing=False):
        old = self.retiring_initiative
        if old is None:
            return
        if closing:
            old.worker.join(timeout=self.s.turn_timeout + self.s.timeout + 1)
        if not old.worker.is_alive():
            for path in old.controller.output.glob(old.token['turn_id']+'-*'):
                path.unlink(missing_ok=True)
            self.retiring_initiative = None

    def cancel(self, reason=None, *, keep_prepared=False):
        with self.lock:
            if not keep_prepared:
                self.discard_prepared(reason or 'interrupted')
            retire = (self.active_followup is not None and getattr(self.bridge.controller,'initiative_audio_limit',None) is not None
                      and reason in ('human_input_priority','host_interrupt') and self.prepared is None
                      and self.retiring_initiative is None and self.bridge.worker and self.bridge.worker.is_alive())
            self.followup_claim=self.active_followup=None
            self.bridge.cancel()
            if retire:
                old = self.bridge
                self.bridge = old.next_studio_turn(cancelled_initiative=True)
                self.retiring_initiative = old
            if self.active_receipt:self.inbox.finish(self.active_receipt,'expired' if reason=='queue_expired' else 'cancelled',reason);self.active_receipt=None

    def close(self):
        self.stop.set();self.cancel('session_closed')
        if self.prepared:
            self.prepared.bridge.worker.join(timeout=self.s.turn_timeout + self.s.timeout + 1)
            self.reap_prepared()
        if self.thread:self.thread.join(timeout=.25)
        self.reap_initiative(closing=True)
        self.inbox.close()
        self.bridge.controller.memory_worker.close()
