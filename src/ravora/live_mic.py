"""Short-lived host microphone PCM on the broadcast encoder's clock."""
from collections import deque
import math
import threading
import time
import uuid

import numpy as np

from ravora.developer_http import RequestError
from ravora.live_media import FPS, RATE, SAMPLES, SILENCE

# Three 30 Hz frames per browser upload at 48 kHz, mono, signed little-endian PCM.
FRAMES_PER_PACKET = 3
PACKET_SECONDS = FRAMES_PER_PACKET / FPS
PACKET_BYTES = FRAMES_PER_PACKET * len(SILENCE)
FRAME_RAMP = np.arange(1, SAMPLES + 1, dtype=np.float32) / SAMPLES


def validate_host_trim_db(value):
    if type(value) not in (float, int) or not -24 <= value <= 0:
        raise RequestError('INVALID_HOST_TRIM_DB')
    return float(value)


class HostMicCleanup:
    """Soften isolated clicks and low-level aperiodic sound on the host bus."""
    block = 40  # 0.83 ms at 48 kHz
    # Test periodicity over typical voice pitch lags. FFT padding avoids wraparound.
    pitch_lags = np.arange(RATE // 450, RATE // 70 + 1)
    fft_size = 1 << (SAMPLES + int(pitch_lags[-1]) - 1).bit_length()
    quiet_rms = 32768 * 10 ** (-36 / 20)
    speech_rms = 32768 * 10 ** (-26 / 20)
    release = np.minimum(1., np.arange(1, SAMPLES + 1, dtype=np.float32) / (RATE * .002))

    def __init__(self):
        self.reset()

    def reset(self):
        self.previous_peaks = None
        self.voice_hold = 0
        self.gain = 1.

    def apply(self, samples):
        # A mouth click can straddle two encoder frames. Keep the preceding
        # four peaks and estimate the right edge within this frame, without queuing audio.
        blocks = samples.reshape(-1, self.block)
        peaks = np.abs(blocks).max(axis=1)
        left = self.previous_peaks if self.previous_peaks is not None else np.full(4, np.median(peaks[:9]))
        right = np.full(4, np.median(peaks[-9:]))
        neighbors = np.lib.stride_tricks.sliding_window_view(np.concatenate((left, peaks, right)), 9)
        limits = np.maximum(400., 4 * np.median(neighbors, axis=1))
        self.previous_peaks = peaks[-4:].copy()
        if np.any(peaks > limits):
            samples = np.clip(blocks, -limits[:, None], limits[:, None]).ravel()

        centered = samples - samples.mean()
        power = float(centered @ centered) / SAMPLES
        rms = math.sqrt(power)
        target = 1.
        if rms >= self.speech_rms:
            self.voice_hold = 2
        elif rms > 1.:
            spectrum = np.fft.rfft(centered, n=self.fft_size)
            corr = np.fft.irfft(np.abs(spectrum) ** 2, n=self.fft_size)
            periodicity = np.max(corr[self.pitch_lags] / ((SAMPLES - self.pitch_lags) * power))
            if periodicity >= .30:
                self.voice_hold = 2
            elif self.voice_hold == 0:
                # Keep quiet voiced interjections and ease off toward normal speech level.
                fraction = max(0., (rms - self.quiet_rms) / (self.speech_rms - self.quiet_rms))
                target = .25 + .75 * fraction
            else:
                self.voice_hold -= 1
        else:
            self.voice_hold = max(0, self.voice_hold - 1)

        if self.gain != target:
            ramp = self.release if target > self.gain else FRAME_RAMP
            samples = samples * (self.gain + (target - self.gain) * ramp)
        elif target != 1.:
            samples = samples * target
        self.gain = target
        return samples, target == 1.


class HostAutoGain:
    """Match active host speech to Ray; all time constants use the program clock."""
    # Mean-square levels in PCM16 units. Ignore DC and very quiet frames.
    full_scale_power = 32768.0 ** 2
    activity_floor = full_scale_power * 10 ** (-48 / 10)
    ray_floor = full_scale_power * 10 ** (-36 / 10)
    peak_limit = 32768 * 10 ** (-1.5 / 20)

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

    def apply(self, samples, *, track_speech=True):
        power = float(np.var(samples, dtype=np.float64))
        if not track_speech or power < self.activity_floor:
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
        return samples * (start + (end - start) * FRAME_RAMP)


class LiveMicrophone:
    def __init__(self, clock=time.monotonic, *, handsfree=None, host_trim_db=0.0, host_mic_cleanup=True):
        self.clock = clock
        self.handsfree = handsfree
        self.leveler = HostAutoGain()
        self.cleanup = HostMicCleanup() if host_mic_cleanup else None
        self.lock = threading.Lock()
        self.set_host_trim_db(host_trim_db)
        self.previous_trim = self.host_trim
        self.owner = None
        self.expires = 0
        self.sequence = -1
        self.frames = deque(maxlen=2 * FRAMES_PER_PACKET)
        self.ready = 0
        self.origin = 0

    def set_host_trim_db(self, value):
        db = validate_host_trim_db(value)
        with self.lock:
            self.host_trim_db = db
            self.host_trim = 10 ** (db / 20)
        return db

    def open(self, *, automatic=False, echo_cancelled=False):
        with self.lock:
            now = self.clock()
            if self.owner and now < self.expires:
                raise RequestError('MIC_ALREADY_OPEN', 409)
            self.owner = uuid.uuid4().hex
            self.frames.clear()
            self._reset_host()
            self.origin = now
            self.expires = now + 2
            self.sequence = -1
            if self.handsfree:
                self.handsfree.session(automatic, listen_during_playback=echo_cancelled)
            return self.owner

    def _reset_host(self):
        self.leveler.reset_host()
        if self.cleanup:
            self.cleanup.reset()

    def _release(self):
        self.owner = None
        self.frames.clear()
        self._reset_host()
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
                self._reset_host()
            if not self.frames:
                self.ready = now + 1 / FPS
            self.sequence = sequence
            self.expires = now + 1
            for start in range(0, len(pcm), len(SILENCE)):
                self.frames.append((now + .25, pcm[start:start + len(SILENCE)]))
            if self.handsfree:
                self.handsfree.feed(pcm, gap=gap)

    def allows_initiative(self, *, during_playback=False, require_echo_cancellation=False):
        # Manual push-to-talk can capture in the browser before the WAV reaches
        # the server. Only hands-free mode can positively identify a quiet open mic.
        with self.lock:
            state = self.handsfree.status()['state'] if self.handsfree else 'off'
            if self.owner and self.clock() < self.expires:
                if require_echo_cancellation and not (self.handsfree and self.handsfree.listen_during_playback):
                    return False
                return state == 'listening' or (during_playback and state == 'playback')
            return state not in ('speaking','transcribing')

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
                if self.cleanup:
                    self.cleanup.reset()
                self.previous_trim = self.host_trim
                return program
            # Legacy host WAVs and live packets share one host bus. The live
            # recorder and STT already received the original capture in push().
            host = np.frombuffer(voice if voice is not None else SILENCE, dtype='<i2').astype(np.float32)
            if host_playback:
                host += np.frombuffer(program, dtype='<i2')
            track_speech = True
            if self.cleanup:
                host, track_speech = self.cleanup.apply(host)
            host = self.leveler.apply(host, track_speech=track_speech)
            if self.previous_trim == self.host_trim:
                host *= self.host_trim
            else:
                # Fade across one program frame when the operator moves the fader.
                host *= self.previous_trim + (self.host_trim - self.previous_trim) * FRAME_RAMP
                self.previous_trim = self.host_trim
        if host_playback or program == SILENCE:
            return np.rint(host).astype('<i2').tobytes()
        if not host.any():
            return program
        mixed = host + np.frombuffer(program, dtype='<i2')
        peak = np.max(np.abs(mixed))
        if peak > self.leveler.peak_limit:
            mixed *= self.leveler.peak_limit / peak
        return np.rint(mixed).astype('<i2').tobytes()
