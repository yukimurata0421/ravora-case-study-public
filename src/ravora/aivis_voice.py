"""Read a complete Kanon utterance from the host's private AivisSpeech Engine."""
from concurrent.futures import ThreadPoolExecutor
from copy import copy
import http.client
import io
import json
import logging
import os
from pathlib import Path
import re
import socket
import tempfile
import time
import urllib.parse
import wave

from ravora.providers import ProviderError
from ravora.spoken_language import japanese_reply_parts


KANON_STYLE = 1325133120
KANON_SPEAKER = 'b1ca560f-f212-4e67-ab7d-0a4f5afb75a8'
WAV_LIMIT = 60 * 24000 * 2 + 4096


# Ray collects these bounded records in the publisher journal without writing another SQLite transaction.
TIMING_LOG = logging.getLogger('ravora.kanon_timing')
TIMING_LOG.setLevel(logging.INFO)
TURN_WAV = re.compile(r'([0-9a-f]{32})-([0-9]{1,6})\.wav')


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

        self.viewer_reply = False
        self.warm_render_ratio = None
        tail = os.environ.get('RAVORA_KANON_TAIL_SOCKET')
        self.tail_socket = (tail if tail and Path(tail).is_absolute()
                            and Path(tail).resolve() != Path(self.socket_path).resolve() else None)

    def can_release_lead(self, parts):
        if len(parts) != 2 or self.warm_render_ratio is None:
            return False
        return self.warm_render_ratio * len(parts[1]) <= 0.8 * len(parts[0])

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

    def _timed_post(self, endpoint, params, body, limit, timing):
        stage = endpoint[1:]
        timing['stage'] = stage
        start = time.perf_counter_ns()
        try:
            return self._post(endpoint, params, body, limit)
        finally:
            timing[stage + '_ms'] = round((time.perf_counter_ns() - start) / 1_000_000, 3)

    def create(self, text, output, *, on_pcm=None):
        parts = japanese_reply_parts(text) if self.viewer_reply else [text]
        if len(parts) == 2 and self.tail_socket and not self.can_release_lead(parts):
            return self._buffer_pair(parts, output, on_pcm)
        return self._single(text, output, on_pcm)

    def _buffer_pair(self, parts, output, on_pcm):
        # Independent local sockets. No audio is released until both jobs succeed.
        # A late or cancelled pair follows the controller's ordinary turn fence.
        tail = copy(self)
        tail.socket_path = self.tail_socket
        with tempfile.TemporaryDirectory(dir=Path(output).parent) as tmp:
            paths = []
            for side in ('lead', 'tail'):
                directory = Path(tmp)/side
                directory.mkdir()
                paths.append(directory/Path(output).name)
            with ThreadPoolExecutor(max_workers=1) as pool:
                second = pool.submit(tail._single, parts[1], paths[1], None, 'tail')
                self._single(parts[0], paths[0], None, 'lead')
                second.result()
            audio = bytearray()
            for path in paths:
                with wave.open(str(path)) as wav:
                    if len(audio) + 2*wav.getnframes() > 60*48000:
                        raise ProviderError('PROGRAM_AUDIO_TOO_LONG')
                    audio.extend(wav.readframes(wav.getnframes()))
            final = Path(tmp)/'joined.wav'
            with wave.open(str(final), 'wb') as wav:
                wav.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
                wav.writeframes(audio)
            final.replace(output)
        if on_pcm:
            on_pcm(bytes(audio))
        return {'source':'local-aivis-pair', 'voice':self.voice, 'duration':len(audio)/48000}

    def _single(self, text, output, on_pcm, part='whole'):
        filename = Path(output).name
        match = TURN_WAV.fullmatch(filename)
        kind = 'turn' if match else 'warmup' if filename == 'ready.wav' else 'other'
        timing = {'kind': kind, 'turn_id': match[1] if match else None,
                  'chunk': int(match[2]) if match else None, 'characters': len(text),
                  'audio_query_ms': None, 'synthesis_ms': None, 'stage': 'audio_query', 'outcome': 'error'}
        if part != 'whole':
            timing['part'] = part
        start = time.perf_counter_ns()
        try:
            result = self._render(text, output, on_pcm, timing)
            timing.update(stage='complete', outcome='ok')
            return result
        except Exception as exc:
            # Only record stable codes produced by the adapter. Callback exceptions may contain private text.
            code = str(exc) if isinstance(exc, ProviderError) else ''
            timing['error'] = (code if re.fullmatch(r'LOCAL_TTS_HTTP_[0-9]{3}|LOCAL_TTS_CONNECTION_FAILED|'
                                                     r'INVALID_TTS_RESULT|TURN_CANCELLED', code)
                               else type(exc).__name__)
            raise
        finally:
            timing['total_ms'] = round((time.perf_counter_ns() - start) / 1_000_000, 3)
            TIMING_LOG.info('KANON_TTS_TIMING %s', json.dumps(timing, separators=(',', ':')))

    def _render(self, text, output, on_pcm, timing):
        params = {'speaker': KANON_STYLE}
        try:
            raw_query = self._timed_post('/audio_query', dict(params, text=text), b'', 512 * 1024, timing)
            timing['stage'] = 'query_decode'
            query = json.loads(raw_query)
            query['outputSamplingRate'] = 24000
            query['outputStereo'] = False
            # Only the first viewer clip needs a fast onset. Let the engine preserve
            # the phonemes and natural pauses, including those between sentences.
            if self.viewer_reply and (on_pcm is not None or timing.get('part') == 'lead'):
                padding = query.get('prePhonemeLength')
                if isinstance(padding, (int, float)) and padding > .01:
                    query['prePhonemeLength'] = .01
            wav = self._timed_post('/synthesis', params, json.dumps(query).encode(), WAV_LIMIT, timing)
            timing['stage'] = 'wav_decode'
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
        timing['stage'] = 'wav_write'
        Path(output).write_bytes(wav)
        if on_pcm:
            timing['stage'] = 'pcm_callback'
            on_pcm(pcm)
        return {'source': 'local-aivis-cuda', 'voice': self.voice, 'duration': frames / 24000}
