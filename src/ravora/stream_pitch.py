"""Pitch-shift 24 kHz mono PCM16 in provider packet order, keeping its time base."""
import ctypes as C
from functools import lru_cache
import struct


@lru_cache(maxsize=1)
def load_live_shifter():
    # Rubber Band 4's LiveShifter C API. Only the pitch-enabled path needs it.
    state = C.c_void_p
    channels = C.POINTER(C.POINTER(C.c_float))
    try:
        lib = C.CDLL('librubberband.so.3')
        for name, result, args in (
            ('new', state, [C.c_uint, C.c_uint, C.c_int]),
            ('delete', None, [state]),
            ('set_pitch_scale', None, [state, C.c_double]),
            ('get_block_size', C.c_uint, [state]),
            ('get_start_delay', C.c_uint, [state]),
            ('shift', None, [state, channels, channels]),
        ):
            function = getattr(lib, 'rubberband_live_' + name)
            function.restype = result
            function.argtypes = args
    except (OSError, AttributeError) as exc:
        raise RuntimeError('RUBBERBAND_LIVE_SHIFTER_UNAVAILABLE') from exc
    return lib


async def pitch_shift_pcm(packets, semitones):
    if semitones == 0:
        async for pcm in packets:
            yield pcm
        return

    lib = load_live_shifter()
    # OptionWindowShort | OptionFormantPreserved keeps the speaker's vocal timbre.
    state = lib.rubberband_live_new(24000, 1, 0x01000000)
    if not state:
        raise RuntimeError('RUBBERBAND_LIVE_SHIFTER_UNAVAILABLE')
    try:
        lib.rubberband_live_set_pitch_scale(state, 2 ** (semitones / 12))
        frames = lib.rubberband_live_get_block_size(state)
        discard = lib.rubberband_live_get_start_delay(state)
        pcm16 = struct.Struct(f'<{frames}h')
        source = (C.c_float * frames)()
        output = (C.c_float * frames)()
        input_channel = C.pointer(C.cast(source, C.POINTER(C.c_float)))
        output_channel = C.pointer(C.cast(output, C.POINTER(C.c_float)))
        pending = bytearray()
        owed = 0

        def shift(block, offset=0):
            nonlocal discard, owed
            source[:] = [sample / 32768 for sample in pcm16.unpack_from(block, offset)]
            lib.rubberband_live_shift(state, input_channel, output_channel)
            skip = min(discard, frames)
            discard -= skip
            count = min(owed, frames - skip)
            owed -= count
            samples = (max(-32768, min(32767, round(x * 32768))) for x in output[skip:skip + count])
            return struct.pack(f'<{count}h', *samples)

        async for packet in packets:
            if len(packet) % 2:
                raise ValueError('ODD_PCM_BYTES')
            # A single RPC response can contain seconds of speech. Deliver each
            # 100 ms as it is converted so playout can begin during synthesis.
            view = memoryview(packet)
            for start in range(0, len(view), 4800):
                chunk = view[start:start + 4800]
                owed += len(chunk) // 2
                pending.extend(chunk)
                complete = len(pending) // pcm16.size * pcm16.size
                converted = b''.join(shift(pending, i) for i in range(0, complete, pcm16.size))
                del pending[:complete]
                if converted:
                    yield converted
        # Only a clean EOF drains the delay line. No tail on cancellation or RPC failure.
        tail = bytearray()
        while owed:
            tail.extend(shift(pending.ljust(pcm16.size, b'\0')))
            pending.clear()
        if tail:
            yield bytes(tail)
    finally:
        lib.rubberband_live_delete(state)
