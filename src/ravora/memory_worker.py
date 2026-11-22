"""One bounded local worker. Only already-played, source-backed jobs are eligible."""
import threading
import time
import queue

from ravora.memory_extraction import LocalPreferenceExtractor,POLICY
from ravora.gemini_memory import GeminiMemoryExtractor,GeminiExtraction,GEMINI_POLICY,source_block_reason
from ravora.providers import ProviderError
from ravora.semantic_memory import EPISODE_POLICY
from ravora.ray_memory import CharacterMemory
from ravora.ray_character import owner_hash


class MemoryWorker:
    def __init__(self,store,settings,*,extractor=None):
        self.store=store;self.s=settings
        name=settings.memory_extractor
        if name=='auto':
            remote=settings.mode=='cloud' and (settings.comment_source!='developer' or settings.memory_api_enabled)
            name='gemini' if remote else 'local_rules'
        self.extractor=extractor or (GeminiMemoryExtractor(settings) if name=='gemini' else LocalPreferenceExtractor())
        self.policy=getattr(self.extractor,'policy',GEMINI_POLICY if name=='gemini' else POLICY)
        if self.policy==GEMINI_POLICY and settings.semantic_memory_mode!='off':self.policy=EPISODE_POLICY
        self.remote=self.policy in (GEMINI_POLICY,EPISODE_POLICY)
        self.model=(settings.memory_model or settings.gemini_model) if self.remote else None
        self.policies=(POLICY,GEMINI_POLICY,EPISODE_POLICY) if self.remote else (POLICY,)
        self.stop=threading.Event();self.lock=threading.Lock();self.thread=None;self.signalled=False
        self.last_error=None;self.active_viewer=None
        self.character=CharacterMemory(store,settings.mode) if settings.character_enabled else None
        self.character_queue=queue.Queue(maxsize=16)
        self.character_cache=()
        self.character_counts={'reads':0,'saves':0,'save_failures':0,'read_failures':0,'queue_dropped':0}
        self.character_ready=threading.Event()

    def character_memories(self,viewer):
        owner=owner_hash(self.s.mode,viewer)
        with self.lock:return tuple(dict(m) for m in self.character_cache if m['owner_hash'] in ('',owner))

    def offer_character(self,turn_id):
        if self.character is None or self.stop.is_set():return
        try:self.character_queue.put_nowait(('promote',turn_id))
        except queue.Full:
            with self.lock:self.character_counts['queue_dropped']+=1
            return
        self.wake()

    def character_referenced(self,memories):
        if self.character is None or not memories:return
        try:self.character_queue.put_nowait(('reference',tuple(m['id'] for m in memories[:8])))
        except queue.Full:
            with self.lock:self.character_counts['queue_dropped']+=1
        self.wake()

    def _character_reload(self):
        if self.character is None:return
        try:
            loaded=self.character.load()
            with self.lock:
                if not self.stop.is_set():self.character_cache=loaded
                self.character_counts['reads']+=1
        except Exception:
            with self.lock:self.character_counts['read_failures']+=1
        finally:self.character_ready.set()

    def _character_step(self):
        try:kind,value=self.character_queue.get_nowait()
        except queue.Empty:return False
        try:
            if kind=='promote':
                count=self.character.promote(value)
                with self.lock:self.character_counts['saves']+=count
                if count:self._character_reload()
            else:self.character.referenced(value)
        except Exception:
            with self.lock:self.character_counts['save_failures']+=1
        finally:self.character_queue.task_done()
        return True

    def wake(self):
        if not (self.s.memory_enabled or self.character is not None) or self.stop.is_set():return
        with self.lock:
            self.signalled=True
            if self.thread and self.thread.is_alive():return
            self.thread=threading.Thread(target=self._run,name='ravora-memory',daemon=True)
            self.thread.start()

    def _run(self):
        try:
            self._character_reload()
            # Queue capacity is <=32. Exit when idle instead of leaving an idle thread.
            for _ in range(32):
                if self.stop.is_set():return
                with self.lock:self.signalled=False
                character_work=self._character_step()
                job=self.store.claim_memory_job(self.s.mode) if self.s.memory_enabled else None
                if not job and self.remote and self.s.memory_enabled:
                    job=self.store.claim_memory_job(self.s.mode,policy=GEMINI_POLICY,
                        lease_seconds=max(15,self.s.memory_api_timeout+10))
                if not job and self.remote and self.s.memory_enabled:
                    job=self.store.claim_memory_job(self.s.mode,policy=EPISODE_POLICY,
                        lease_seconds=max(15,self.s.memory_api_timeout+10))
                if not job:
                    with self.lock:
                        if self.signalled or character_work or not self.character_queue.empty():continue
                        self.thread=None;return
                with self.lock:self.active_viewer=job['viewer_key']
                try:
                    if job['policy'] in (GEMINI_POLICY,EPISODE_POLICY):
                        self._remote(job);continue
                    local=LocalPreferenceExtractor() if self.remote else self.extractor
                    result=local.extract(job['text'])
                    if self.stop.is_set():self.store.release_memory_job(job);return
                    self.store.complete_memory_job(job,result)
                except Exception:
                    self.store.fail_memory_job(job,'EXTRACTION_FAILED')
                finally:
                    with self.lock:self.active_viewer=None
        except Exception:
            self.last_error='MEMORY_STORAGE_UNAVAILABLE'
        finally:
            with self.lock:
                self.active_viewer=None
                if self.thread is threading.current_thread():self.thread=None

    def wait_for(self,viewer_key,*,cancel=None):
        if not self.s.memory_enabled:return True
        until=time.monotonic()+self.s.memory_wait_seconds
        if self.store.memory_pending(viewer_key,self.s.mode,policies=self.policies):self.wake()
        def pending():
            # Saving the job precedes the final request ledger transaction.
            # Do not announce readiness in that small commit-to-commit window.
            if self.store.memory_pending(viewer_key,self.s.mode,policies=self.policies):return True
            with self.lock:return self.active_viewer==viewer_key
        while pending():
            if self.stop.is_set() or (cancel and cancel.is_set()) or time.monotonic()>=until:return False
            if cancel:cancel.wait(.01)
            else:time.sleep(.01)
        return True

    def _remote(self,job):
        blocked=source_block_reason(job['text'])
        if blocked:
            self.store.complete_memory_job(job,GeminiExtraction(reason=blocked));return
        if job['policy']==EPISODE_POLICY:
            job['prior_candidates']=self.store.revalidate_episode_candidates(
                job['viewer_key'],job['text'],self.s,job.get('prior_candidates',[]))
        key=self.store.prepare_memory_call(job)
        if not key:return
        if not self.s.api_enabled or not self.s.memory_api_enabled or not self.s.gemini_key:
            code='API_DISABLED' if not self.s.api_enabled else 'MEMORY_API_DISABLED' if not self.s.memory_api_enabled else 'MISSING_GEMINI_API_KEY'
            self.store.memory_call_state(job,key,'held',code=code)
            self.store.fail_memory_job(job,code,status='skipped');return
        if self.stop.is_set():
            self.store.memory_call_state(job,key,'cancelled_before_send');self.store.release_memory_job(job);return
        if not self.store.dispatch_memory_call(job,key):
            self.store.memory_call_state(job,key,'cancelled_before_send')
            self.store.fail_memory_job(job,'SOURCE_UNAVAILABLE',status='cancelled');return
        if self.stop.is_set():
            self.store.memory_call_state(job,key,'cancelled_before_send')
            self.store.fail_memory_job(job,'WORKER_STOPPED_AFTER_DISPATCH',status='skipped');return
        try:
            result=self.extractor.extract_job(job)
            self.store.call_usage(key,getattr(self.extractor,'usage',{}))
            self.store.memory_call_state(job,key,'response_received')
            if self.stop.is_set():
                self.store.memory_call_state(job,key,'discarded_after_cancel')
                self.store.fail_memory_job(job,'WORKER_STOPPED_AFTER_SEND',status='skipped');return
            accepted=self.store.complete_memory_job(job,result)
            self.store.memory_call_state(job,key,'accepted' if accepted else 'discarded_after_cancel')
        except Exception as exc:
            code=str(exc) if isinstance(exc,ProviderError) else 'EXTRACTION_FAILED'
            unknown=any(label in code for label in ('CONNECTION_FAILED','DEADLINE','STREAM_INTERRUPTED','TIMEOUT'))
            self.store.call_usage(key,getattr(self.extractor,'usage',{}))
            self.store.memory_call_state(job,key,'outcome_unknown' if unknown else 'failed',code=code)
            self.store.fail_memory_job(job,'OUTCOME_UNKNOWN' if unknown else code)

    def close(self):
        self.stop.set()
        with self.lock:thread=self.thread
        if thread and thread is not threading.current_thread():thread.join(timeout=1)
        with self.lock:self.character_cache=()
        while True:
            try:self.character_queue.get_nowait();self.character_queue.task_done()
            except queue.Empty:break

    def snapshot(self):
        with self.store.connect() as db:
            counts=dict(db.execute('SELECT status,COUNT(*) FROM memory_jobs WHERE scope=? GROUP BY status',(self.s.mode,)))
            attempted=db.execute('''SELECT COALESCE(SUM(p.attempted),0) FROM provider_calls p
                JOIN memory_jobs j ON j.api_call_id=p.id WHERE j.scope=?''',(self.s.mode,)).fetchone()[0]
        with self.lock:character_counts=dict(self.character_counts)
        return {'enabled':self.s.memory_enabled,'extractor':'gemini' if self.remote else 'local_rules',
                'character':{'enabled':self.character is not None,**character_counts,'pending':self.character_queue.qsize()},
                'provider_requests_attempted':attempted,'jobs':counts,'scope':self.s.mode,'last_error':self.last_error,
                'thread_running':bool(self.thread and self.thread.is_alive())}
