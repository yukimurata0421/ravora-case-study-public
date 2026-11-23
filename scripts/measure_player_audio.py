#!/usr/bin/env python3
"""Capture a public Twitch player's PCM privately and match a local reply excerpt."""
import argparse
from contextlib import ExitStack
from datetime import datetime
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import threading
import time
import urllib.request
import wave


RATE = 48_000
MATCH_RATE = 8_000
FRAME_BYTES = 4  # Stereo signed 16-bit PCM.


def stop(child):
    if child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=4)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()


def record(pipe, output):
    end = 0
    with (output / 'player.pcm').open('wb') as audio, (output / 'clock.jsonl').open('w') as clock:
        while chunk := os.read(pipe.fileno(), 4_096):
            now = time.time()
            end += len(chunk)
            audio.write(chunk)
            clock.write(json.dumps([end, now]) + '\n')


def wait_file(path, child):
    expiry = time.monotonic() + 10
    while not path.exists() and child.poll() is None and time.monotonic() < expiry:
        time.sleep(.05)
    if not path.exists() or child.poll() is not None:
        raise RuntimeError('CAPTURE_PROCESS_NOT_READY')


def capture(channel, chromium, seconds, output):
    import websocket

    if not re.fullmatch(r'[A-Za-z0-9_]{1,25}', channel):
        raise ValueError('INVALID_TWITCH_CHANNEL')
    output.mkdir(mode=0o700)
    os.umask(0o077)
    with tempfile.TemporaryDirectory(prefix='ray-public-player-') as directory, ExitStack() as resources:
        root = Path(directory)
        sock = root / 'pulse.sock'
        env = {**os.environ, 'PULSE_SERVER': f'unix:{sock}', 'PULSE_SINK': 'ray_capture'}

        def start(*argv, stdout=subprocess.DEVNULL):
            child = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=stdout, env=env)
            resources.callback(stop, child)
            return child

        pulse = start('pulseaudio', '-n', '--daemonize=no', '--use-pid-file=no', '--exit-idle-time=-1',
                      f'--load=module-native-protocol-unix socket={sock} auth-anonymous=1',
                      '--load=module-null-sink sink_name=ray_capture rate=48000 channels=2')
        wait_file(sock, pulse)
        recorder = start('parec', '--device=ray_capture.monitor', '--rate=48000', '--channels=2',
                         '--format=s16le', '--latency-msec=20', '--raw', stdout=subprocess.PIPE)
        reader = threading.Thread(target=record, args=(recorder.stdout, output), daemon=True)
        reader.start()
        resources.callback(reader.join, 4)
        resources.callback(stop, recorder)
        browser = start(chromium, '--headless=new', '--disable-gpu', '--no-first-run',
                        '--remote-debugging-address=127.0.0.1', '--remote-debugging-port=0',
                        f'--user-data-dir={root / "profile"}', '--autoplay-policy=no-user-gesture-required',
                        f'https://www.twitch.tv/{channel}')
        port_file = root / 'profile/DevToolsActivePort'
        wait_file(port_file, browser)
        port = int(port_file.read_text().splitlines()[0])
        with urllib.request.urlopen(f'http://127.0.0.1:{port}/json/list', timeout=4) as response:
            tab = next(item for item in json.load(response) if item['type'] == 'page')
        connection = websocket.create_connection(tab['webSocketDebuggerUrl'], suppress_origin=True, timeout=3)
        resources.callback(connection.close)
        started = time.monotonic()
        previous, ready = None, False
        expression = """(() => {
          const video = document.querySelector('video');
          if (!video) return null;
          if (video.muted) video.muted = false;
          if (video.paused) video.play().catch(() => {});
          const ad = document.querySelector('[data-a-target="video-ad-countdown"]');
          return video.readyState >= 2 && !video.paused && !ad && video.volume > 0
            ? {position: video.currentTime, width: video.videoWidth, height: video.videoHeight} : null;
        })()"""
        sequence = 0
        while time.monotonic() - started < seconds:
            if recorder.poll() is not None or browser.poll() is not None:
                raise RuntimeError('CAPTURE_PROCESS_EXIT')
            sequence += 1
            connection.send(json.dumps({'id': sequence, 'method': 'Runtime.evaluate',
                                        'params': {'expression': expression, 'returnByValue': True}}))
            result = json.loads(connection.recv()).get('result', {}).get('result', {}).get('value')
            if result and not ready and previous is not None and result['position'] > previous:
                ready = True
                print(json.dumps({'state': 'playing', 'width': result['width'], 'height': result['height'],
                                  'observed_at': time.time()}), flush=True)
            previous = result['position'] if result else None
            time.sleep(.5)
        if not ready:
            raise RuntimeError('WATCH_PLAYBACK_UNCONFIRMED')


