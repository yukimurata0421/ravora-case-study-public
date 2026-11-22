"""Comment reception -> inbox -> turn preparation -> browser playback acknowledgement."""
import threading
import time
import uuid

from ravora.comments import Comment,CommentInbox,fixture_page
from ravora.ray_character import initiative_message
from ravora.youtube import YouTubeReader


class InputPipeline:
    def __init__(self,settings,store,bridge,fixtures,*,memory_fixture_comments=None):
        self.s=settings;self.bridge=bridge;self.inbox=CommentInbox(store,capacity=settings.comment_queue_size,ttl=settings.comment_ttl,
            scope=settings.mode,relationships_enabled=settings.memory_enabled and settings.relationship_enabled)
        self.fixtures=fixtures;self.stop=threading.Event();self.reader=None;self.thread=None
        self.last_source=None
        self.active_receipt=None;self.paused_reason=None;self.lock=threading.RLock();self.source_done=False
        self.memory_fixture_comments=memory_fixture_comments or ('I like coffee and jazz.','Do you remember my favorite drink?')
        if len(self.memory_fixture_comments)!=2 or any(not isinstance(x,str) or not 0<len(x)<=1000 for x in self.memory_fixture_comments):raise ValueError('INVALID_MEMORY_FIXTURE')

    def start(self,seconds,*,wait_for_chat=False,access_token=None):
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
        else:
            self.reader=YouTubeReader(self.s,self.inbox,on_page=self.check_removed,access_token=access_token)
            def run():
                try:self.reader.run(self.stop,seconds=seconds,wait_for_chat=wait_for_chat)
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

    def next(self, *, allow_host_input=False):
        with self.lock:
            if self.paused_reason:return {'available':False,'finished':True,'held':True,'code':self.paused_reason}
            if not allow_host_input and self.reader and self.reader.status=='failed':return {'available':False,'finished':True,'error':self.reader.code}
            if self.active_receipt or (self.bridge.worker and self.bridge.worker.is_alive()):return {'available':False,'finished':False}
            prefer='live-host' if allow_host_input and self.last_source!='live-host' else None
            comment=self.inbox.take(prefer_source=prefer)
            if comment:
                self.last_source=comment['source']
                return {'available':True,'finished':False,'comment':comment}
            character=getattr(self.bridge.controller,'character',None)
            if character and self.s.character_initiative_enabled and self.s.comment_source not in ('fixture','memory-fixture','developer'):
                topic=character.idle_topic(self.bridge.controller.memory_worker.character_memories(self.bridge.viewer or ''))
                if topic is not None:
                    initiative=Comment('ray-initiative',self.inbox.session,uuid.uuid4().hex,'ray','Ray',
                        initiative_message(topic),time.time())
                    if self.inbox.put(initiative)=='queued':
                        comment=self.inbox.take()
                        if comment:return {'available':True,'finished':False,'comment':comment}
            held=bool(self.reader and self.reader.status=='held')
            return {'available':False,'finished':self.source_done,'held':held,'code':self.reader.code if held else None}

    def begin(self,receipt):
        if not isinstance(receipt,str) or len(receipt)!=64:raise ValueError('INVALID_COMMENT_RECEIPT')
        with self.lock:
            if self.paused_reason:raise ValueError('INPUT_PAUSED')
            comment,viewer=self.inbox.claimed(receipt)
            if self.active_receipt:raise ValueError('TURN_BUSY')
            self.active_receipt=receipt
            def bind(token):
                if not self.inbox.bind(receipt,token['turn_id']):raise ValueError('COMMENT_NO_LONGER_AVAILABLE')
            def terminal(value):
                with self.lock:
                    kind=value['kind'];status={'complete':'generated','error':'failed','held':'held'}[kind]
                    self.inbox.finish(receipt,status,value.get('code'))
                    if kind!='complete':self.active_receipt=None
                    if kind=='error' and value.get('code') in ('PROVIDER_HTTP_401','PROVIDER_HTTP_402','PROVIDER_HTTP_403','PROVIDER_HTTP_429','LOCAL_MONTHLY_TTS_LIMIT'):
                        self.paused_reason=value['code']
            try:return self.bridge.begin(comment,viewer,on_begin=bind,on_terminal=terminal)
            except Exception:
                self.active_receipt=None;raise

    def played(self,token):
        with self.lock:
            accepted=self.bridge.mark_played(token)
            if accepted and self.active_receipt:
                self.inbox.finish(self.active_receipt,'played');self.active_receipt=None
                if self.s.comment_source=='memory-fixture':self._memory_fixture_next()
            return accepted

    def check_removed(self):
        with self.lock:
            if self.active_receipt and self.bridge.token and self.inbox.removed_turn(self.bridge.token['turn_id']):
                self.bridge.cancel();self.active_receipt=None

    def cancel(self):
        with self.lock:
            self.bridge.cancel()
            if self.active_receipt:self.inbox.finish(self.active_receipt,'cancelled');self.active_receipt=None

    def close(self):
        self.stop.set();self.cancel()
        if self.thread:self.thread.join(timeout=.25)
        self.inbox.close()
        self.bridge.controller.memory_worker.close()
