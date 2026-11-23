"""Short, mono room response for Ray's 48 kHz studio playout."""
import numpy as np

from ravora.live_media import RATE


DEFAULT_RAY_REVERB_MIX = 0.06


def validate_ray_reverb_mix(value):
    if type(value) not in (int, float) or not 0 <= value <= 1:
        raise ValueError('INVALID_RAY_REVERB_MIX')
    return float(value)


class RayReverb:
    predelay = round(.008 * RATE)
    # Six unequal paths diffuse a short, damped reflection into a cluster
    # of 64 arrivals. Their level falls exponentially with travel time.
    paths = tuple((delay, sign * np.exp(-delay / (.016 * RATE)))
                  for delay, sign in ((157,-1), (277,1), (419,1), (787,-1), (1237,1), (2491,-1)))
    tail_samples = predelay + sum(delay for delay, _ in paths) + 2

    def __init__(self, mix=DEFAULT_RAY_REVERB_MIX):
        self.mix = validate_ray_reverb_mix(mix)
        self.reset()
        if self.mix:
            self.wet_gain = self.mix / np.linalg.norm(self._reflections(np.ones(1, dtype=np.float32)))

    def reset(self):
        self.history = None

    def _reflections(self, dry):
        size = len(dry)
        wet = np.zeros(size + self.tail_samples, dtype=np.float32)
        room = wet[self.predelay:]
        room[:size] = .25 * dry
        room[1:size+1] += .5 * dry
        room[2:size+2] += .25 * dry
        size += 2
        for delay, gain in self.paths:
            room[delay:delay+size] += room[:size] * np.float32(gain)
            size += delay
        return wet

    def render(self, pcm, *, final):
        if not self.mix:
            return pcm
        dry = np.frombuffer(pcm, dtype='<i2')
        source = dry if self.history is None else np.concatenate((self.history, dry))
        start = len(source) - len(dry)
        self.history = None if final else source[-self.tail_samples:].copy()
        wet = self._reflections(source.astype(np.float32))
        audio = wet[start:] if final else wet[start:len(source)]
        audio *= self.wet_gain
        audio[:len(dry)] += (1 - self.mix) * dry
        samples = np.clip(np.rint(audio), -32768, 32767).astype('<i2')
        if final:
            # Preserve all original samples and only extend the clip for an audible tail.
            tail_length = len(np.trim_zeros(samples[len(dry):], trim='b'))
            samples = samples[:len(dry)+tail_length]
        return samples.tobytes()
