"""Read-only observation of RAY's public Twitch player from a separate observer."""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from urllib.parse import urljoin, urlsplit
from urllib.request import urlopen

CHANNEL = os.environ.get('TWITCH_CHANNEL', 'ray_ravora').lower()
DATABASE = '/data/observations.sqlite3'
HEARTBEAT = Path('/tmp/ray-observer-heartbeat')
FIELDS = ('observed_at', 'provider', 'stream_id', 'media', 'reason', 'segment_hash',
          'video_frames', 'audio_frames', 'media_bytes')


def provider(ydl):
    # Same public StreamMetadata operation as the pinned Twitch player extractor.
    extractor = ydl.get_info_extractor('TwitchStream')
    rows = extractor._download_gql(CHANNEL, [{
        'operationName': 'StreamMetadata',
        'variables': {'channelLogin': CHANNEL, 'includeIsDJ': True},
    }], 'Reading public stream state')
    if rows[0].get('errors'):
        raise ValueError('metadata_error')
    user = rows[0]['data']['user']
    stream = user['stream']
    if stream is None:
        return None
    if stream.get('type') != 'live' or not str(stream.get('id', '')).isdigit():
        raise ValueError('invalid_stream')
    return str(stream['id'])


def fetch(address, limit):
    if urlsplit(address).scheme != 'https':
        raise ValueError('invalid_media_url')
    with urlopen(address, timeout=4) as response:
        data = response.read(limit + 1)
        if urlsplit(response.geturl()).scheme != 'https' or len(data) > limit:
            raise ValueError('invalid_media_response')
        return data, response.geturl()


def last_segment(playlist):
    pending = False
    segment = None
    ad = False
    for line in playlist.splitlines():
        if line.startswith('#EXTINF:'):
            pending = True
            ad = 'stitched-ad' in line.lower()
        elif pending and line and not line.startswith('#'):
            segment = None if ad else line
            pending = False
    if segment is None:
        raise ValueError('no_program_segment')
    return segment


def frame_progress(frames, kind):
    pts = [float(frame['best_effort_timestamp_time']) for frame in frames
           if frame.get('media_type') == kind and 'best_effort_timestamp_time' in frame]
    return len(pts), len(pts) > 1 and all(a < b for a, b in zip(pts, pts[1:]))


def media(ydl, stream_id, previous_hash):
    info = ydl.extract_info('https://www.twitch.tv/' + CHANNEL, download=False)
    if (info.get('uploader_id') != CHANNEL or info.get('id') != stream_id
            or info.get('is_live') is not True):
        return {'media': 'unknown', 'reason': 'stream_changed'}
    formats = [f for f in info['formats'] if f.get('height') and f.get('vcodec') != 'none'
               and f.get('acodec') != 'none' and f.get('protocol', '').startswith('m3u8')]
    if not formats:
        return {'media': 'unknown', 'reason': 'no_av_rendition'}
    manifest, base = fetch(min(formats, key=lambda f: f['height'])['url'], 192 * 1024)
    segment, _ = fetch(urljoin(base, last_segment(manifest.decode())), 4 * 1024 * 1024)
    digest = hashlib.sha256(segment).hexdigest()
    proc = subprocess.run(['ffprobe', '-v', 'error', '-threads', '1', '-protocol_whitelist', 'pipe',
                           '-show_frames', '-show_entries', 'frame=media_type,best_effort_timestamp_time',
                           '-of', 'json', '-i', 'pipe:0'], input=segment, capture_output=True, timeout=7)
    if proc.returncode:
        return {'media': 'unknown', 'reason': 'decode_failed'}
    frames = json.loads(proc.stdout)['frames']
    video, video_ok = frame_progress(frames, 'video')
    audio, audio_ok = frame_progress(frames, 'audio')
    reason = ('same_segment' if digest == previous_hash else 'video_time' if not video_ok
              else 'audio_time' if not audio_ok else None)
    return dict(media='stalled' if reason else 'advancing', reason=reason, segment_hash=digest,
                media_bytes=len(segment), video_frames=video, audio_frames=audio)


class Quiet:
    def debug(self, *_): pass
    def warning(self, *_): pass
    def error(self, *_): pass


def sample(previous_id, previous_hash, due):
    from yt_dlp import YoutubeDL
    report = dict(provider='unknown', media='unknown', reason='provider_unavailable')
    with YoutubeDL(dict(quiet=True, logger=Quiet(), cachedir=False, socket_timeout=4, retries=0,
                        extractor_retries=0, noplaylist=True)) as ydl:
        try:
            stream = provider(ydl)
        except Exception:
            return report
        if stream is None:
            return dict(provider='offline', media='offline')
        report.update(provider='live', stream_id=stream, media='not_due', reason=None)
        if due or stream != previous_id:
            # Preserve the independent provider result if the media read hits the outer deadline.
            print(json.dumps(dict(report, media='unknown', reason='media_timeout')), flush=True)
            try:
                report.update(media(ydl, stream, previous_hash if previous_id == stream else None))
            except Exception:
                report.update(media='unknown', reason='media_unavailable')
    return report


def bounded_sample(previous_id, previous_hash, due):
    cmd = [sys.executable, __file__, '--sample', previous_id or '', previous_hash or '', str(int(due))]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=24)
        output = result.stdout
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout or b''
    if output.splitlines():
        with contextlib.suppress(ValueError, IndexError):
            return json.loads(output.splitlines()[-1])
    return dict(provider='unknown', media='unknown', reason='observer_unavailable')


def run():
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    with contextlib.closing(sqlite3.connect(DATABASE, timeout=5)) as db:
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('''CREATE TABLE IF NOT EXISTS observations (
            id INTEGER PRIMARY KEY, observed_at REAL NOT NULL, provider TEXT NOT NULL,
            stream_id TEXT, media TEXT NOT NULL, reason TEXT, segment_hash TEXT,
            video_frames INTEGER, audio_frames INTEGER, media_bytes INTEGER)''')
        previous = db.execute('SELECT stream_id,segment_hash FROM observations WHERE segment_hash IS NOT NULL ORDER BY id DESC LIMIT 1').fetchone()
        previous_id, previous_hash = previous or (None, None)
        tick = 0
        while not stop.is_set():
            start = time.monotonic()
            observation = bounded_sample(previous_id, previous_hash, tick % 2 == 0)
            observation['observed_at'] = time.time()
            with db:
                db.execute('INSERT INTO observations (' + ','.join(FIELDS) + ') VALUES (?,?,?,?,?,?,?,?,?)',
                           tuple(observation.get(field) for field in FIELDS))
            if observation['provider'] == 'live':
                if observation['stream_id'] != previous_id:
                    previous_hash = None
                previous_id = observation['stream_id']
                previous_hash = observation.get('segment_hash', previous_hash)
            elif observation['provider'] == 'offline':
                previous_id, previous_hash = None, None
            print(json.dumps({k: v for k, v in observation.items() if k != 'segment_hash'}), flush=True)
            HEARTBEAT.touch()
            tick += 1
            stop.wait(max(0, 30 - (time.monotonic() - start)))


if __name__ == '__main__':
    if sys.argv[1:2] == ['--sample']:
        print(json.dumps(sample(sys.argv[2], sys.argv[3], sys.argv[4] == '1')))
    elif sys.argv[1:2] == ['--healthy']:
        sys.exit(0 if HEARTBEAT.exists() and time.time() - HEARTBEAT.stat().st_mtime < 90 else 1)
    else:
        os.umask(0o077)
        run()
