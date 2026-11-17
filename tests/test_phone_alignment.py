import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path

from ravora.phone_alignment import PhoneAligner
from ravora.speech import LocalSpeech


@unittest.skipUnless(importlib.util.find_spec("pocketsphinx"), "optional phone alignment")
class PhoneAlignmentTests(unittest.TestCase):
    def test_aligns_spoken_vowels_and_keeps_lip_closures(self):
        text = "Meet me by the old blue bus."
        with tempfile.TemporaryDirectory() as directory, LocalSpeech() as speech:
            path = Path(directory) / "reply.wav"
            reference = speech.create(text, path)
            pcm = subprocess.check_output(
                ["ffmpeg", "-nostdin", "-v", "error", "-threads", "1", "-i", str(path),
                 "-ar", "24000", "-f", "s16le", "pipe:1"],
                timeout=6,
            )
        result = PhoneAligner()(text, pcm)
        self.assertEqual(result["status"], "aligned")
        self.assertEqual(result["frames"], len(pcm) // 2)
        self.assertTrue({"I", "U", "closed"}.issubset({key["shape"] for key in result["plan"]}))
        self.assertTrue(all(key["amount"] == 0 for key in result["plan"] if key["phone"] in ("m", "b", "p")))
        self.assertGreaterEqual(sum(key["phone"] == "m" for key in result["plan"]), 2)
        expected = next(phone["start"] for phone in reference["phones"] if phone["phone"] == "iy")
        observed = next(phone["start"] for phone in result["phones"] if phone["phone"] == "iy")
        self.assertLess(abs(expected - observed), .10)
