"""The selected Ray voice and the matching, validated local PCM response."""
import http.client
import io
import json
import wave

from ravora.aivis_voice import UnixConnection, WAV_LIMIT
from ravora.providers import ProviderError


class RayEnglishProfile:
    voice = 'en-US-Kokoro-Ray-Kanon-v1'
    style = 'ray-kanon-en-v1'

    def _pcm(self, text):
        conn = UnixConnection(self.socket_path, self.timeout)
        try:
            conn.request('POST', '/synthesis', json.dumps({'text': text, 'voice': self.style}).encode(),
                         {'Content-Type': 'application/json'})
            response = conn.getresponse()
            if response.status != 200:
                raise ProviderError('LOCAL_TTS_HTTP_' + str(response.status))
            if response.getheader('X-Kokoro-Voice') != self.style:
                raise ProviderError('KOKORO_VOICE_MISMATCH')
            raw = response.read(WAV_LIMIT + 1)
            if len(raw) > WAV_LIMIT:
                raise ProviderError('PROGRAM_AUDIO_TOO_LONG')
            with wave.open(io.BytesIO(raw)) as wav:
                n = wav.getnframes()
                if (wav.getframerate(), wav.getsampwidth(), wav.getnchannels()) != (24000, 2, 1) or not 0 < n <= 60*24000:
                    raise ProviderError('INVALID_TTS_RESULT')
                pcm = wav.readframes(n)
                if len(pcm) != n*2:
                    raise ProviderError('INVALID_TTS_RESULT')
                return pcm
        except (wave.Error, EOFError) as exc:
            raise ProviderError('INVALID_TTS_RESULT') from exc
        except (OSError, http.client.HTTPException) as exc:
            raise ProviderError('LOCAL_TTS_CONNECTION_FAILED') from exc
        finally:
            conn.close()
