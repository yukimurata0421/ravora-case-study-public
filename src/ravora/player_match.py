"""Match a turn's first validated WAV against an independently recorded player."""
import hashlib
import math
from pathlib import Path

import numpy as np

from ravora.live_media import RATE, decode_wav


MATCH_RATE = RATE // 4


def samples(wav):
    pcm = np.frombuffer(decode_wav(wav), '<i2')
    length = len(pcm) - len(pcm) % 4
    return pcm[:length].reshape(-1,4).mean(axis=1)


def locate(reference, capture):
    # A one-second (or shorter) excerpt from the first audible sample. Correlate
    # both halves at the same lag and reject a second plausible occurrence.
    ref = samples(reference)
    recording = samples(capture)
    voiced = np.flatnonzero(abs(ref) >= 33)
    if not voiced.size:
        raise ValueError('PLAYER_REFERENCE_SILENT')
    template = ref[voiced[0]:voiced[0] + MATCH_RATE]
    n = len(template)
    if n < MATCH_RATE // 3 or len(recording) < n:
        raise ValueError('PLAYER_MATCH_AUDIO_TOO_SHORT')
    centered = template - template.mean()
    fft_size = 1 << (len(recording) + n - 2).bit_length()
    product = np.fft.rfft(recording,fft_size) * np.fft.rfft(centered[::-1],fft_size)
    numerator = np.fft.irfft(product,fft_size)[n-1:len(recording)]
    sums = np.r_[0., recording.cumsum()]
    squares = np.r_[0., (recording**2).cumsum()]
    variance = np.maximum(0, squares[n:]-squares[:-n]-(sums[n:]-sums[:-n])**2/n)
    denominator = np.sqrt(variance)*np.linalg.norm(centered)
    scores = np.divide(numerator,denominator,out=np.zeros_like(numerator),where=denominator>0)
    offset = int(np.argmax(scores))
    peak = float(scores[offset])
    radius = MATCH_RATE // 20
    rivals = np.r_[scores[:max(0,offset-radius)],scores[offset+radius+1:]]
    runner = float(rivals.max()) if rivals.size else 0.
    if peak < .82 or peak-runner < .08:
        raise ValueError('PLAYER_MATCH_WEAK_OR_AMBIGUOUS')
    excerpt = recording[offset:offset+n]
    for part in (slice(0,n//2), slice(n//2,None)):
        a = template[part] - template[part].mean()
        b = excerpt[part] - excerpt[part].mean()
        norm = np.linalg.norm(a)*np.linalg.norm(b)
        if norm == 0 or np.dot(a,b)/norm < .75:
            raise ValueError('PLAYER_MATCH_INCONSISTENT')
    return offset / MATCH_RATE, round(min(1.,peak),5)


def bounded_wav(path):
    with Path(path).open('rb') as file:
        data = file.read(12*1024**2 + 1)
    if len(data) > 12*1024**2:
        raise ValueError('PLAYER_MATCH_WAV_TOO_LARGE')
    return data


def player_observation(db, session, turn, captured, captured_at, reference=None):
    row = db.execute("""SELECT i.receipt, u.audio_path, u.audio_sha256, i.first_written
        FROM studio_inputs i JOIN utterances u USING(turn_id)
        WHERE i.session=? AND i.turn_id=? AND u.chunk_index=0 AND i.first_written IS NOT NULL
          AND i.source IN ('twitch','youtube','youtube-fixture') AND u.audio_sha256 IS NOT NULL""",
                     (session,turn)).fetchone()
    if not row or not math.isfinite(captured_at):
        raise ValueError('MISSING_TURN_REFERENCE_OR_CLOCK')
    wav = bounded_wav(reference or row[1])
    if hashlib.sha256(wav).hexdigest() != row[2]:
        raise ValueError('PLAYER_REFERENCE_TURN_MISMATCH')
    recording = bounded_wav(captured)
    offset, score = locate(wav,recording)
    stamp = captured_at + offset
    if stamp < row[3]:
        raise ValueError('PLAYER_CAPTURE_CLOCK_MISMATCH')
    return {'kind':'player_signal','session':session,'turn_id':turn,'receipt':row[0],
            'method':'waveform_match','observed_at':stamp,
            'capture_sha256':hashlib.sha256(recording).hexdigest(),'correlation':score}
