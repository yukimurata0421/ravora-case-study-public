"""Stream one explicitly selected ALSA capture device into the developer session."""
from contextlib import asynccontextmanager
from pathlib import Path
import re
import sys


def parse_device(device):
    match = isinstance(device, str) and re.fullmatch(r'([A-Za-z0-9_-]{1,16}),([0-9]{1,3})', device)
    if not match:
        raise ValueError('INVALID_DELL_DEVICE')
    return match.groups()


def capture_nodes(device):
    """Resolve the operator's ALSA card ID and capture PCM for the private /dev."""
    card_id, pcm = parse_device(device)
    for card in Path('/sys/class/sound').glob('card[0-9]*'):
        index = card.name[4:]
        try:
            if index != card_id and (card / 'id').read_text().strip() != card_id:
                continue
        except FileNotFoundError:  # USB capture device unplugged during discovery.
            continue
        control = Path(f'/dev/snd/controlC{index}')
        capture = Path(f'/dev/snd/pcmC{index}D{int(pcm)}c')
        if control.is_char_device() and capture.is_char_device():
            return control, capture
    return None


@asynccontextmanager
async def open_microphone(device):
    import asyncio
    from ravora.edge_grpc import EdgeError
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, '-I', '-S', __file__, device, env={},
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, limit=6400)
    except OSError as exc:
        raise EdgeError('DELL_MIC_UNAVAILABLE') from exc
    try:
        yield proc.stdout
    finally:
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        # Drain pipe data already in flight so wait() can also reap a paused transport.
        await proc.communicate()


def record_pcm(device):
    """Blocking ALSA reader in a disposable child. Its only output is raw PCM."""
    import ctypes as c
    import os

    alsa = c.CDLL('libasound.so.2')
    pcm = c.c_void_p()
    alsa.snd_pcm_open.argtypes = [c.POINTER(c.c_void_p), c.c_char_p, c.c_int, c.c_int]
    alsa.snd_pcm_set_params.argtypes = [c.c_void_p, c.c_int, c.c_int, c.c_uint, c.c_uint, c.c_int, c.c_uint]
    alsa.snd_pcm_readi.argtypes = [c.c_void_p, c.c_void_p, c.c_ulong]
    alsa.snd_pcm_readi.restype = c.c_long
    alsa.snd_pcm_close.argtypes = [c.c_void_p]
    alsa.snd_pcm_format_value.argtypes = [c.c_char_p]
    capture, rw_interleaved = 1, 3  # snd_pcm_stream_t and snd_pcm_access_t in alsa/pcm.h
    if alsa.snd_pcm_open(c.byref(pcm), f'plughw:{device}'.encode('ascii'), capture, 0) < 0:
        return 1
    try:
        fmt = alsa.snd_pcm_format_value(b'S16_LE')
        if alsa.snd_pcm_set_params(pcm, fmt, rw_interleaved, 1, 16000, 1, 80000) < 0:
            return 1
        # Read 20 ms at a time. The 80 ms device buffer absorbs scheduling jitter.
        frames = 320
        buffer = c.create_string_buffer(frames * 2)
        while True:
            count = alsa.snd_pcm_readi(pcm, buffer, frames)
            if count <= 0:  # Interruptions and overruns terminate this input.
                return 1
            os.write(1, buffer.raw[:count * 2])  # One atomic write within PIPE_BUF.
    finally:
        alsa.snd_pcm_close(pcm)


if __name__ == '__main__':
    raise SystemExit(record_pcm(sys.argv[1]))
