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
FUNCTION_WORDS = frozenset({
    "a", "an", "the", "and", "or", "but", "if", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "as", "is", "am", "are", "was", "were", "be",
    "been", "it", "its", "it's", "that", "this", "i", "i'd", "i'm", "i'll",
    "you", "we", "they", "he", "she", "me", "my", "your", "our", "of", "so",
})
VOWEL_PHONES = frozenset({
    "aa", "ae", "ah", "ax", "iy", "ih", "uw", "uh", "eh", "ao", "er",
    "ey", "ay", "aw", "oy", "ow",
})


def enrich_phone_prosody(text, phones, words=None):
    """Attach lexical stress, pitch accent, and content/function word class to aligned phones.

    Pocketsphinx phone alignment emits bare phone names without stress/accent/gpos, which caused
    plan_mouth() to give every vowel a flat 0.48 opening regardless of emphasis. Annotate aligned
    phones from local Flite prosody (falling back to word-class heuristics if unavailable).
    """
    prosody = []
    try:
        with LocalSpeech() as local:
            prosody = local.prosody(text)
    except (RuntimeError, ValueError, OSError):
        prosody = []

    def norm(name):
        b = name.lower().rstrip("012")
        return "ah" if b == "ax" else b

    word_spans = words or []
    enriched = []
    pi = 0
    for ph in phones:
        entry = dict(ph)
        base = ph["phone"].lower().rstrip("012")
        if base in ("sil", "sp", "pau"):
            enriched.append(entry)
            continue
        matched = None
        # Match aligned phone sequentially against Flite prosody stream (small lookahead window).
        for look in range(pi, min(len(prosody), pi + 5)):
            if norm(prosody[look]["phone"]) == norm(base):
                matched = prosody[look]
                pi = look + 1
                break
        if matched is not None:
            for key in ("vc", "stress", "accented", "gpos"):
                if matched.get(key) is not None:
                    entry[key] = matched[key]
            # Preserve Flite's schwa distinction ('ax') when Pocketsphinx collapses it to 'ah'.
            if matched["phone"].rstrip("012") == "ax" and base == "ah":
                entry["phone"] = "ax"
        elif base in VOWEL_PHONES:
            mid = (ph["start"] + ph["end"]) * 0.5
            word = next((w["word"] for w in word_spans if w["start"] <= mid <= w["end"]), "")
            entry["vc"] = "+"
            if word in FUNCTION_WORDS:
                entry["gpos"] = "aux"
                entry["stress"] = "0"
                entry["accented"] = "0"
            else:
                entry["gpos"] = "content"
                entry["stress"] = "1"
                entry["accented"] = "1" if (ph["end"] - ph["start"]) >= 0.085 else "0"
        enriched.append(entry)
    return enriched


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

        word_list = [{"word": word, "start": start, "end": min(end, length)}
                     for word, start, end in anchors if 0 <= start < min(end, length)]
        phones = enrich_phone_prosody(text, phones, word_list)
        plan = plan_mouth(phones, lead=.035, minimum_key=.045)
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
            "words": word_list,
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
