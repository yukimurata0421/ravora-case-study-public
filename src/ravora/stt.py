"""WAV parsing and legacy/mock adapters. Runtime CPU STT lives in local_stt.py."""
from array import array
from dataclasses import dataclass
import hashlib
import io
import math
from pathlib import Path
import shutil
import subprocess
import sys
import wave

from ravora.providers import ProviderError


def speech_access_token(settings):
    token = settings.gcp_access_token
    if not token:
        sdk = Path.home() / 'google-cloud-sdk' / 'bin'
        gcloud = shutil.which('gcloud') or shutil.which('gcloud', path=str(sdk))
        if not gcloud:
            raise ProviderError('GCLOUD_NOT_FOUND')
        try:
            result = subprocess.run([gcloud, 'auth', 'print-access-token', '--project', settings.cloud_project],
                                    capture_output=True, text=True, timeout=15, check=True)
        except (OSError, subprocess.SubprocessError):
            raise ProviderError('STT_AUTH_REFRESH_FAILED') from None
        token = result.stdout.strip()
    if not token or any(ch.isspace() for ch in token):
        raise ProviderError('STT_AUTH_UNAVAILABLE')
    return token


@dataclass(frozen=True)
class AudioPacket:
    data: bytes
    duration: float
    rms: float
    sha256: str

    @classmethod
    def parse(cls,data,*,maximum_seconds=30,minimum_rms=.001):
        if not isinstance(data,bytes) or not 44<=len(data)<=int(maximum_seconds*32000)+4096:
            raise ProviderError('INVALID_AUDIO_SIZE')
        if data[:4]!=b'RIFF' or data[8:12]!=b'WAVE' or int.from_bytes(data[4:8],'little')+8!=len(data):
            raise ProviderError('INVALID_WAV_CONTAINER')
        try:
            with wave.open(io.BytesIO(data)) as audio:
                if (audio.getnchannels(),audio.getsampwidth(),audio.getframerate(),audio.getcomptype())!=(1,2,16000,'NONE'):
                    raise ProviderError('PCM16_MONO_16000_REQUIRED')
                count=audio.getnframes();duration=count/16000
                if not .2<=duration<=maximum_seconds:raise ProviderError('INVALID_AUDIO_DURATION')
                pcm=audio.readframes(count)
                if len(pcm)!=count*2:raise ProviderError('TRUNCATED_AUDIO')
        except (wave.Error,EOFError,ValueError):raise ProviderError('INVALID_WAV') from None
        samples=array('h');samples.frombytes(pcm)
        if sys.byteorder!='little':samples.byteswap()
        rms=math.sqrt(sum(x*x for x in samples)/len(samples))/32768
        if rms<minimum_rms:raise ProviderError('AUDIO_SILENCE')
        return cls(data,duration,rms,hashlib.sha256(data).hexdigest())
