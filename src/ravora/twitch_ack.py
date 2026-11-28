"""Sample Linux TCP_INFO on the socket opened for one Twitch publishing generation."""
from contextlib import contextmanager
import os
import socket
import stat
import struct
import threading
from urllib.parse import urlsplit


def sockets():
    found = {}
    try:
        for fd in os.listdir('/proc/self/fd'):
            try:
                entry = os.stat('/proc/self/fd/' + fd)
                if stat.S_ISSOCK(entry.st_mode):
                    found[entry.st_ino] = int(fd)
            except OSError:
                continue
    except OSError:
        pass
    return found


class TwitchPeer:
    def __init__(self, target):
        url = urlsplit(target)
        self.host, self.port = url.hostname, url.port or 443
        self.addresses = frozenset()
        self.resolver = None

    def _resolve(self):
        try:
            self.addresses = frozenset(addr[4][0] for addr in socket.getaddrinfo(
                self.host,self.port,type=socket.SOCK_STREAM))
        except OSError:
            self.addresses = frozenset()

    def before_open(self):
        # DNS validation is optional telemetry and never delays sending media.
        if not self.resolver or not self.resolver.is_alive():
            self.resolver = threading.Thread(target=self._resolve,daemon=True)
            try:
                self.resolver.start()
            except RuntimeError:
                self.addresses = frozenset()
        return set(sockets())

    @contextmanager
    def connected(self, before):
        if socket.getdefaulttimeout() is not None:
            # Adopting an fd would apply the global timeout to the shared socket.
            yield None
            return
        candidates = []
        try:
            for inode, fd in sockets().items():
                if inode in before:
                    continue
                try:
                    duplicate = os.dup(fd)
                    try:
                        opened = socket.socket(fileno=duplicate)
                    except OSError:
                        os.close(duplicate)
                        continue
                    candidate = opened
                    try:
                        if (os.fstat(candidate.fileno()).st_ino != inode or candidate.family not in
                                (socket.AF_INET,socket.AF_INET6) or candidate.type != socket.SOCK_STREAM):
                            continue
                        address, port = candidate.getpeername()[:2]
                        if port == self.port and address in self.addresses:
                            # socket.dup() applies the Python timeout to the shared open
                            # file description and changes FFmpeg's O_NONBLOCK flag.
                            candidates.append(candidate)
                            candidate = None
                    finally:
                        if candidate is not None:
                            candidate.close()
                except OSError:
                    continue
            yield AckProgress(candidates[0]) if len(candidates)==1 else None
        finally:
            for candidate in candidates:
                candidate.close()


class AckProgress:
    def __init__(self, connection):
        self.connection = connection
        self.last_sample = self.last_acked = self.pending_since = None
        self.samples = 0

    def sample(self, now):
        if self.last_sample is not None and now-self.last_sample < 1:
            return None
        gap = self.last_sample is None or now-self.last_sample > 2.5
        self.last_sample = now
        try:
            info = self.connection.getsockopt(socket.IPPROTO_TCP,socket.TCP_INFO,192)
            # linux/tcp.h: tcpi_bytes_acked, tcpi_unacked and tcpi_notsent_bytes.
            acked, = struct.unpack_from('=Q',info,120)
            pending = struct.unpack_from('=I',info,24)[0] or struct.unpack_from('=I',info,144)[0]
        except (OSError,AttributeError,struct.error):
            self.last_acked = self.pending_since = None
            self.samples = 0
            return 'unknown'
        advanced = self.last_acked is not None and acked > self.last_acked
        if gap or not pending or self.last_acked != acked:
            self.pending_since = None
            self.samples = 0
        self.last_acked = acked
        if pending and not advanced:
            if self.pending_since is None:
                self.pending_since = now
            self.samples += 1
            if self.samples >= 4 and now-self.pending_since >= 5:
                return 'stalled'
        return 'progress' if advanced else 'sampled'
