import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import wave

try:
    import numpy as np
except ModuleNotFoundError:
    raise unittest.SkipTest('requires numpy') from None

from scripts.measure_player_audio import align, FRAME_BYTES, match, MATCH_RATE, RATE


class PlayerAudioTests(unittest.TestCase):
    def test_wav_alignment_counts_actual_sample_onset_relative_to_chat(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            rng = np.random.default_rng(24)
            clip = rng.integers(-3000, 3001, RATE // 2, dtype=np.int16)
            clip[:RATE // 20] = 0
            ref = root / 'ref.wav'
            with wave.open(str(ref), 'wb') as wav:
                wav.setparams((1, 2, RATE, 0, 'NONE', ''))
                wav.writeframes(clip.astype('<i2').tobytes())
            pcm = rng.integers(-120, 121, (RATE * 9, 2), dtype=np.int16)
            pcm[5 * RATE:5 * RATE + len(clip)] += clip[:, None]
            (root / 'player.pcm').write_bytes(pcm.astype('<i2').tobytes())
            clock = [[i * FRAME_BYTES, 1000 + i / RATE] for i in range(1_920, len(pcm) + 1, 1_920)]
            (root / 'clock.jsonl').write_text('\n'.join(json.dumps(pair) for pair in clock))
            result = align(root, ref, 1001)
            self.assertAlmostEqual(result['event_to_player_audio_seconds'], 4.05, places=3)
            self.assertGreater(result['waveform_correlation'], .98)
            with self.assertRaisesRegex(ValueError, 'OUTSIDE'):
                align(root, ref, 999)

    def test_repeating_signal_is_not_a_unique_reply(self):
        rng = np.random.default_rng(24)
        reply = rng.normal(size=MATCH_RATE // 2)
        with self.assertRaisesRegex(ValueError, 'MATCH_UNCONFIRMED'):
            match(np.tile(reply, 3), reply)
