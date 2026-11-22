"""Operator-controlled Dell microphone for the live program mixer."""
import json
import os
from pathlib import Path
import re
import select
import stat
import subprocess
import sys
import threading
import time

from ravora.dell_microphone import capture_nodes
from ravora.developer_http import RequestError
from ravora.live_handsfree import host_wave
from ravora.live_media import RATE
from ravora.live_mic import PACKET_BYTES


def shared_source(pcm_node):
    card, device = re.fullmatch(r'pcmC(\d+)D(\d+)c', pcm_node.name).groups()
    runtime = Path(os.environ.get('XDG_RUNTIME_DIR', f'/run/user/{os.getuid()}')) / 'pulse'
    machine = Path('/etc/machine-id').read_text().strip()
    config = Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config'))
    paths = [runtime, config / 'pulse' / f'{machine}-runtime']
    for path in dict.fromkeys(p.resolve() / 'native' for p in paths):
        try:
            info = path.stat()
        except OSError:
            continue
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
            continue
        server = 'unix:' + str(path)
        try:
            result = subprocess.run(['/usr/bin/pactl', '--server=' + server, '-f', 'json', 'list', 'sources'],
                                    capture_output=True, timeout=2, check=True)
            sources = json.loads(result.stdout)
        except (OSError, ValueError, subprocess.SubprocessError):
            continue
        for source in sources:
            props = source.get('properties', {})
            if (props.get('device.class') == 'sound' and str(props.get('alsa.card')) == card
                    and str(props.get('alsa.device')) == device):
                return ['/usr/bin/parec', '--server=' + server, '--device=' + source['name'],
                        '--raw', '--format=s16le', '--channels=1', '--rate=48000',
                        '--latency-msec=80', '--process-time-msec=20']
    return None


def capture_command(device):
    nodes = capture_nodes(device)
    if not nodes:
        raise RequestError('DELL_MIC_NOT_FOUND', 503)
    shared = shared_source(nodes[1])
    if shared:
        return shared
    command = ['/usr/bin/ffmpeg', '-nostdin', '-loglevel', 'error', '-probesize', '32',
               '-analyzeduration', '1', '-f', 'alsa', '-sample_rate', str(RATE), '-ch_layout', 'mono',
               '-i', 'plughw:' + device, '-ac', '1', '-ar', str(RATE), '-f', 's16le',
               '-flush_packets', '1', 'pipe:1']
    if not all(os.access(node, os.R_OK | os.W_OK) for node in nodes):
        command = ['sudo', '-n', '-g', 'audio', '--', *command]
    return command


def packet(pipe, timeout):
    data = bytearray()
    end = time.monotonic() + timeout
    while len(data) < PACKET_BYTES:
        if not select.select([pipe], [], [], max(0, end - time.monotonic()))[0]:
            raise RequestError('DELL_MIC_TIMED_OUT', 503)
        part = os.read(pipe.fileno(), PACKET_BYTES - len(data))
        if not part:
            raise RequestError('DELL_MIC_UNAVAILABLE', 503)
        data.extend(part)
    return bytes(data)


def stop_child(proc):
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


class DellCapture:
    """A tab renews a short lease. Only the capture thread reads the hardware."""
    def __init__(self, device, mixer, max_seconds):
        self.device = device
        self.mixer = mixer
        self.max_bytes = max_seconds * RATE * 2
        self.lock = threading.RLock()
        self.owner = self.child = self.question = None
        self.question_language = None
        self.expires = 0
        self.closed = False

    def prepare(self):
        # Any password prompt belongs to the launching SSH terminal.
        if capture_command(self.device)[0] == 'sudo' and sys.stdin.isatty():
            if subprocess.run(['sudo', '-g', 'audio', '-v']).returncode:
                raise ValueError('DELL_MIC_PERMISSION_REQUIRED')

    def _start(self, command):
        proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, bufsize=0)
        try:
            return proc, packet(proc.stdout, 3)
        except BaseException:
            stop_child(proc)
            proc.stdout.close()
            raise

    def open(self, *, automatic=False):
        with self.lock:
            if self.closed:
                raise RequestError('PROGRAM_STOPPED', 409)
            if self.owner and time.monotonic() < self.expires:
                raise RequestError('MIC_ALREADY_OPEN', 409)
            self.mute(self.owner)
            command = capture_command(self.device)
            try:
                proc, first = self._start(command)
            except RequestError:
                # ALSA can activate a sharing daemon during its first open.
                retry = capture_command(self.device)
                if command == retry or retry[0] != '/usr/bin/parec':
                    raise
                proc, first = self._start(retry)
            try:
                owner = self.mixer.open(automatic=automatic)
                self.owner, self.child = owner, proc
                self.expires = time.monotonic() + 3
                self.mixer.push(owner, 0, first)
                threading.Thread(target=self._capture, args=(owner, proc), daemon=True).start()
            except BaseException:
                self.mute(self.owner)
                stop_child(proc)
                proc.stdout.close()
                raise
            return owner

    def _capture(self, owner, proc):
        sequence = 1
        try:
            while True:
                data = packet(proc.stdout, 1.5)
                with self.lock:
                    self._check(owner)
                    self.mixer.push(owner, sequence, data)
                    sequence += 1
                    if self.question is not None and len(self.question) < self.max_bytes:
                        self.question.extend(data)
        except (OSError, RequestError):
            pass
        finally:
            self.mute(owner)
            proc.stdout.close()

    def _check(self, owner):
        if not owner or owner != self.owner or time.monotonic() >= self.expires:
            raise RequestError('MIC_SESSION_EXPIRED', 409)

    def heartbeat(self, owner):
        with self.lock:
            self._check(owner)
            self.expires = time.monotonic() + 3

    def mute(self, owner):
        with self.lock:
            if owner and owner == self.owner:
                self.mixer.mute(owner)
                self.owner = self.question = None
                self.question_language = None
                stop_child(self.child)
                self.child = None

    def close(self):
        with self.lock:
            self.closed = True
            self.mute(self.owner)

    def start_question(self, owner, *, language=None):
        with self.lock:
            self._check(owner)
            if self.question is not None:
                raise RequestError('HOST_QUESTION_ALREADY_STARTED', 409)
            self.question = bytearray()
            self.question_language = language

    def finish_question(self, owner):
        with self.lock:
            self._check(owner)
            pcm, self.question = self.question, None
            language, self.question_language = self.question_language, None
        if not pcm or len(pcm) < 2 * PACKET_BYTES:
            raise RequestError('HOST_QUESTION_TOO_SHORT', 422)
        return host_wave(pcm), language
