"""Offline CPU STT in one owned process, with cancellation and a hard deadline."""
import hashlib
import io
import json
import multiprocessing as mp
import os
from pathlib import Path
import threading
import time
import wave

from ravora.providers import ProviderError


def verify_model(directory):
    path=Path(directory)
    path=path.resolve()
    if not path.is_relative_to(Path.cwd()/'assets/stt') or not path.is_dir():raise ProviderError('LOCAL_STT_MODEL_MISSING')
    try:
        manifest=json.loads(Path(__file__).with_name('stt-model.json').read_text())
        for name in ('model.bin','config.json','tokenizer.json','vocabulary.txt'):
            item=manifest['files'][name];file=path/name
            if file.is_symlink() or not file.is_file() or file.stat().st_size!=item['bytes']:raise ValueError()
            # Check the large weight once per worker lifetime, without an unbounded read.
            digest=hashlib.sha256()
            with file.open('rb') as stream:
                while block:=stream.read(1024*1024):digest.update(block)
            if digest.hexdigest()!=item['sha256']:raise ValueError()
        revision=manifest['revision']
        if not isinstance(revision,str) or len(revision)!=40:raise ValueError()
    except (OSError,ValueError,KeyError,TypeError):raise ProviderError('LOCAL_STT_MODEL_INTEGRITY_FAILED') from None
    return path,revision


def _worker(pipe,options):
    # No LLM/TTS credential is passed to this process. Prevent implicit hub fetches.
    for name in list(os.environ):
        if any(part in name for part in ('API_KEY','ACCESS_TOKEN','CREDENTIALS_DIRECTORY','STREAM_KEY')):os.environ.pop(name,None)
    os.environ.update({'HF_HUB_OFFLINE':'1','TRANSFORMERS_OFFLINE':'1','OMP_NUM_THREADS':'1','OPENBLAS_NUM_THREADS':'1','MKL_NUM_THREADS':'1','CUDA_VISIBLE_DEVICES':''})
    try:
        from faster_whisper import WhisperModel
        import numpy as np
        started=time.monotonic();path,revision=verify_model(options['model_path'])
        model=WhisperModel(str(path),device='cpu',compute_type='int8',cpu_threads=1,num_workers=1,local_files_only=True)
        identity={'engine':'faster-whisper','device':'cpu','compute_type':model.model.compute_type,
            'cpu_threads':1,'model_revision':revision,'load_seconds':round(time.monotonic()-started,4)}
        pipe.send({'ready':True,'identity':identity})
        while True:
            request=pipe.recv()
            if request is None:break
            language,pcm=request
            audio=np.frombuffer(pcm,dtype=np.float32).copy();started=time.monotonic()
            generator,info=model.transcribe(audio,language=None if language=='auto' else language,
                beam_size=1,best_of=1,temperature=0.,condition_on_previous_text=False,word_timestamps=True,
                vad_filter=True,vad_parameters={'threshold':options['vad_probability'],'neg_threshold':options['vad_probability']-.15,
                    'min_speech_duration_ms':options['min_speech_ms'],'min_silence_duration_ms':options['silence_ms'],
                    'speech_pad_ms':options['pad_ms'],'max_speech_duration_s':30},
                no_speech_threshold=options['no_speech'],log_prob_threshold=options['log_probability'],
                compression_ratio_threshold=options['compression'],max_new_tokens=256)
            segments=[];rejected=0
            for segment in generator:
                if len(segments)>=128:raise ValueError('segment limit')
                if segment.no_speech_prob>options['no_speech'] or segment.avg_logprob<options['log_probability'] or segment.compression_ratio>options['compression']:
                    rejected+=1;continue
                segments.append({'text':segment.text,'start':segment.start,'end':segment.end,
                    'avg_logprob':segment.avg_logprob,'no_speech_prob':segment.no_speech_prob,'compression_ratio':segment.compression_ratio,
                    'words':[{'word':w.word,'start':w.start,'end':w.end,'probability':w.probability} for w in (segment.words or [])][:256]})
            if info.language_probability<options['language_probability']:segments=[]
            pipe.send({'segments':segments,'language':info.language,'language_probability':info.language_probability,
                'duration_after_vad':info.duration_after_vad,'rejected_segments':rejected,
                'decode_seconds':round(time.monotonic()-started,4),'identity':identity})
    except (EOFError,BrokenPipeError):pass
    except ProviderError as exc:
        try:pipe.send({'error':str(exc)})
        except (EOFError,BrokenPipeError,OSError):pass
    except ModuleNotFoundError:
        try:pipe.send({'error':'LOCAL_STT_DEPENDENCY_MISSING'})
        except (EOFError,BrokenPipeError,OSError):pass
    except Exception:
        try:pipe.send({'error':'LOCAL_STT_ENGINE_FAILED'})
        except (EOFError,BrokenPipeError,OSError):pass
    finally:pipe.close()


