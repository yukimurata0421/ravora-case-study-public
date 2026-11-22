"""Short-lived host microphone PCM on the broadcast encoder's clock."""
from collections import deque
import math
import threading
import time
import uuid

import numpy as np

from ravora.developer_http import RequestError
from ravora.live_media import FPS, SAMPLES, SILENCE

# Three 30 Hz frames per browser upload at 48 kHz, mono, signed little-endian PCM.
FRAMES_PER_PACKET = 3
PACKET_SECONDS = FRAMES_PER_PACKET / FPS
PACKET_BYTES = FRAMES_PER_PACKET * len(SILENCE)


class HostAutoGain:
    """Match active host speech to Ray; all time constants use the program clock."""
    # Mean-square levels in PCM16 units. Ignore DC and very quiet frames.
    full_scale_power = 32768.0 ** 2
    activity_floor = full_scale_power * 10 ** (-48 / 10)
    ray_floor = full_scale_power * 10 ** (-36 / 10)
    peak_limit = 32768 * 10 ** (-1.5 / 20)
    ramp = np.arange(1, SAMPLES + 1, dtype=np.float32) / SAMPLES

    def __init__(self):
        self.ray_power = self.full_scale_power * 10 ** (-16 / 10)
        self.reset_host()

    def reset_host(self):
        self.host_power = None
        self.gain_db = 0.0
        self.quiet_frames = 0

    def observe_ray(self, pcm):
        power = float(np.var(np.frombuffer(pcm, dtype='<i2'), dtype=np.float64))
        if power > self.ray_floor:
            self.ray_power += (1 - math.exp(-1 / (FPS * .8))) * (power - self.ray_power)

    def apply(self, samples):
        power = float(np.var(samples, dtype=np.float64))
        if power < self.activity_floor:
            self.quiet_frames += 1
            if self.quiet_frames >= FPS // 2:
                self.host_power = None
            # Hold through short gaps within a word. Relax to unity in room tone.
            desired_db = self.gain_db if self.quiet_frames <= 4 else 0.0
        else:
            self.quiet_frames = 0
            if self.host_power is None:
                self.host_power = power
            else:
                # Follow a louder phrase promptly, without chasing every soft syllable.
                seconds = .08 if power > self.host_power else .50
                self.host_power += (1 - math.exp(-1 / (FPS * seconds))) * (power - self.host_power)
            reference_db = min(-12, max(-23, 10 * math.log10(self.ray_power / self.full_scale_power)))
            desired_db = reference_db - 10 * math.log10(self.host_power / self.full_scale_power)
            # Fade in the available boost above the noise floor.
            boost_limit = min(18., 2 * 10 * math.log10(power / self.activity_floor))
            desired_db = min(boost_limit, max(-18., desired_db))

        previous_db = self.gain_db
        seconds = .06 if desired_db < previous_db else .18
        self.gain_db += (1 - math.exp(-1 / (FPS * seconds))) * (desired_db - previous_db)
        start, end = 10 ** (previous_db / 20), 10 ** (self.gain_db / 20)
        peak = float(np.max(np.abs(samples)))
        if peak:
            headroom = self.peak_limit / peak
            start, end = min(start, headroom), min(end, headroom)
            self.gain_db = 20 * math.log10(end)
        return samples * (start + (end - start) * self.ramp)


class LiveMicrophone:
    def __init__(self, clock=time.monotonic, *, handsfree=None):
        self.clock = clock
        self.handsfree = handsfree
        self.leveler = HostAutoGain()
        self.lock = threading.Lock()
        self.owner = None
        self.expires = 0
        self.sequence = -1
        self.frames = deque(maxlen=2 * FRAMES_PER_PACKET)
        self.ready = 0
        self.origin = 0

    def open(self, *, automatic=False):
        with self.lock:
            now = self.clock()
            if self.owner and now < self.expires:
                raise RequestError('MIC_ALREADY_OPEN', 409)
            self.owner = uuid.uuid4().hex
            self.frames.clear()
            self.leveler.reset_host()
            self.origin = now
            self.expires = now + 2
            self.sequence = -1
            if self.handsfree:
                self.handsfree.session(automatic)
            return self.owner

    def _release(self):
        self.owner = None
        self.frames.clear()
        self.leveler.reset_host()
        if self.handsfree:
            self.handsfree.session(False)

    def mute(self, owner):
        with self.lock:
            if self.owner == owner:
                self._release()

    def push(self, owner, sequence, pcm):
        if len(pcm) != PACKET_BYTES or type(sequence) is not int or not 0 <= sequence < 2**31:
            raise RequestError('INVALID_MIC_PACKET')
        with self.lock:
            now = self.clock()
            if not self.owner or self.owner != owner or now >= self.expires:
                raise RequestError('MIC_SESSION_EXPIRED', 409)
            if sequence <= self.sequence:
                return
            expected = self.origin + (sequence + 1) * PACKET_SECONDS
            drift = now - expected
            if abs(drift) > .5:
                raise RequestError('MIC_CLOCK_DISCONTINUITY', 409)
            # Follow hardware clock drift slowly, without following a network stall.
            self.origin += max(-.0005, min(.0005, drift))
            gap = sequence != self.sequence + 1
            if gap:
                self.frames.clear()
                self.leveler.reset_host()
            if not self.frames:
                self.ready = now + 1 / FPS
            self.sequence = sequence
            self.expires = now + 1
            for start in range(0, len(pcm), len(SILENCE)):
                self.frames.append((now + .25, pcm[start:start + len(SILENCE)]))
            if self.handsfree:
                self.handsfree.feed(pcm, gap=gap)

    def mix(self, program, *, ray_speaking=False, host_playback=False):
        with self.lock:
            now = self.clock()
            if self.owner and now >= self.expires:
                self._release()
            if ray_speaking:
                if self.handsfree:
                    self.handsfree.playback()
                self.leveler.observe_ray(program)
            while self.frames and self.frames[0][0] <= now:
                self.frames.popleft()
            voice = self.frames.popleft()[1] if self.frames and now >= self.ready else None
            if voice is None and not host_playback:
                return program
            # Legacy host WAVs and live packets share one host bus. The live
            # recorder and STT already received the original capture in push().
            host = np.frombuffer(voice if voice is not None else SILENCE, dtype='<i2').astype(np.float32)
            if host_playback:
                host += np.frombuffer(program, dtype='<i2')
            host = self.leveler.apply(host)
        if host_playback or program == SILENCE:
            return np.rint(host).astype('<i2').tobytes()
        if not host.any():
            return program
        mixed = host + np.frombuffer(program, dtype='<i2')
        peak = np.max(np.abs(mixed))
        if peak > self.leveler.peak_limit:
            mixed *= self.leveler.peak_limit / peak
        return np.rint(mixed).astype('<i2').tobytes()
