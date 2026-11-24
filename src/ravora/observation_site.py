"""Validate a studio export and render the public observation files."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import selectors
import subprocess
import sys
import tempfile
import time

from ravora.ray_page import TEMPLATE, render
from ravora.ray_public_contract import validate

MAX_BYTES = 256 * 1024
ASSETS = ('ray-20261124.css', 'ray-20261124.js')


def prepare(destination, body):
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=destination.parent, prefix='.ray-', delete=False) as out:
        path = Path(out.name)
        try:
            out.write(body)
            out.flush()
            os.fchmod(out.fileno(), 0o644)
            os.fsync(out.fileno())
        except BaseException:
            path.unlink(missing_ok=True)
            raise
    return path


def write(root, value):
    """Keep the last successful page and JSON on a failed or superseded collection."""
    body = (json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':')) + '\n').encode()
    html = render(value).encode()
    root.mkdir(parents=True, exist_ok=True)
    page = root / 'ray/index.html'
    data = root / 'ray-observation.json'
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    pending = []
    try:
        fcntl.flock(directory, fcntl.LOCK_EX)
        previous = data.read_bytes() if data.is_file() else None
        if previous:
            try:
                old_stamp = json.loads(previous)['generated_at']
            except (KeyError, ValueError, TypeError):
                old_stamp = None
            if type(old_stamp) in (float, int) and value['generated_at'] < old_stamp <= time.time() + 10:
                return False
        for name in ASSETS:
            dest = root / 'assets' / name
            asset = (TEMPLATE.parent / name).read_bytes()
            if not dest.is_file() or dest.read_bytes() != asset:
                staged = prepare(dest, asset)
                pending.append(staged)
                os.replace(staged, dest)
        snapshot = prepare(data, body)
        pending.append(snapshot)
        webpage = prepare(page, html)
        pending.append(webpage)
        os.replace(snapshot, data)
        try:
            os.replace(webpage, page)
        except OSError:
            if previous is None:
                data.unlink()
            else:
                rollback = prepare(data, previous)
                pending.append(rollback)
                os.replace(rollback, data)
            raise
    finally:
        for path in pending:
            path.unlink(missing_ok=True)
        os.close(directory)
    return True


def source_output(command):
    """Read at most one bounded export and require the exporter to exit successfully."""
    deadline = time.monotonic() + 30
    body = bytearray()
    with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                          stdin=subprocess.DEVNULL) as process, selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        try:
            while True:
                if not selector.select(max(0, deadline - time.monotonic())):
                    raise subprocess.TimeoutExpired(command, 30)
                chunk = os.read(process.stdout.fileno(), min(65536, MAX_BYTES + 1 - len(body)))
                if not chunk:
                    break
                body.extend(chunk)
                if len(body) > MAX_BYTES:
                    raise ValueError('SOURCE_TOO_LARGE')
            code = process.wait(timeout=max(0, deadline - time.monotonic()))
            if code:
                raise subprocess.CalledProcessError(code, command)
        finally:
            if process.poll() is None:
                process.kill()
    return body


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True, help='Directory served by the static site')
    parser.add_argument('--source-command', nargs=argparse.REMAINDER, help='Exporter command and arguments (default: read stdin)')
    args = parser.parse_args(argv)
    try:
        if args.source_command is None:
            raw = sys.stdin.buffer.read(MAX_BYTES + 1)
        elif args.source_command:
            raw = source_output(args.source_command)
        else:
            parser.error('--source-command needs an executable')
        if len(raw) > MAX_BYTES:
            raise ValueError('SOURCE_TOO_LARGE')
        collected = time.time()
        value = validate(json.loads(raw), collected)
        value['pipeline']['collected_at'] = collected
        written = write(args.output, value)
    except ModuleNotFoundError as exc:
        if exc.name != 'jsonschema':
            raise
        parser.exit(2, 'Install the observations extra to validate public exports.\n')
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, KeyError) as exc:
        # Upstream output can contain private data. Emit only a bounded error category.
        print(json.dumps({'status': 'failed', 'reason': type(exc).__name__}), file=sys.stderr)
        return 1
    print(json.dumps({'status': 'ok' if written else 'superseded', 'collected_at': collected,
                      'broadcasts': len(value['broadcasts'])}))
    return 0


if __name__ == '__main__':
    sys.exit(main())
