from contextlib import contextmanager
from fractions import Fraction
import json
import threading
import time
import unittest
from unittest.mock import patch
from types import SimpleNamespace

try:
    from ravora.live_transport import PublishError, PublishFIFO
except ModuleNotFoundError as exc:
    if exc.name != 'av':
        raise
    PublishFIFO = None


VIDEO = SimpleNamespace(index=0, type='video')
AUDIO = SimpleNamespace(index=1, type='audio')


def packet(timestamp, *, video=True, key=False):
    return SimpleNamespace(size=100, pts=timestamp, dts=timestamp, time_base=Fraction(1, 1000),
                           stream=VIDEO if video else AUDIO, is_keyframe=key)


class Sink:
    streams = (VIDEO, AUDIO)

    def __init__(self):
        self.packets = []
        self.wrote = threading.Event()

    def mux(self, value):
        self.packets.append((value.stream.type, value.pts, value.is_keyframe))
        self.wrote.set()


@unittest.skipUnless(PublishFIFO, 'youtube-live extra required')
class TransportTests(unittest.TestCase):
    def test_overflow_discards_stale_gop_and_rebases_both_tracks_on_video(self):
        gate = threading.Event()
        release = threading.Event()
        class PausedConsumer(PublishFIFO):
            def _take(self):
                release.wait(2)
                return super()._take()
        sink = Sink()

        @contextmanager
        def open_output():
            gate.wait(2)
            yield sink

        fifo = PausedConsumer(open_output, capacity=3)
        try:
            self.assertFalse(fifo.take_keyframe_request())
            fifo.put(packet(0, key=True))
            self.assertEqual(fifo.status()['queue_packets'], 0)
            gate.set()
            deadline = time.monotonic() + 2
            while fifo.status()['state'] != 'waiting_keyframe' and time.monotonic() < deadline:
                time.sleep(.001)
            self.assertTrue(fifo.take_keyframe_request())
            self.assertFalse(fifo.take_keyframe_request())
            fifo.put(packet(6000, key=True))
            fifo.put(packet(6033))
            fifo.put(packet(6048, video=False, key=True))
            fifo.put(packet(6066))
            fifo.put(packet(6100, video=False, key=True))
            self.assertTrue(fifo.take_keyframe_request())
            fifo.put(packet(8000, key=True))
            fifo.put(packet(7980, video=False, key=True))
            fifo.put(packet(8012, video=False, key=True))
            self.assertEqual(fifo.status()['queue_packets'], 3)
        finally:
            gate.set()
            release.set()
            fifo.close()
        self.assertEqual(sink.packets, [('video', 0, True), ('audio', 12, True)])
        self.assertEqual(fifo.status()['packets_dropped'], 7)
        self.assertEqual(fifo.status()['queue_overflows'], 1)
        self.assertFalse(fifo.thread.is_alive())

    def test_encoder_starts_a_slow_connection_mid_gop_with_a_decodable_keyframe(self):
        import io
        import av
        try:
            from ravora.live_media import ProgramWriter, SILENCE, Stage
        except ModuleNotFoundError as exc:
            if exc.name not in ('numpy', 'PIL'):
                raise
            self.skipTest('youtube-live media extras required')
        from ravora.live_transport import rtmps_output

        output = io.BytesIO()
        gate = threading.Event()
        frame = av.VideoFrame(160, 96, 'yuv420p')
        for plane in frame.planes:
            plane.update(bytes([90]) * plane.buffer_size)
        @contextmanager
        def connect(target, streams):
            gate.wait(3)
            with rtmps_output(output, streams) as sink:
                yield sink
        with patch('ravora.live_transport.rtmps_output', connect), patch('ravora.live_media.SIZE', (160, 96)):
            writer = ProgramWriter('test', SimpleNamespace(render=lambda stage: frame),
                                   publish=True, video_encoder='libx264')
        try:
            for _ in range(15):
                writer.write(Stage(), SILENCE)
            self.assertEqual(writer.transport.status()['queue_packets'], 0)
            gate.set()
            deadline = time.monotonic() + 2
            while writer.transport.status()['state'] != 'waiting_keyframe' and time.monotonic() < deadline:
                time.sleep(.001)
            for _ in range(24):
                writer.write(Stage(), SILENCE)
        finally:
            gate.set()
            writer.close()
        output.seek(0)
        with av.open(output) as reader:
            decoded = list(reader.decode(video=0))
        self.assertEqual(len(decoded), 24)
        self.assertEqual(decoded[0].pts, 0)
        self.assertTrue(decoded[0].key_frame)
        self.assertFalse(any(frame.key_frame for frame in decoded[1:]))

    def test_flapping_single_packet_connections_exhaust_one_budget(self):
        entered = threading.Event()
        class Flapping(Sink):
            def mux(self, value):
                if self.packets:
                    raise BrokenPipeError('synthetic-private-destination')
                super().mux(value)
        @contextmanager
        def open_output():
            entered.set()
            yield Flapping()

        fifo = PublishFIFO(open_output, max_retries=2, retry_wait=.001)
        try:
            for i in range(3):
                self.assertTrue(entered.wait(2))
                entered.clear()
                # Opening and enabling admission happen on the same worker.
                deadline = time.monotonic() + 2
                while fifo.status()['state'] != 'waiting_keyframe' and time.monotonic() < deadline:
                    time.sleep(.001)
                fifo.put(packet(i*2000, key=True))
                fifo.put(packet(i*2000+33))
            fifo.thread.join(2)
            self.assertFalse(fifo.thread.is_alive())
            with self.assertRaisesRegex(PublishError, 'PUBLISH_RECOVERY_EXHAUSTED'):
                fifo.put(packet(9000, key=True))
        finally:
            with self.assertRaisesRegex(PublishError, 'PUBLISH_RECOVERY_EXHAUSTED'):
                fifo.close()
        state = fifo.status()
        self.assertEqual((state['attempts'], state['connections_with_media'], state['writes_unknown']), (3, 3, 3))
        self.assertNotIn('synthetic-private-destination', json.dumps(state))

    def test_stop_interrupts_retry_and_releases_single_writer(self):
        failed = threading.Event()
        def unavailable():
            failed.set()
            raise ConnectionRefusedError()
        fifo = PublishFIFO(unavailable, retry_wait=60)
        self.assertTrue(failed.wait(2))
        deadline = time.monotonic() + 2
        while fifo.status()['state'] != 'retry_wait' and time.monotonic() < deadline:
            time.sleep(.001)
        fifo.close()
        self.assertEqual(fifo.status()['state'], 'stopped_during_recovery')
        self.assertEqual(fifo.status()['attempts'], 1)
        self.assertFalse(fifo.thread.is_alive())

    def test_access_denied_is_terminal_without_exposing_the_native_message(self):
        def denied():
            raise PermissionError('private destination')
        fifo = PublishFIFO(denied)
        fifo.thread.join(2)
        with self.assertRaisesRegex(PublishError, '^PUBLISH_ACCESS_DENIED$'):
            fifo.close()
        self.assertEqual(fifo.status()['attempts'], 1)
        self.assertNotIn('private destination', json.dumps(fifo.status()))

    def test_shutdown_deadline_marks_a_stuck_open_and_never_retries_it(self):
        opening, release = threading.Event(), threading.Event()

        @contextmanager
        def slow_open():
            opening.set()
            release.wait(2)
            yield Sink()

        fifo = PublishFIFO(slow_open)
        try:
            self.assertTrue(opening.wait(2))
            with patch('ravora.live_transport.CLOSE_TIMEOUT', .02):
                with self.assertRaisesRegex(PublishError, 'PUBLISH_CLOSE_TIMEOUT'):
                    fifo.close()
            self.assertEqual(fifo.status()['state'], 'failed')
        finally:
            release.set()
            fifo.thread.join(2)
        self.assertFalse(fifo.thread.is_alive())
        self.assertEqual(fifo.status()['attempts'], 1)

    def test_encoded_program_reconnects_to_loopback_and_both_tracks_decode(self):
        for encoder, codec in (('libx265', 'hevc'), ('libx264', 'h264')):
            with self.subTest(encoder=encoder):
                self.verify_loopback_reconnect(encoder, codec)

    def verify_loopback_reconnect(self, encoder, codec):
        import io
        import socket
        import struct
        import av
        try:
            import numpy as np
            from ravora.live_media import ProgramWriter, RATE, SAMPLES, Stage
        except ModuleNotFoundError as exc:
            if exc.name not in ('numpy', 'PIL'):
                raise
            self.skipTest('youtube-live media extras required')

        receiver_errors = []
        recording = bytearray()
        received_again = threading.Event()
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            listener.listen(1)
            listener.settimeout(8)

            def receive():
                try:
                    first, _ = listener.accept()
                    with first:
                        first.settimeout(5)
                        total = 0
                        while total < 80_000:
                            chunk = first.recv(65536)
                            if not chunk:
                                raise EOFError('first connection ended early')
                            total += len(chunk)
                        # Abort just this test connection while the encoder is running.
                        first.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack('ii', 1, 0))
                    second, _ = listener.accept()
                    with second:
                        second.settimeout(5)
                        while chunk := second.recv(65536):
                            recording.extend(chunk)
                            if len(recording) > 200_000:
                                received_again.set()
                except Exception as exc:
                    receiver_errors.append(exc)

            reader = threading.Thread(target=receive)
            reader.start()
            image = av.VideoFrame.from_ndarray(np.full((180, 320, 3), 90, dtype='uint8'), format='rgb24')
            scene = SimpleNamespace(render=lambda stage: image.reformat(format='yuv420p'))
            sound = (6000 * np.sin(np.arange(SAMPLES) * (2 * np.pi * 450 / RATE))).astype('<i2').tobytes()
            # The synthetic receiver captures the FLV muxer's exact bytes before TLS.
            with patch('ravora.live_media.SIZE', (320, 180)):
                writer = ProgramWriter(f'tcp://127.0.0.1:{listener.getsockname()[1]}', scene,
                                       publish=True, video_encoder=encoder)
            try:
                deadline = time.monotonic() + 7
                while not received_again.is_set() and time.monotonic() < deadline:
                    writer.write(Stage(), sound)
                    time.sleep(.012)
            finally:
                try:
                    writer.close()
                finally:
                    reader.join(8)
        self.assertFalse(reader.is_alive())
        self.assertEqual(receiver_errors, [])
        self.assertTrue(received_again.is_set())
        self.assertEqual(writer.transport.status()['connections_with_media'], 2)
        with av.open(io.BytesIO(recording)) as received:
            self.assertEqual(received.streams.video[0].codec_context.name, codec)
            self.assertEqual(received.streams.audio[0].codec_context.name, 'aac')
            frames = list(received.decode(video=0))
        self.assertGreater(len(frames), 3)
        self.assertTrue(frames[0].key_frame)
        self.assertEqual(frames[0].pts, 0)
        self.assertTrue(all(a.pts < b.pts for a, b in zip(frames, frames[1:])))
        with av.open(io.BytesIO(recording)) as received:
            self.assertTrue(any(np.abs(f.to_ndarray()).max() > .01 for f in received.decode(audio=0)))
