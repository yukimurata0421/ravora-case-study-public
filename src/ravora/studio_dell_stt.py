"""Dell hands-free calibration on the untrimmed 16 kHz recognizer input."""
from functools import wraps
import io
import math
import threading
import wave

import numpy as np

from ravora.developer_http import RequestError
from ravora.settings import validate_handsfree_onset_dbfs


def levels(wav, threshold):
    with wave.open(io.BytesIO(wav)) as stream:
        if (stream.getframerate(), stream.getnchannels(), stream.getsampwidth()) != (16000, 1, 2):
            raise ValueError('EXPECTED_STUDIO_PCM')
        pcm = np.frombuffer(stream.readframes(stream.getnframes()), dtype='<i2')
    audio = pcm.astype(np.float32) / 32768
    def db(value):
        return round(20 * math.log10(max(float(value), 1e-6)), 1)
    # The onset detector retains the beginning of a quiet word. Confirm anywhere
    # in the completed utterance, without trimming or changing the original PCM.
    frames = audio[:len(audio) // 320 * 320].reshape(-1, 320)
    loud = np.mean(frames * frames, axis=1) >= 10 ** (threshold / 10)
    confirmed = bool(np.any(loud[:-3] & loud[1:-2] & loud[2:-1] & loud[3:]))
    return confirmed, (db(np.sqrt(np.mean(audio * audio))), db(np.max(np.abs(audio))),
                       int(np.count_nonzero((pcm == -32768) | (pcm == 32767))))


def metered_submit(submit, threshold):
    @wraps(submit)
    def measured(identifier, wav, **kwargs):
        confirmed, (rms, peak, clips) = levels(wav, threshold)
        result = 'failed'
        try:
            if not confirmed:
                raise RequestError('HOST_NO_VOICE', 422)
            response = submit(identifier, wav, **kwargs)
            result = response['status']
            return response
        except RequestError as exc:
            result = exc.code
            raise
        finally:
            print(f'DELL_STT_INPUT rms_dbfs={rms} peak_dbfs={peak} clips={clips} '
                  f'confirmed={int(confirmed)} result={result}', flush=True)

    measured.studio_original_submit = submit
    return measured


def configure(program, voice):
    from ravora import local_stt
    threshold = voice.get('dell_stt_confirm_dbfs')
    if threshold is not None:
        threshold = validate_handsfree_onset_dbfs(threshold)
    beam = voice.get('dell_stt_japanese_beam_size', 1)
    if type(beam) is not int or not 1 <= beam <= 5:
        raise ValueError('INVALID_LOCAL_STT_BEAM_SIZE')
    current = program.stt
    replacement = None
    if getattr(current, 'japanese_beam_size', 1) != beam:
        replacement = local_stt.LocalSTT(current.s, japanese_beam_size=beam)
    handsfree = program.handsfree
    submit = getattr(handsfree.submit, 'studio_original_submit', handsfree.submit)
    handsfree.submit = metered_submit(submit, threshold) if threshold is not None else submit
    if replacement:
        program.stt = replacement
        if current.process:
            threading.Thread(target=current.close, name='retire-studio-stt', daemon=True).start()