def match(signal, reference):
    """Return the strongest unique match after equalizing DC and normalizing energy."""
    import numpy as np

    if not len(signal) or not len(reference):
        raise ValueError('INSUFFICIENT_AUDIO')
    x = np.diff(signal, prepend=signal[0])
    y = np.diff(reference, prepend=reference[0])
    size = len(y)
    power = np.dot(y, y)
    if len(x) < size or power == 0:
        raise ValueError('INSUFFICIENT_AUDIO')
    length = 1 << (len(x) + size - 1).bit_length()
    raw = np.fft.irfft(np.fft.rfft(x, length) * np.fft.rfft(y[::-1], length), length)[size - 1:len(x)]
    energy = np.r_[0, np.cumsum(x * x, dtype=float)]
    scores = raw / np.sqrt(power * np.maximum(energy[size:] - energy[:-size], 1.0))
    index = int(np.argmax(scores))
    other = scores.copy()
    radius = MATCH_RATE // 10
    other[max(0, index - radius):index + radius + 1] = 0
    best, second = float(scores[index]), float(other.max())
    if best < .65 or best - second < .15:
        raise ValueError('PLAYER_AUDIO_MATCH_UNCONFIRMED')
    return index, best


def align(output, reference, event_time, window=40):
    import numpy as np

    with wave.open(str(reference), 'rb') as wav:
        if (wav.getframerate(), wav.getsampwidth(), wav.getnchannels()) != (RATE, 2, 1):
            raise ValueError('USE_A_48KHZ_MONO_REPLY_EXCERPT')
        pcm = np.frombuffer(wav.readframes(RATE), dtype='<i2')
    active = np.flatnonzero(np.abs(pcm.astype(np.int32)) >= 33)
    if len(active) == 0 or len(pcm) < RATE // 5:
        raise ValueError('USE_AN_AUDIBLE_REPLY_EXCERPT')
    factor = RATE // MATCH_RATE
    y = pcm[:len(pcm) // factor * factor].reshape(-1, factor).mean(axis=1)

    with (output / 'clock.jsonl').open() as log:
        clock = np.array([json.loads(line) for line in log], dtype=float)
    if (clock.ndim != 2 or clock.shape[1] != 2 or len(clock) < 2 or not np.isfinite(clock).all()
            or clock[0, 0] < 0 or (np.diff(clock[:, 0]) <= 0).any()
            or not clock[0, 1] < event_time < clock[-1, 1] or (np.diff(clock[:, 1]) < 0).any()):
        raise ValueError('EVENT_OUTSIDE_MONOTONIC_CAPTURE_WINDOW')
    offsets = np.interp([event_time, event_time + window], clock[:, 1], clock[:, 0])
    unit = factor * FRAME_BYTES
    start, end = (int(offset) // unit for offset in offsets)
    with (output / 'player.pcm').open('rb') as audio:
        audio.seek(start * unit)
        data = audio.read((end - start) * unit)
    data = data[:len(data) // unit * unit]
    if not data:
        raise ValueError('INSUFFICIENT_AUDIO')
    x = np.frombuffer(data, dtype='<i2').reshape(-1, factor * 2).mean(axis=1)
    index, score = match(x, y)
    first = (start + index) * unit + int(active[0]) * FRAME_BYTES
    heard = float(np.interp(first, clock[:, 0], clock[:, 1]))
    return {'event_to_player_audio_seconds': round(heard - event_time, 3),
            'player_audio_epoch': round(heard, 3), 'waveform_correlation': round(score, 3)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='action', required=True)
    watch = commands.add_parser('capture', help='Record an anonymous watch session with a private audio sink')
    watch.add_argument('channel')
    watch.add_argument('directory', type=Path)
    watch.add_argument('--chromium', default='chromium')
    watch.add_argument('--seconds', type=int, default=90, choices=range(10, 301), metavar='10..300')
    correlation = commands.add_parser('align', help='Compare captured player audio with a reply excerpt')
    correlation.add_argument('directory', type=Path)
    correlation.add_argument('reference', type=Path)
    correlation.add_argument('--event-time', required=True, help='ISO 8601 chat event timestamp with timezone')
    args = parser.parse_args()
    if args.action == 'capture':
        capture(args.channel, args.chromium, args.seconds, args.directory)
    else:
        event = datetime.fromisoformat(args.event_time)
        if event.tzinfo is None:
            parser.error('--event-time needs a timezone')
        print(json.dumps(align(args.directory, args.reference, event.timestamp()), indent=2))


if __name__ == '__main__':
    main()
