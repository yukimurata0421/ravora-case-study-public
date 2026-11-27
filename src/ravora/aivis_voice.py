"""Read a complete Kanon utterance from the host's private AivisSpeech Engine."""
import http.client
import io
import json
import os
from pathlib import Path
import socket
import urllib.parse
import wave

from ravora.providers import ProviderError


KANON_STYLE = 1325133120
KANON_SPEAKER = 'b1ca560f-f212-4e67-ab7d-0a4f5afb75a8'
WAV_LIMIT = 60 * 24000 * 2 + 4096


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, socket_path, timeout):
        super().__init__('localhost', timeout=timeout)
        self.socket_path = socket_path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.socket_path)


class KanonSpeech:
    local = True
    streams_program_pcm = True
    voice = 'ja-JP-Aivis-Kanon-Normal'

    def __init__(self, settings, socket_path=None):
        self.timeout = settings.timeout
        runtime = os.environ.get('XDG_RUNTIME_DIR') or f'/run/user/{os.getuid()}'
        self.socket_path = str(socket_path or Path(runtime) / 'ray-aivis' / 'kanon.sock')

    def _post(self, endpoint, params, body, limit):
        conn = UnixConnection(self.socket_path, self.timeout)
        try:
            conn.request('POST', endpoint + '?' + urllib.parse.urlencode(params),
                         body=body, headers={'Content-Type': 'application/json'})
            response = conn.getresponse()
            if response.status != 200:
                raise ProviderError('LOCAL_TTS_HTTP_' + str(response.status))
            data = response.read(limit + 1)
            if not data or len(data) > limit:
                raise ProviderError('INVALID_TTS_RESULT')
            return data
        except (OSError, http.client.HTTPException) as exc:
            raise ProviderError('LOCAL_TTS_CONNECTION_FAILED') from exc
        finally:
            conn.close()

    def create(self, text, output, *, on_pcm=None):
        params = {'speaker': KANON_STYLE}
        try:
            query = json.loads(self._post('/audio_query', dict(params, text=text), b'', 512 * 1024))
            query['outputSamplingRate'] = 24000
            query['outputStereo'] = False
            wav = self._post('/synthesis', params, json.dumps(query).encode(), WAV_LIMIT)
            with wave.open(io.BytesIO(wav)) as f:
                frames = f.getnframes()
                if ((f.getframerate(), f.getnchannels(), f.getsampwidth()) != (24000, 1, 2)
                        or not 0 < frames <= 60 * 24000):
                    raise ProviderError('INVALID_TTS_RESULT')
                pcm = f.readframes(frames)
                if len(pcm) != 2 * frames:
                    raise ProviderError('INVALID_TTS_RESULT')
        except (TypeError, ValueError, EOFError, wave.Error) as exc:
            raise ProviderError('INVALID_TTS_RESULT') from exc
        Path(output).write_bytes(wav)
        if on_pcm:
            on_pcm(pcm)
        return {'source': 'local-aivis-cuda', 'voice': self.voice, 'duration': frames / 24000}
