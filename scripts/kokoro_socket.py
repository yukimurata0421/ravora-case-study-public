"""Serve English Kokoro on a private Unix socket, using a single warmed CUDA session."""
from collections import Counter
from http.server import BaseHTTPRequestHandler
import io
import json
import os
from pathlib import Path
import socketserver
import wave

from kokoro_onnx import Kokoro
import numpy as np
import onnxruntime as ort


VOICE = 'ray-kanon-en-v1'
LEGACY_VOICE = 'af_heart'
# The previous Ray voice used 85% jf_tebukuro. Halfway back to the
# original af_heart leaves 42.5% jf_tebukuro in the synthesis style.
KANON_BLEND = 0.425
TUNING_REVISION = 'us-midpoint-20261127'
KANON_GAIN = 10 ** (3.5 / 20)
CUDA_ARENA_LIMIT = 3 * 1024**3
SOCKET = Path('/socket/kokoro.sock')
MODEL = Path('/model')


def waveform(engine, text, voice=VOICE):
    samples, rate = engine.create(text, voice, lang='en-us')
    if rate != 24_000 or not 0 < samples.size <= 60 * rate or not np.isfinite(samples).all():
        raise ValueError('Invalid audio')
    if voice == VOICE:
        # Match the relative level of the Japanese voice with a soft peak ceiling.
        level = np.abs(samples) * KANON_GAIN
        level = np.minimum(level, 0.80) + 0.19 * np.tanh(np.maximum(level - 0.80, 0.0) / 0.19)
        samples = np.copysign(level, samples)
    pcm = np.rint(np.clip(samples, -1, 1) * 32767).astype('<i2')
    audio = io.BytesIO()
    with wave.open(audio, 'wb') as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(rate)
        f.writeframes(pcm.tobytes())
    return audio.getvalue()


def load_engine():
    options = ort.SessionOptions()
    options.intra_op_num_threads = 2
    options.inter_op_num_threads = 1
    options.enable_profiling = True
    options.profile_file_prefix = '/tmp/kokoro-warmup'
    options.log_severity_level = 3
    gpu = {'device_id': 0, 'gpu_mem_limit': CUDA_ARENA_LIMIT, 'arena_extend_strategy': 'kSameAsRequested',
           'cudnn_conv_algo_search': 'HEURISTIC', 'cudnn_conv_use_max_workspace': '0'}
    session = ort.InferenceSession(str(MODEL/'model.onnx'), options, providers=[('CUDAExecutionProvider', gpu)])
    session.disable_fallback()
    if session.get_providers()[0] != 'CUDAExecutionProvider':
        raise RuntimeError('Kokoro CUDA provider unavailable')
    def style(name):
        data = np.fromfile(MODEL/(name+'.bin'), dtype='<f4')
        if data.size != 510 * 256 or not np.isfinite(data).all():
            raise ValueError('Invalid Kokoro voice style')
        return data.reshape(-1, 1, 256)
    heart = style(LEGACY_VOICE)
    ray = KANON_BLEND * style('jf_tebukuro') + (1 - KANON_BLEND) * heart
    styles = '/tmp/kokoro-voice.npz'
    np.savez(styles, **{VOICE: ray, LEGACY_VOICE: heart})
    vocab = json.loads((MODEL/'tokenizer.json').read_bytes())['model']
    engine = Kokoro.from_session(session, styles, vocab_config=vocab)
    wav = waveform(engine, 'Welcome to the stream.')
    with wave.open(io.BytesIO(wav)) as f:
        if not np.any(np.abs(np.frombuffer(f.readframes(f.getnframes()), '<i2').astype(np.int32)) > 64):
            raise RuntimeError('Silent Kokoro warm-up')
    profile = Path(session.end_profiling())
    try:
        operations = Counter(event['args']['op_name'] for event in json.loads(profile.read_bytes())
                             if event.get('args', {}).get('provider') == 'CUDAExecutionProvider')
    finally:
        profile.unlink(missing_ok=True)
    if not operations.keys() & {'Conv', 'FusedConv', 'LSTM', 'Gemm', 'MatMul', 'FusedMatMul'}:
        raise RuntimeError('No CUDA compute kernels in Kokoro warm-up')
    return engine, dict(operations)


class Synthesis(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(8)

    def log_message(self, *args):
        pass

    def respond(self, status, body, content_type, *, voice=None):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        if voice:
            self.send_header('X-Kokoro-Voice', voice)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == '/health':
            self.respond(200, self.server.health, 'application/json')
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path != '/synthesis':
            self.send_error(404)
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 8192:
                self.send_error(413)
                return
            request = json.loads(self.rfile.read(length))
            text = request['text']
            voice = request.get('voice', VOICE)
            if voice not in (VOICE, LEGACY_VOICE):
                raise ValueError
            if not isinstance(text, str) or not 0 < len(text.strip()) <= 500:
                raise ValueError
        except (KeyError, ValueError, TypeError, UnicodeError):
            self.send_error(400)
            return
        try:
            data = waveform(self.server.engine, text, voice)
        except ValueError:
            self.send_error(422)
            return
        except Exception:
            self.send_error(503)
            return
        self.respond(200, data, 'audio/wav', voice=voice)


if __name__ == '__main__':
    os.umask(0o077)
    engine, operations = load_engine()
    health = {'model': 'Kokoro-82M-v1.0', 'voice': VOICE, 'provider': 'CUDAExecutionProvider', 'unqualified_voice': VOICE,
              'tuning_revision': TUNING_REVISION, 'language': 'en-us',
              'style_weights': {'jf_tebukuro': KANON_BLEND, 'af_heart': 1 - KANON_BLEND},
              'cuda_arena_limit_bytes': CUDA_ARENA_LIMIT, 'warmup_cuda_kernels': sum(operations.values()), 'cuda_ops': operations}
    SOCKET.unlink(missing_ok=True)
    try:
        with socketserver.UnixStreamServer(str(SOCKET), Synthesis) as server:
            server.engine = engine
            server.health = json.dumps(health).encode()
            print(f"Kokoro ready on CUDA, voice {VOICE}, {sum(operations.values())} CUDA kernels", flush=True)
            server.serve_forever()
    finally:
        SOCKET.unlink(missing_ok=True)
