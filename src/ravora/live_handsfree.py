"""Segment the host's live PCM and submit completed utterances off the encoder thread."""
import threading
import time
import uuid

import av
import numpy as np

from ravora.developer_http import RequestError
from ravora.edge_grpc import pcm_wave
from ravora.fast_voice import SpeechOnset
from ravora.live_media import RATE


def host_wave(pcm):
    frame = av.AudioFrame.from_ndarray(np.frombuffer(pcm, dtype='<i2').reshape(1, -1),
                                       format='s16', layout='mono')
    frame.sample_rate = RATE
    converter = av.AudioResampler(format='s16', layout='mono', rate=16000)
    frames = converter.resample(frame) + converter.resample(None)
    return pcm_wave(b''.join(f.to_ndarray().tobytes() for f in frames), 16000)


class LiveHandsfree:
    def __init__(self, submit, max_seconds, onset_dbfs, silence_ms, *, language='auto'):
        self.submit = submit
        self.max_bytes = max_seconds * RATE * 2
        self.onset_dbfs = onset_dbfs
        self.quiet_power = (32768 * 10 ** ((onset_dbfs - 4) / 20)) ** 2
        self.end_bytes = silence_ms * RATE * 2 // 1000
        self.condition = threading.Condition()
        self.language = language
        self.cancel = self.worker = self.pending = self.utterance = None
        self.closed = self.processing = False
        self.playback_until = 0
        self.listen_during_playback = False
        self.result = ''

    def _arm(self, wait_for_quiet=False):
        self.onset = SpeechOnset(self.onset_dbfs, sample_rate=RATE, wait_for_quiet=wait_for_quiet)
        self.utterance = None
        self.quiet_bytes = 0

    def session(self, enabled, *, listen_during_playback=False):
        with self.condition:
            if self.cancel:
                self.cancel.set()
            self.cancel = threading.Event() if enabled and not self.closed else None
            self.listen_during_playback = self.cancel is not None and listen_during_playback
            if self.listen_during_playback:
                self.playback_until = 0
            self.pending = None
            self.processing = False
            self.result = ''
            self._arm()
            if self.cancel and self.worker is None:
                self.worker = threading.Thread(target=self._run, name='ray-host-stt', daemon=True)
                self.worker.start()

    def playback(self):
        with self.condition:
            if self.listen_during_playback:
                return
            now = time.monotonic()
            if self.cancel and now >= self.playback_until:
                self._arm()
            self.playback_until = now + .3

    def feed(self, pcm, *, gap=False):
        with self.condition:
            if self.cancel is None or time.monotonic() < self.playback_until:
                return
            if gap:
                self._arm()
            if self.utterance is None:
                onset = self.onset.feed(pcm)
                if onset is not None:
                    self.utterance = bytearray(onset)
                    self.utterance_language = self.language
                return
            self.utterance.extend(pcm)
            samples = np.frombuffer(pcm, dtype='<i2').astype(np.float32)
            self.quiet_bytes = self.quiet_bytes + len(pcm) if np.mean(samples * samples) < self.quiet_power else 0
            full = len(self.utterance) >= self.max_bytes
            if not full and self.quiet_bytes < self.end_bytes:
                return
            if self.pending is None:
                self.pending = (self.cancel, bytes(self.utterance[:self.max_bytes]), self.utterance_language)
                self.condition.notify()
            else:
                self.result = 'HOST_MIC_BUSY'
            self._arm(wait_for_quiet=full)

    def speaking(self):
        with self.condition:
            return self.utterance is not None

    def _run(self):
        while True:
            with self.condition:
                self.condition.wait_for(lambda: self.closed or self.pending is not None)
                if self.closed:
                    return
                cancel, pcm, language = self.pending
                self.pending = None
                self.processing = True
            try:
                self.submit(uuid.uuid4().hex, host_wave(pcm), already_aired=True, cancel=cancel, language=language)
                code = 'received'
            except RequestError as exc:
                code = exc.code
            except Exception:
                code = 'HOST_RECOGNITION_FAILED'
            with self.condition:
                if self.cancel is cancel:
                    self.processing = False
                    self.result = code
                    self.condition.notify_all()

    def status(self):
        with self.condition:
            if self.utterance is not None:
                state = 'speaking'
            elif self.processing or self.pending:
                state = 'transcribing'
            elif self.cancel and time.monotonic() < self.playback_until:
                state = 'playback'
            else:
                state = 'listening' if self.cancel else 'off'
            return {'state': state, 'result': self.result}

    def close(self):
        with self.condition:
            self.closed = True
            self.session(False)
            self.condition.notify_all()
        if self.worker:
            self.worker.join()
