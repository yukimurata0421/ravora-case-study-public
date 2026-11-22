"""One presentation clock for HEVC video, burned-in captions and AAC program audio."""
from dataclasses import dataclass
from fractions import Fraction
import io
import re
import ssl
from bisect import bisect_right
import threading

import av
import numpy as np

from ravora.live_overlay import AVATAR_X, ChatLine, Overlay, SIZE, lines, typeface

FPS = 30
RATE = 48000
SAMPLES = RATE // FPS
SILENCE = bytes(SAMPLES * 2)


DEFAULT_INGEST = 'rtmps://a.rtmps.youtube.com:443/live2'


def publish_target(base, stream_key):
    base = base.rstrip('/')
    if not re.fullmatch(r'rtmps://[ab]\.rtmps\.youtube\.com(?::443)?/live2', base):
        raise ValueError('INVALID_YOUTUBE_RTMPS_URL')
    if not re.fullmatch(r'[A-Za-z0-9_-]{8,128}', stream_key):
        raise ValueError('MISSING_OR_INVALID_YOUTUBE_STREAM_KEY')
    return base + '/' + stream_key


def decode_wav(data):
    """Decode only locally generated or validated WAV, capped at one speech segment."""
    if not 0 < len(data) <= 12 * 1024 ** 2:
        raise ValueError('INVALID_PROGRAM_AUDIO')
    parts = bytearray()
    convert = av.AudioResampler(format='s16', layout='mono', rate=RATE)
    with av.open(io.BytesIO(data), format='wav') as source:
        for frame in source.decode(audio=0):
            for converted in convert.resample(frame):
                parts.extend(converted.to_ndarray().tobytes())
            if len(parts) > 60 * RATE * 2:
                raise ValueError('PROGRAM_AUDIO_TOO_LONG')
        for converted in convert.resample(None):
            parts.extend(converted.to_ndarray().tobytes())
    if not 0 < len(parts) <= 60 * RATE * 2:
        raise ValueError('INVALID_PROGRAM_AUDIO')
    return bytes(parts)


@dataclass(frozen=True)
class Stage:
    speaker: str = ''
    caption: str = ''
    expression: str = 'neutral'
    motion: str = 'none'

    chat: tuple[ChatLine, ...] = ()
    selected: int = -1
    thinking: bool = False
    replying: bool = False
    previous: str = ''
    highlight: tuple[int,int] = (-1,-1)
    mouth: str = 'closed'
    host_caption: str = ''


class Clip:
    def __init__(self, speaker, text, data, *, mouth_plan=(), word_starts=()):
        self.speaker = speaker
        self.pcm = decode_wav(data)
        text = ' '.join(text.split())
        wrapped = lines(text,typeface(text,38,True),1170)
        self.pages = ['\n'.join(wrapped[i:i+2]) for i in range(0,len(wrapped),2)] or ['']
        self.position = 0
        self.words = []
        for page, caption in enumerate(self.pages):
            for match in re.finditer(r"[a-zA-Z0-9_'’.-]+[^\w\s]*|[^\s]",caption):
                self.words.append((page,match.start(),match.end()))
        # Timestamps from the original synthesizer take priority. For unmarked
        # audio, distribute the text across voiced PCM windows (including pauses).
        self.starts = list(word_starts)
        duration = len(self.pcm)/(2*RATE)
        if (len(self.starts)!=len(self.words) or not self.starts or
                any(not isinstance(t,(int,float)) or not 0<=t<=duration for t in self.starts)
                or self.starts!=sorted(self.starts)):
            samples=np.frombuffer(self.pcm,dtype='<i2')
            windows=np.pad(samples,(0,(-len(samples))%SAMPLES)).reshape(-1,SAMPLES)
            voiced=np.flatnonzero(np.max(np.abs(windows.astype(np.int32)),axis=1)>300)
            if not len(voiced):
                voiced=np.arange(len(windows))
            weights=np.array([max(2,end-start) for _,start,end in self.words],dtype=float)
            fractions=(weights.cumsum()-weights)/max(1,weights.sum())
            self.starts=(voiced[(fractions*len(voiced)).astype(int)]/FPS).tolist()
        self.plan=[key for key in mouth_plan if key.get('shape') in ('A','I','U','E','O','closed')]
        self.plan_times=[key['start'] for key in self.plan]
        self.highlight = (0, 0)
        self.mouth = 'closed'

    def take(self):
        start = self.position
        seconds = start / (2 * RATE)
        page = 0
        word = bisect_right(self.starts, seconds) - 1
        if word >= 0:
            page, first, end = self.words[word]
            self.highlight = (first, end)
        self.position = min(start + len(SILENCE), len(self.pcm))
        pcm = self.pcm[start:self.position].ljust(len(SILENCE), b'\0')
        if self.speaker == 'Ray':
            if self.plan:
                i = bisect_right(self.plan_times, seconds) - 1
                self.mouth = self.plan[i]['shape'] if i >= 0 and seconds < self.plan[i]['end'] else 'closed'
            else:
                loudness = np.mean(np.abs(np.frombuffer(pcm, dtype='<i2').astype(np.int32)))
                self.mouth = 'A' if loudness > 1800 else 'E' if loudness > 400 else 'closed'
        return self.pages[page], pcm

    @property
    def done(self):
        return self.position==len(self.pcm)


