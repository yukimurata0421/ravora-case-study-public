"""Bounded RTMPS packet FIFO. Only its consumer owns the network muxer."""
from collections import deque
from contextlib import contextmanager
import ssl
import threading
import time

import av

from ravora.youtube_auth import YouTubeError

OPEN_TIMEOUT = 6
IO_TIMEOUT = 5
DRAIN_SECONDS = 2
# An open, final in-flight write and trailer may each use their full deadline.
CLOSE_TIMEOUT = OPEN_TIMEOUT + 2 * IO_TIMEOUT + DRAIN_SECONDS + 1


class PublishError(YouTubeError):
    """Operator-safe code. Native I/O exceptions may contain the stream key."""


@contextmanager
def rtmps_output(target, streams):
    options = {'tls_verify': '1', 'rw_timeout': str(IO_TIMEOUT * 1_000_000),
               'flvflags': 'no_duration_filesize', 'flush_packets': '1'}
    if streams[0].codec_context.codec.canonical_name == 'hevc':
        options['rtmp_enhanced_codecs'] = 'hvc1'
    cafile = ssl.get_default_verify_paths().cafile
    if cafile:
        options['ca_file'] = cafile
    with av.open(target, 'w', format='flv', options=options, timeout=(OPEN_TIMEOUT, IO_TIMEOUT)) as output:
        for stream in streams:
            output.add_stream_from_template(stream, opaque=True)
        output.start_encoding()
        yield output


class PublishFIFO:
    def __init__(self, open_output, *, capacity=120, retry_wait=1., max_retries=4):
        self.open_output = open_output
        self.capacity = capacity
        self.retry_wait = retry_wait
        self.max_retries = max_retries
        self.muxed_bytes = self.io_errors = 0
        self.condition = threading.Condition()
        self.packets = deque()
        self.need_keyframe = True
        self.keyframe_requested = False
        self.accepting = False
        self.closing_at = None
        self.error = None
        self.state = 'connecting'
        self.last_io_error = self.last_mux_at = None
        self.overflow_events = self.write_outcome_unknown = 0
        self.attempts = self.connections = self.dropped = self.muxed = 0
        self.thread = threading.Thread(target=self._run, name='ray-rtmps', daemon=True)
        self.thread.start()

    def status(self):
        with self.condition:
            return {'state': self.state, 'attempts': self.attempts,
                    'connections_with_media': self.connections, 'packets_muxed': self.muxed,
                    'packets_dropped': self.dropped, 'queue_packets': len(self.packets),
                    'muxed_bytes': self.muxed_bytes, 'io_errors': self.io_errors,
                    'queue_limit': self.capacity, 'queue_overflows': self.overflow_events,
                    'writes_unknown': self.write_outcome_unknown,
                    'last_mux_age_seconds': None if self.last_mux_at is None else round(time.monotonic() - self.last_mux_at, 2),
                    'last_io_error': self.last_io_error, 'error': self.error}

    def _discard(self):
        self.dropped += len(self.packets)
        self.packets.clear()
        self.need_keyframe = True
        self.keyframe_requested = False

    def take_keyframe_request(self):
        with self.condition:
            if self.accepting and self.need_keyframe and not self.keyframe_requested:
                self.keyframe_requested = True
                return True
            return False

    def put(self, packet):
        with self.condition:
            if self.error:
                raise PublishError(self.error)
            if self.closing_at is not None:
                raise PublishError('PUBLISH_CLOSED')
            if len(self.packets) >= self.capacity:
                self.overflow_events += 1
                self._discard()
            if not self.accepting or self.need_keyframe and not (packet.stream.type == 'video' and packet.is_keyframe):
                self.dropped += 1
                return
            self.need_keyframe = False
            self.packets.append(packet)
            self.condition.notify()

    def _take(self):
        with self.condition:
            self.condition.wait_for(lambda: self.packets or self.closing_at is not None)
            if self.closing_at is not None and time.monotonic() >= self.closing_at + DRAIN_SECONDS:
                self._discard()
            return self.packets.popleft() if self.packets else None

    def _run(self):
        failures = 0
        try:
            while True:
                with self.condition:
                    if self.closing_at is not None and (failures or not self.packets):
                        break
                    self.state = 'connecting'
                    self.attempts += 1
                stage = 'PUBLISH_OPEN_IO'
                try:
                    with self.open_output() as output:
                        with self.condition:
                            if self.error:
                                break
                            self.accepting = True
                            self.state = 'waiting_keyframe'
                        origin = previous_write = None
                        stable_since = None
                        stage = 'PUBLISH_WRITE_IO'
                        while (packet := self._take()) is not None:
                            # Rebase both streams to the first video keyframe of this connection.
                            if origin is None:
                                origin = packet.dts * packet.time_base
                            offset = round(origin / packet.time_base)
                            if packet.dts < offset:
                                with self.condition:
                                    self.dropped += 1
                                continue
                            packet.dts -= offset
                            packet.pts -= offset
                            packet.stream = output.streams[packet.stream.index]
                            size = packet.size
                            output.mux(packet)
                            now = time.monotonic()
                            with self.condition:
                                if previous_write is None:
                                    self.connections += 1
                                if previous_write is None or now - previous_write > 1:
                                    stable_since = now
                                previous_write = now
                                if not self.error:
                                    self.state = 'writing'
                                self.muxed += 1
                                self.muxed_bytes += size
                                self.last_mux_at = now
                            # A handshake or one packet cannot renew a flapping link's budget.
                            if now - stable_since >= 2:
                                failures = 0
                        stage = 'PUBLISH_CLOSE_IO'
                    break
                except (OSError, av.FFmpegError) as exc:
                    with self.condition:
                        self.io_errors += 1
                        self.last_io_error = stage
                        if stage == 'PUBLISH_WRITE_IO':
                            self.write_outcome_unknown += 1
                        self.accepting = False
                        self._discard()
                        if self.closing_at is not None:
                            self.error = self.error or stage
                            break
                        failures += 1
                        if isinstance(exc, PermissionError) or failures > self.max_retries:
                            self.error = 'PUBLISH_ACCESS_DENIED' if isinstance(exc, PermissionError) else 'PUBLISH_RECOVERY_EXHAUSTED'
                            break
                        self.state = 'retry_wait'
                        self.condition.wait_for(lambda: self.closing_at is not None, timeout=self.retry_wait)
        except Exception:
            with self.condition:
                self.error = self.error or 'PUBLISH_CONFIGURATION_OR_MUX_FAILED'
        finally:
            with self.condition:
                self._discard()
                if self.error:
                    self.state = 'failed'
                elif self.muxed and self.state == 'writing':
                    self.state = 'closed'
                elif self.last_io_error:
                    self.state = 'stopped_during_recovery'
                else:
                    self.state = 'stopped_without_media'
                self.condition.notify_all()

    def close(self):
        with self.condition:
            if self.closing_at is None:
                self.closing_at = time.monotonic()
            self.condition.notify_all()
        self.thread.join(timeout=CLOSE_TIMEOUT)
        with self.condition:
            if self.thread.is_alive():
                self.accepting = False
                self._discard()
                self.error = 'PUBLISH_CLOSE_TIMEOUT'
                self.state = 'failed'
        if self.error:
            raise PublishError(self.error)
