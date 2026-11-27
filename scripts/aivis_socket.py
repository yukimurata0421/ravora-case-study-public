"""Expose the network-isolated engine through an owner-only Unix socket after warm-up."""
import asyncio
import contextlib
import http.client
import json
import os
from pathlib import Path
import signal
import urllib.parse


SOCKET = Path('/socket/kanon.sock')
STYLE = 1325133120
SPEAKER = 'b1ca560f-f212-4e67-ab7d-0a4f5afb75a8'


def request(method, path, body=None):
    conn = http.client.HTTPConnection('127.0.0.1', 10101, timeout=45)
    try:
        conn.request(method, path, body, headers={'Content-Type': 'application/json'})
        response = conn.getresponse()
        data = response.read(2 * 1024 * 1024)
        if response.status != 200:
            raise RuntimeError('Aivis readiness HTTP ' + str(response.status))
        return data
    finally:
        conn.close()


def warm():
    speakers = json.loads(request('GET', '/speakers'))
    if not any(s['speaker_uuid'] == SPEAKER and s['name'] == '花音'
               and any(v['name'] == 'ノーマル' and v['id'] == STYLE for v in s['styles']) for s in speakers):
        raise RuntimeError('Unexpected Aivis speaker or style')
    params = urllib.parse.urlencode({'speaker': STYLE, 'text': 'こんにちは。'})
    query = json.loads(request('POST', '/audio_query?' + params, b''))
    query['outputSamplingRate'] = 24000
    wav = request('POST', '/synthesis?speaker=' + str(STYLE), json.dumps(query).encode())
    if wav[:4] != b'RIFF' or len(wav) < 1000:
        raise RuntimeError('Aivis voice warm-up failed')


async def relay(client_reader, client_writer):
    # Each Ray HTTP connection carries one request. Forward the response as it arrives.
    try:
        reader, writer = await asyncio.open_connection('127.0.0.1', 10101)
        async def pump(source, target):
            while data := await source.read(65536):
                target.write(data)
                await target.drain()
            if target.can_write_eof():
                target.write_eof()
        try:
            await asyncio.wait_for(asyncio.gather(pump(client_reader, writer), pump(reader, client_writer)), 65)
        finally:
            writer.close()
            await writer.wait_closed()
    except (OSError, TimeoutError, ConnectionError):
        pass
    finally:
        client_writer.close()
        with contextlib.suppress(OSError):
            await client_writer.wait_closed()


async def main():
    os.umask(0o077)
    # Aivis 1.2.0 removes its old logs on boot and tolerates PermissionError.
    # The unprivileged process can open these sinks but cannot replace their names.
    log_dir = Path.home() / '.local/share/AivisSpeech-Engine/Logs'
    for name in ('AivisSpeech-Engine.log', 'AivisSpeech-Engine-Access.log'):
        (log_dir / name).symlink_to('/dev/null')
    log_dir.chmod(0o555)
    engine = await asyncio.create_subprocess_exec('/engine/run', '--host', '127.0.0.1', '--port', '10101',
        '--use_gpu', '--disable_sentry', '--disable_mutable_api',
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stopping.set)
    try:
        for _ in range(100):
            if engine.returncode is not None or stopping.is_set():
                raise RuntimeError('Engine stopped before readiness')
            try:
                if json.loads(await asyncio.to_thread(request, 'GET', '/version')) == '1.2.0':
                    break
            except OSError:
                pass
            await asyncio.sleep(.5)
        else:
            raise RuntimeError('Engine readiness timeout')
        await asyncio.to_thread(warm)
        providers = [p for p in Path('/proc').glob('[0-9]*/maps')
                     if (p.parent/'comm').read_text().strip() == 'run']
        if not any('libonnxruntime_providers_cuda' in p.read_text() for p in providers):
            raise RuntimeError('CUDA provider unavailable')
        SOCKET.unlink(missing_ok=True)
        server = await asyncio.start_unix_server(relay, path=SOCKET)
        async with server:
            print('Kanon normal ready (CUDA, style 1325133120)', flush=True)
            await asyncio.wait([asyncio.create_task(engine.wait()), asyncio.create_task(stopping.wait())],
                               return_when=asyncio.FIRST_COMPLETED)
            if not stopping.is_set():
                raise RuntimeError('Aivis engine stopped')
    finally:
        SOCKET.unlink(missing_ok=True)
        if engine.returncode is None:
            engine.terminate()
            try:
                await asyncio.wait_for(engine.wait(), 5)
            except asyncio.TimeoutError:
                engine.kill()
                await engine.wait()


if __name__ == '__main__':
    asyncio.run(main())