class Scene:
    def __init__(self, directory=None, *, live=False):
        self.overlay=Overlay(directory,live=live)
        self.avatar = self.clip = self.portrait = None
        self.tick = 0
        self.last=self.frame=self.image=None
        self.revision=0
        self._jpeg=None
        self.lock=threading.Lock()

    def avatar_background(self):
        buffer = io.BytesIO()
        base = self.overlay.backgrounds['neutral']
        base.crop((AVATAR_X, 0, AVATAR_X+1080, 1080)).save(buffer, format='JPEG', quality=97)
        return buffer.getvalue()

    def render(self, stage):
        portrait = self.avatar.render(self.tick, stage, self.clip) if self.avatar else None
        self.tick += 1
        if portrait is not None:
            self.portrait = portrait
        if stage!=self.last or portrait is not None:
            image=self.overlay.compose(stage, self.portrait)
            frame=av.VideoFrame.from_image(image).reformat(format='yuv420p',dst_colorspace='ITU709')
            with self.lock:
                self.last,self.image,self.frame=stage,image,frame
                self.revision+=1
                self._jpeg=None
        return self.frame

    def preview(self, after=None):
        # JPEG encoding is on the HTTP thread. No browser work on the media clock.
        with self.lock:
            revision,image,cached=self.revision,self.image,self._jpeg
        if image is None or str(revision)==after:
            return revision, None
        if cached is not None:
            return revision, cached
        buffer=io.BytesIO()
        image.save(buffer,format='JPEG',quality=86,subsampling=0)
        data=buffer.getvalue()
        with self.lock:
            if self.revision==revision:
                self._jpeg=data
        return revision, data


class ProgramWriter:
    def __init__(self, target, scene, *, publish=False):
        self.scene = scene
        self.frames = 0
        options = {'flvflags': 'no_duration_filesize'} if publish else {}
        if publish:
            options.update(tls_verify='1', ca_file=ssl.get_default_verify_paths().cafile, rw_timeout='5000000', rtmp_enhanced_codecs='hvc1')
        self.output = av.open(str(target), 'w', format='flv', options=options, timeout=(6, 5))
        try:
            self.video = self.output.add_stream('libx265', rate=FPS)
            self.video.width, self.video.height = SIZE
            self.video.pix_fmt = 'yuv420p'
            self.video.codec_context.thread_count = 3
            self.video.bit_rate = 6_000_000
            self.video.options = {
                'preset': 'ultrafast',
                'x265-params': 'pools=3:frame-threads=3:rc-lookahead=0:qpmin=22:ctu=64:min-cu-size=32:me=dia:merange=8:rd=1:subme=0:max-merge=1:sao=0:deblock=0:keyint=60:min-keyint=60:scenecut=0:bframes=0:'
                               'vbv-maxrate=6000:vbv-bufsize=12000:strict-cbr=1:colorprim=bt709:transfer=bt709:colormatrix=bt709:repeat-headers=1:log-level=error',
            }
            self.audio = self.output.add_stream('aac', rate=RATE)
            self.audio.layout = 'stereo'
            self.audio.bit_rate = 128000
            self.output.start_encoding()
        except BaseException:
            self.output.close()
            raise

    def write(self, stage, pcm):
        frame = self.scene.render(stage)
        frame.pts = self.frames
        frame.time_base = Fraction(1, FPS)
        stereo = np.repeat(np.frombuffer(pcm, dtype='<i2'), 2).reshape(1, -1)
        sound = av.AudioFrame.from_ndarray(stereo, format='s16', layout='stereo')
        sound.sample_rate = RATE
        sound.pts = self.frames * SAMPLES
        sound.time_base = Fraction(1, RATE)
        for stream, item in ((self.video, frame), (self.audio, sound)):
            for packet in stream.encode(item):
                self.output.mux(packet)
        self.frames += 1

    def close(self):
        try:
            for stream in (self.video, self.audio):
                for packet in stream.encode(None):
                    self.output.mux(packet)
        finally:
            self.output.close()
