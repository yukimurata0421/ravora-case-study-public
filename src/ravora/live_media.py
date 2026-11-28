"""One presentation clock for video, burned-in captions and AAC program audio."""
from dataclasses import dataclass
from fractions import Fraction
import io
import re
from bisect import bisect_right
import threading

import av
import numpy as np

from ravora.live_overlay import AVATAR_X, ChatLine, Overlay, SIZE, caption_layout

FPS = 30
RATE = 48000
SAMPLES = RATE // FPS
SILENCE = bytes(SAMPLES * 2)


DEFAULT_INGEST = 'rtmps://a.rtmps.youtube.com:443/live2'
TWITCH_INGEST = 'rtmps://ingest.global-contribute.live-video.net/app'


def validate_video_encoder(name, *, twitch=False):
    encoders = ('libx264', 'h264_nvenc') if twitch else ('libx265', 'hevc_nvenc')
    if name not in encoders:
        raise ValueError('INVALID_PROGRAM_VIDEO_ENCODER')
    return name


def publish_target(base, stream_key):
    base = base.rstrip('/')
    if not re.fullmatch(r'rtmps://[ab]\.rtmps\.youtube\.com(?::443)?/live2', base):
        raise ValueError('INVALID_YOUTUBE_RTMPS_URL')
    if not re.fullmatch(r'[A-Za-z0-9_-]{8,128}', stream_key):
        raise ValueError('MISSING_OR_INVALID_YOUTUBE_STREAM_KEY')
    return base + '/' + stream_key


def twitch_publish_target(base, stream_key):
    base = base.rstrip('/')
    if not re.fullmatch(
        r'rtmps://(?:ingest\.global-contribute|[a-z0-9-]+\.contribute)\.live-video\.net(?::443)?/app',
        base,
    ):
        raise ValueError('INVALID_TWITCH_RTMPS_URL')
    if not re.fullmatch(r'[A-Za-z0-9_-]{8,128}', stream_key):
        raise ValueError('MISSING_OR_INVALID_TWITCH_STREAM_KEY')
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
    caption_font: tuple[int, bool] = (38, False)  # pixel size and CJK face used to wrap this page
    prompt_title: str = ''
    language: str = 'en'


def caption_pages(text):
    return list(caption_layout(text)[1])


class Clip:
    def __init__(self, speaker, text, data=None, *, mouth_plan=(), word_starts=(), pcm=None, layout=None):
        self.speaker = speaker
        self.pcm = decode_wav(data) if pcm is None else pcm
        self.playout_pcm = self.pcm
        self.caption_font, self.pages = layout or caption_layout(text)
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
        self.position = min(start + len(SILENCE), len(self.playout_pcm))
        pcm = self.playout_pcm[start:self.position].ljust(len(SILENCE), b'\0')
        if self.speaker == 'Ray':
            if self.plan:
                i = bisect_right(self.plan_times, seconds) - 1
                self.mouth = self.plan[i]['shape'] if i >= 0 and seconds < self.plan[i]['end'] else 'closed'
            else:
                voiced = np.frombuffer(self.pcm[start:self.position], dtype='<i2')
                loudness = np.mean(np.abs(voiced.astype(np.int32))) if voiced.size else 0
                self.mouth = 'A' if loudness > 1800 else 'E' if loudness > 400 else 'closed'
        return self.pages[page], pcm

    @property
    def done(self):
        return self.position==len(self.playout_pcm)