class LocalSTT:
    provider='local_cpu'
    model_id='faster-whisper-base-int8'
    def __init__(self,settings):
        self.s=settings;self.usage={};self.process=None;self.pipe=None;self.identity={};self.lock=threading.Lock()

    def _receive(self,deadline,cancel):
        while time.monotonic()<deadline:
            if cancel and cancel.is_set():raise ProviderError('LOCAL_STT_CANCELLED')
            if self.pipe.poll(.05):
                try:value=self.pipe.recv()
                except (OSError,EOFError):raise ProviderError('LOCAL_STT_WORKER_EXITED') from None
                if value.get('error'):raise ProviderError(value['error'])
                return value
            if not self.process.is_alive():raise ProviderError('LOCAL_STT_WORKER_EXITED')
        raise ProviderError('LOCAL_STT_TIMEOUT')

    def _stop(self):
        if self.process:
            if self.process.is_alive():self.process.terminate()
            self.process.join(timeout=1)
            if self.process.is_alive():self.process.kill();self.process.join(timeout=1)
        if self.pipe:self.pipe.close()
        self.pipe=None;self.process=None;self.identity={}

    def transcribe(self,packet,*,cancel=None,language=None):
        from ravora.airband_vad import analyze
        import numpy as np
        language = self.s.stt_language if language is None else language
        if language not in ('ja','en','auto'):
            raise ProviderError('INVALID_STT_LANGUAGE')
        with self.lock:
            started=time.monotonic();deadline=started+self.s.stt_timeout
            with wave.open(io.BytesIO(packet.data)) as wav:audio=np.frombuffer(wav.readframes(wav.getnframes()),dtype='<i2').astype(np.float32)/32768
            vad=analyze(audio,self.s.vad_config())
            diagnostics={'vad':vad.diagnostics,'device':'cpu','compute_type':'int8','cpu_threads':1,'engine_invoked':False}
            self.usage={'response_model':self.model_id,'local_diagnostics':diagnostics}
            if not len(vad.audio):
                diagnostics.update({'elapsed_seconds':round(time.monotonic()-started,4),'reason':'RMS_VAD_NO_SPEECH'})
                return {'text':'','language':'und','speech':False,'segments':[]}
            options={'model_path':self.s.stt_model_path,'vad_probability':self.s.stt_vad_probability,
                'min_speech_ms':self.s.stt_min_speech_ms,'silence_ms':self.s.stt_silence_ms,'pad_ms':self.s.stt_pad_ms,
                'no_speech':self.s.stt_no_speech,'log_probability':self.s.stt_log_probability,
                'compression':self.s.stt_compression,'language_probability':self.s.stt_language_probability}
            try:
                if not self.process or not self.process.is_alive():
                    self._stop();context=mp.get_context('spawn');parent,child=context.Pipe()
                    self.pipe=parent;self.process=context.Process(target=_worker,args=(child,options),name='ravora-local-stt',daemon=True)
                    self.process.start();child.close();ready=self._receive(deadline,cancel);self.identity=ready['identity']
                self.pipe.send((language,vad.audio.tobytes()));value=self._receive(deadline,cancel)
            except (OSError,EOFError):
                self._stop();raise ProviderError('LOCAL_STT_WORKER_EXITED') from None
            except ProviderError:
                self._stop();raise
            segments=[]
            for segment in value['segments']:
                a=vad.original_time(segment['start']);b=vad.original_time(segment['end'],end=True)
                if not (0<=a<b<=packet.duration+.001):raise ProviderError('INVALID_LOCAL_STT_TIMESTAMPS')
                words=[]
                for word in segment['words']:
                    words.append({**word,'start':round(vad.original_time(word['start']),6),'end':round(vad.original_time(word['end'],end=True),6)})
                segments.append({**segment,'start':round(a,6),'end':round(b,6),'words':words})
            text=''.join(s['text'] for s in segments).strip()
            if len(text)>1000:raise ProviderError('LOCAL_TRANSCRIPT_TOO_LONG')
            diagnostics.update({**value['identity'],'engine_invoked':True,'decode_seconds':value['decode_seconds'],'elapsed_seconds':round(time.monotonic()-started,4),
                'language':value['language'],'language_probability':value['language_probability'],'duration_after_vad':value['duration_after_vad'],
                'rejected_segments':value['rejected_segments'],'vad_probability':self.s.stt_vad_probability,'no_speech_threshold':self.s.stt_no_speech,
                'log_probability_threshold':self.s.stt_log_probability,'compression_ratio_threshold':self.s.stt_compression,
                # Keep timing/confidence evidence without duplicating words after source expiry.
                'segments':[{k:([{x:y for x,y in w.items() if x!='word'} for w in v] if k=='words' else v) for k,v in s.items() if k!='text'} for s in segments]})
            return {'text':text,'language':value['language'] if text else 'und','speech':bool(text),'segments':segments}

    def close(self):
        with self.lock:
            if self.pipe and self.process and self.process.is_alive():
                try:
                    self.pipe.send(None)
                    self.process.join(timeout=1)
                except (BrokenPipeError, EOFError, OSError):
                    pass
            self._stop()
