"""Estimate English phone times from the actual synthesized reply, on the local CPU."""

import re
import subprocess
import time
import unicodedata
from array import array
from math import log10
from statistics import median
from threading import Lock

from ravora.speech import LocalSpeech, plan_mouth

SAMPLE_RATE = 24000
HOP = 240
WORDS = re.compile(r"[a-z]+(?:'[a-z]+)*")


class PhoneAligner:
    def __init__(self):
        from pocketsphinx import Decoder

        self.decoder = Decoder(lm=None, frate=100, loglevel="FATAL")
        self.lock = Lock()

    def __call__(self, text, pcm):
        with self.lock:
            return self._align(text, pcm)

    def _align(self, text, pcm):
        started = time.monotonic()
        normalized = unicodedata.normalize("NFKD", text.lower().replace("’", "'"))
        normalized = "".join(c for c in normalized if not unicodedata.combining(c))
        words = WORDS.findall(normalized)
        if not words or any(c.isdigit() for c in normalized):
            return {"status": "text_not_supported"}
        if not 0 < len(pcm) <= SAMPLE_RATE * 2 * 30 or len(pcm) % 2:
            return {"status": "invalid_alignment_pcm"}

        try:
            missing = {word for word in words if not self.decoder.lookup_word(word)}
            if missing:
                with LocalSpeech() as local:
                    for word in sorted(missing):
                        phones = [p.rstrip("012").upper() for p, _ in local.pronunciation(word)]
                        if not phones:
                            return {"status": "word_outside_dictionary"}
                        self.decoder.add_word(word, " ".join("AH" if p == "AX" else p for p in phones), False)
            audio = subprocess.run(
                ["ffmpeg", "-nostdin", "-v", "error", "-threads", "1", "-filter_threads", "1",
                 "-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", "1", "-i", "pipe:0",
                 "-ar", "16000", "-f", "s16le", "pipe:1"],
                input=pcm, capture_output=True, check=True, timeout=2,
            ).stdout
            self.decoder.set_align_text(" ".join(words))
            self._decode(audio)
            anchors = [(s.word.split("(")[0], s.start_frame/100, (s.end_frame+1)/100)
                       for s in self.decoder.seg() if not s.word.startswith("<")]
            if [a[0] for a in anchors] != words:
                return {"status": "word_alignment_incomplete"}
            length = len(pcm) / (SAMPLE_RATE * 2)
        except (RuntimeError, ValueError, TypeError, OSError, subprocess.SubprocessError):
            return {"status": "word_alignment_failed"}

        method = "pocketsphinx-phone-alignment"
        try:
            self.decoder.set_alignment()
            self._decode(audio)
            phones = []
            for word in self.decoder.get_alignment():
                for phone in word:
                    start = phone.start / 100
                    end = min(length, (phone.start + phone.duration) / 100)
                    if end > start:
                        phones.append({"phone": phone.name.lower(), "start": start, "end": end})
        except (RuntimeError, ValueError, TypeError):
            phones = []

        if not phones or phones[-1]["end"] < length - .12:
            method = "pocketsphinx-word-anchors-with-flite-phones"
            try:
                phones = self._interpolate_phones(anchors, length)
            except (RuntimeError, ValueError, OSError):
                return {"status": "phone_alignment_incomplete"}

        plan = plan_mouth(phones, lead=.015, minimum_key=.045)
        if not plan or not any(p["shape"] != "closed" for p in plan):
            return {"status": "phone_alignment_incomplete"}
        samples = array("h", pcm)
        # The same PCM used for playout provides emphasis independent of the phone shape.
        db = []
        for offset in range(0, len(samples), HOP):
            block = samples[offset:offset + HOP]
            power = sum(v * v for v in block) / (len(block) * 32768**2)
            db.append(round(10 * log10(max(1e-10, power)), 1))
        voiced = [value for value in db if value > -48]
        if not voiced:
            return {"status": "alignment_no_voice"}
        return {
            "status": "aligned", "method": method, "plan": plan,
            "words": [{"word": word, "start": start, "end": min(end, length)}
                      for word, start, end in anchors if 0 <= start < min(end, length)],
            "phones": phones, "frames": len(samples), "hop_frames": HOP, "db": db,
            "reference_db": median(voiced), "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
        }

    @staticmethod
    def _interpolate_phones(anchors, length):
        phones, previous = [], 0.
        with LocalSpeech() as local:
            for word, start, stop in anchors:
                start, stop = max(previous, start), min(stop, length)
                if stop <= start:
                    continue
                if start > previous:
                    phones.append({"phone": "sil", "start": previous, "end": start})
                parts = local.pronunciation(word)
                total = sum(duration for _, duration in parts)
                if not total:
                    raise ValueError("PHONETIC_WORD_EMPTY")
                timepoint = start
                for phone, duration in parts:
                    end = timepoint + (stop - start) * duration / total
                    phones.append({"phone": phone, "start": timepoint, "end": end})
                    timepoint = end
                previous = stop
        if previous < length:
            phones.append({"phone": "sil", "start": previous, "end": length})
        return phones

    def _decode(self, audio):
        self.decoder.start_utt()
        try:
            self.decoder.process_raw(audio, full_utt=True)
        finally:
            self.decoder.end_utt()