class SpeechPrefix:
    """Frame-aligned 48 kHz playout before the complete, claimable WAV is ready."""
    window = 12 * len(SILENCE)  # 400 ms; also an immutable clip for the operator monitor

    def __init__(self, text):
        self.text = text
        self.layout = caption_layout(text)
        self.single_page = len(self.layout[1]) == 1
        self.converter = av.AudioResampler(format='s16', layout='mono', rate=RATE)
        self.pending = bytearray()
        self.consumed = 0

    def feed(self, packet):
        if self.single_page:
            frame = av.AudioFrame.from_ndarray(np.frombuffer(packet, '<i2').reshape(1,-1), format='s16', layout='mono')
            frame.sample_rate = 24000
            for converted in self.converter.resample(frame):
                self.pending.extend(converted.to_ndarray().tobytes())

    def take(self):
        if len(self.pending) < self.window:
            return None
        clip = Clip('Ray', self.text, pcm=bytes(self.pending[:self.window]), layout=self.layout)
        # Word timing needs the complete sentence. Never restart its highlight at each window.
        clip.starts = []
        clip.highlight = (-1,-1)
        del self.pending[:self.window]
        self.consumed += self.window
        return clip


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
    def __init__(self, target, scene, *, publish=False, video_encoder='libx265'):
        twitch = video_encoder in ('libx264', 'h264_nvenc')
        validate_video_encoder(video_encoder, twitch=twitch)
        self.scene = scene
        self.bgm = None
        self.frames = 0
        self.encoded_bytes = 0
        self.twitch = twitch
        self.transport = None
        self.closed = False
        # In live mode this local container owns only the encoder headers. Encoded
        # packets go directly to the FIFO; the network thread only remuxes them.
        self.output = av.open(io.BytesIO() if publish else target, 'w', format='flv')
        self.mux = self.output.mux
        try:
            self.video = self.output.add_stream(video_encoder, rate=FPS)
            self.video.width, self.video.height = SIZE
            self.video.pix_fmt = 'yuv420p'
            bitrate_kbps = 4500 if twitch else 6000
            self.video.bit_rate = bitrate_kbps * 1000
            codec = self.video.codec_context
            if video_encoder in ('hevc_nvenc', 'h264_nvenc'):
                codec.gop_size = 2 * FPS
                codec.max_b_frames = 0
                codec.color_primaries = codec.color_trc = codec.colorspace = 1  # BT.709
                self.video.options = {
                    'preset': 'p4', 'tune': 'll', 'rc': 'cbr', 'cbr_padding': '1',
                    'maxrate': str(self.video.bit_rate), 'bufsize': str(self.video.bit_rate * 2), 'rc-lookahead': '0',
                    'no-scenecut': '1', 'strict_gop': '1', 'zerolatency': '1', 'delay': '0',
                }
                if twitch:
                    self.video.options = {**self.video.options, 'profile': 'high'}
            elif twitch:
                codec.thread_count = 3
                self.video.options = {
                    'preset': 'veryfast', 'tune': 'zerolatency', 'profile': 'high',
                    'x264-params': f'keyint={2 * FPS}:min-keyint={2 * FPS}:scenecut=0:bframes=0:'
                                   f'vbv-maxrate={bitrate_kbps}:vbv-bufsize={2 * bitrate_kbps}:nal-hrd=cbr:'
                                   'colorprim=bt709:transfer=bt709:colormatrix=bt709',
                }
            else:
                codec.thread_count = 3
                self.video.options = {
                    'preset': 'ultrafast',
                    'x265-params': 'pools=3:frame-threads=3:rc-lookahead=0:qpmin=22:ctu=64:min-cu-size=32:me=dia:merange=8:rd=1:subme=0:max-merge=1:sao=0:deblock=0:keyint=60:min-keyint=60:scenecut=0:bframes=0:'
                                   'vbv-maxrate=6000:vbv-bufsize=12000:strict-cbr=1:colorprim=bt709:transfer=bt709:colormatrix=bt709:repeat-headers=1:log-level=error',
                }
            if publish:
                self.video.options = {**self.video.options, 'forced-idr': '1'}
            self.audio = self.output.add_stream('aac', rate=RATE)
            self.audio.layout = 'stereo'
            self.audio.bit_rate = 128000
            self.output.start_encoding()
            if publish and target is not None:
                self.start_publish(target)
        except BaseException:
            self.output.close()
            raise

    def start_publish(self, target):
        from ravora.live_transport import PublishFIFO, rtmps_output
        if self.transport is not None or self.closed:
            raise RuntimeError('PROGRAM_PUBLISH_ALREADY_STARTED_OR_CLOSED')
        streams = (self.video, self.audio)
        from ravora.twitch_ack import TwitchPeer
        self.transport = PublishFIFO(lambda: rtmps_output(target, streams),
                                     recovery_seconds=75 if self.twitch else None,
                                     peer=TwitchPeer(target) if self.twitch else None)
        self.mux = self.transport.put

    def write(self, stage, pcm):
        frame = self.scene.render(stage)
        if self.transport:
            # Scene can return the same frame on the next tick. Clear a previous I request.
            frame.pict_type = (av.video.frame.PictureType.I if self.transport.take_keyframe_request()
                               else av.video.frame.PictureType.NONE)
        frame.pts = self.frames
        frame.time_base = Fraction(1, FPS)
        voice = np.frombuffer(pcm, dtype='<i2')
        stereo = self.bgm.mix(voice) if self.bgm is not None else np.repeat(voice, 2).reshape(1, -1)
        sound = av.AudioFrame.from_ndarray(stereo, format='s16', layout='stereo')
        sound.sample_rate = RATE
        sound.pts = self.frames * SAMPLES
        sound.time_base = Fraction(1, RATE)
        for stream, item in ((self.video, frame), (self.audio, sound)):
            for packet in stream.encode(item):
                self.encoded_bytes += packet.size
                self.mux(packet)
        self.frames += 1

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            for stream in (self.video, self.audio):
                for packet in stream.encode(None):
                    self.encoded_bytes += packet.size
                    self.mux(packet)
        finally:
            try:
                if self.transport:
                    self.transport.close()
            finally:
                # A wedged native open retains its stream templates until the worker exits.
                if not self.transport or not self.transport.thread.is_alive():
                    self.output.close()
