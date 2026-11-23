"""Loop local music on the program's sample clock, preserving stereo."""
from pathlib import Path
import threading

import av
import numpy as np

from ravora.developer_http import RequestError
from ravora.live_media import RATE


DEFAULT_BGM_DB = -24
MUTE_DB = -60


def validate_bgm_db(value):
    if type(value) not in (int, float) or not MUTE_DB <= value <= 0:
        raise RequestError('INVALID_BGM_VOLUME_DB')
    return float(value)


def bgm_tracks(config, root):
    entries = config.get('bgm_tracks', [])
    if not isinstance(entries, list) or not all(isinstance(name, str) and name.strip() for name in entries):
        raise ValueError('INVALID_BGM_TRACKS')
    return tuple(root / name for name in entries)


def stereo_blocks(path):
    resampler = av.AudioResampler(format='s16', layout='stereo', rate=RATE)
    with path.open('rb') as file, av.open(file, format='mp3') as source:
        if not source.streams.audio:
            raise ValueError('BGM_AUDIO_STREAM_MISSING')
        for frame in source.decode(audio=0):
            for output in resampler.resample(frame):
                yield output.to_ndarray().reshape(-1, 2)
        for output in resampler.resample(None):
            yield output.to_ndarray().reshape(-1, 2)


class LoopingMusic:
    def __init__(self, paths, volume_db=DEFAULT_BGM_DB):
        self.paths = tuple(Path(path) for path in paths)
        self.lock = threading.Lock()
        self.volume_db = validate_bgm_db(volume_db)
        self.previous_gain = self.gain()
        self.track = None
        self.failed = set()
        self.blocks = self._playlist()
        self.pending = np.empty((0, 2), dtype=np.int16)

    def _playlist(self):
        while len(self.failed) < len(self.paths):
            for index, path in enumerate(self.paths):
                if index in self.failed:
                    continue
                produced = False
                try:
                    for block in stereo_blocks(path):
                        if block.size:
                            produced = True
                            self.track = path.stem
                            yield block
                except (OSError, ValueError, EOFError, av.error.FFmpegError):
                    self.failed.add(index)
                if not produced:
                    self.failed.add(index)
        self.track = None

    def gain(self):
        return 0.0 if self.volume_db == MUTE_DB else 10 ** (self.volume_db / 20)

    def set_volume(self, value):
        with self.lock:
            self.volume_db = validate_bgm_db(value)
            return self.volume_db

    def status(self):
        with self.lock:
            return {'volume_db': self.volume_db, 'track': self.track,
                    'available': len(self.failed) < len(self.paths), 'failed_tracks': len(self.failed)}

    def mix(self, voice):
        """Return interleaved stereo PCM; continue the playlist when the fader is muted."""
        music = np.zeros((len(voice), 2), dtype=np.float32)
        with self.lock:
            filled = 0
            while filled < len(voice):
                if not self.pending.size:
                    block = next(self.blocks, None)
                    if block is None:
                        break
                    self.pending = block
                count = min(len(self.pending), len(voice) - filled)
                music[filled:filled + count] = self.pending[:count]
                self.pending = self.pending[count:]
                filled += count
            gain = self.gain()
            if gain == self.previous_gain:
                music *= gain
            else:
                ramp = np.linspace(self.previous_gain, gain, len(voice), dtype=np.float32)
                music *= ramp[:, None]
                self.previous_gain = gain
        music += voice[:, None]
        return np.clip(np.rint(music), -32768, 32767).astype('<i2').reshape(1, -1)

    def close(self):
        with self.lock:
            self.blocks.close()
