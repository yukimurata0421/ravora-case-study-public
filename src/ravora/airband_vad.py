"""RMS VAD and sample-based time mapping adapted from airband_ai.

Source: yukimurata0421/airband_ai, 3cbfacca54554f4ae2229a23dcafbbb59e1be41a,
scripts/vad_filter.py (upstream readme declares MIT). See NOTICE.md.
Keep 30/15 ms analysis, percentile noise estimate, margin and gap merge.
For conversation, merge before minimum-length filtering and add edge padding.
"""
from __future__ import annotations
from dataclasses import dataclass
import math

SAMPLE_RATE=16000
FRAME_MS=30
HOP_MS=15


@dataclass(frozen=True)
class VadConfig:
    noise_percentile: float = 20.
    margin_db: float = 6.
    floor_db: float = -50.
    min_speech_ms: int = 200
    silence_ms: int = 300
    pad_ms: int = 150
    min_output_ms: int = 500

    def validate(self):
        values=(self.noise_percentile,self.margin_db,self.floor_db)
        if not all(type(v) in (int,float) and math.isfinite(v) for v in values):raise ValueError('INVALID_VAD_THRESHOLDS')
        if not (5<=self.noise_percentile<=40 and 3<=self.margin_db<=15 and -70<=self.floor_db<=-25):raise ValueError('INVALID_VAD_THRESHOLDS')
        if any(type(v) is not int for v in (self.min_speech_ms,self.silence_ms,self.pad_ms,self.min_output_ms)) or not (100<=self.min_speech_ms<=600 and 150<=self.silence_ms<=1000 and 50<=self.pad_ms<=300 and 200<=self.min_output_ms<=1000):raise ValueError('INVALID_VAD_DURATIONS')
        return self


def frame_rms_db(waveform):
    import numpy as np
    # Same framing and RMS epsilon as the upstream _compute_frame_rms_db.
    frame_size=SAMPLE_RATE*FRAME_MS//1000;hop_size=SAMPLE_RATE*HOP_MS//1000
    if len(waveform)<frame_size:return np.array([],dtype=np.float32)
    count=1+(len(waveform)-frame_size)//hop_size
    values=[]
    for i in range(count):
        frame=waveform[i*hop_size:i*hop_size+frame_size]
        rms=np.sqrt(np.mean(frame**2)+1e-12);values.append(20*np.log10(rms+1e-12))
    return np.array(values,dtype=np.float32)


@dataclass(frozen=True)
class VadAudio:
    audio: np.ndarray
    # [original start, original end, compact start, compact end], all PCM samples.
    mapping: tuple
    diagnostics: dict

    def original_time(self,seconds,*,end=False):
        if not self.mapping:return 0.
        offset=max(0,min(len(self.audio),int(round(seconds*SAMPLE_RATE))))
        for a,b,c,d in self.mapping:
            if offset<d or (end and offset==d):return (a+max(0,offset-c))/SAMPLE_RATE
        return self.mapping[-1][1]/SAMPLE_RATE


def analyze(waveform,config=VadConfig()):
    import numpy as np
    config.validate();waveform=np.asarray(waveform,dtype=np.float32)
    if waveform.ndim!=1 or not np.all(np.isfinite(waveform)):raise ValueError('INVALID_VAD_PCM')
    levels=frame_rms_db(waveform);total=len(waveform);regions=[]
    noise=float(np.percentile(levels,config.noise_percentile)) if levels.size else -120.
    threshold=max(config.floor_db,noise+config.margin_db)
    hop=SAMPLE_RATE*HOP_MS//1000;frame=SAMPLE_RATE*FRAME_MS//1000;start=None
    for i,flag in enumerate(levels>=threshold):
        if flag and start is None:start=i*hop
        elif not flag and start is not None:
            regions.append((start,min(total,(i-1)*hop+frame)));start=None
    if start is not None:regions.append((start,total))
    merged=[];gap=SAMPLE_RATE*config.silence_ms//1000
    for a,b in regions:
        if merged and a-merged[-1][1]<=gap:merged[-1]=(merged[-1][0],b)
        else:merged.append((a,b))
    minimum=SAMPLE_RATE*config.min_speech_ms//1000;pad=SAMPLE_RATE*config.pad_ms//1000
    selected=[]
    for a,b in merged:
        if b-a<minimum:continue
        a=max(0,a-pad);b=min(total,b+pad)
        if selected and a<=selected[-1][1]:selected[-1]=(selected[-1][0],b)
        else:selected.append((a,b))
    size=sum(b-a for a,b in selected)
    if size<SAMPLE_RATE*config.min_output_ms//1000:selected=[]
    chunks=[];mapping=[];offset=0
    for a,b in selected:
        chunks.append(waveform[a:b]);mapping.append((a,b,offset,offset+b-a));offset+=b-a
    audio=np.concatenate(chunks) if chunks else np.empty(0,dtype=np.float32)
    diagnostics={'algorithm':'airband-rms-conversation-v1','frame_ms':FRAME_MS,'hop_ms':HOP_MS,
        'noise_percentile':config.noise_percentile,'noise_floor_dbfs':round(noise,3),'margin_db':config.margin_db,
        'floor_dbfs':config.floor_db,'threshold_dbfs':round(threshold,3),'min_speech_ms':config.min_speech_ms,
        'silence_ms':config.silence_ms,'pad_ms':config.pad_ms,'min_output_ms':config.min_output_ms,
        'original_seconds':round(total/SAMPLE_RATE,6),'selected_seconds':round(len(audio)/SAMPLE_RATE,6),
        'regions':[{'start':a/SAMPLE_RATE,'end':b/SAMPLE_RATE,'compact_start':c/SAMPLE_RATE,'compact_end':d/SAMPLE_RATE} for a,b,c,d in mapping]}
    return VadAudio(audio,tuple(mapping),diagnostics)
