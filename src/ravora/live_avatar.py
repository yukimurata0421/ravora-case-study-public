"""Run the same animated canvas as the conversation console, independent of operator tabs."""
import base64
from contextlib import ExitStack
import io
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from urllib.request import urlopen

from PIL import Image
import numpy as np
import websocket

from ravora.live_media import FPS, RATE, SILENCE, Stage
from ravora.settings import SettingsError


class ProgramAvatar:
    def __init__(self, port, run_dir):
        self.resources = ExitStack()
        self.clip = None
        self.serial = 0
        self.condition = threading.Condition()
        self.closed = False
        self.pending = self.finished = self.worker = self.failure = None
        self.rendered = 0
        try:
            binary = next((str(p) for p in (Path('/opt/google/chrome/chrome'),
                          Path('/snap/chromium/current/usr/lib/chromium-browser/chrome')) if p.is_file()),
                          shutil.which('chromium') or shutil.which('google-chrome'))
            if not binary:
                raise SettingsError('PROGRAM_AVATAR_CHROMIUM_REQUIRED')
            profile = self.resources.enter_context(tempfile.TemporaryDirectory(dir=run_dir))
            url = f'http://127.0.0.1:{port}/program-avatar'
            args = [binary, '--headless', '--no-first-run', '--disable-extensions', '--disable-sync',
                    '--disable-background-networking', '--disable-component-update', '--no-default-browser-check',
                    '--disable-dev-shm-usage', '--num-raster-threads=1', '--renderer-process-limit=2',
                    '--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1',
                    '--remote-debugging-port=0', '--user-data-dir='+profile, url]
            env = {k:os.environ[k] for k in ('HOME','PATH','LANG','DISPLAY','XDG_RUNTIME_DIR') if k in os.environ}
            if Path('/dev/nvidia0').exists():
                args.extend(['--enable-gpu', '--use-gl=angle', '--use-angle=gl-egl', '--ignore-gpu-blocklist'])
            proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, env=env, start_new_session=True)
            self.resources.callback(self._stop, proc)
            marker = Path(profile)/'DevToolsActivePort'
            deadline = time.monotonic()+20
            while time.monotonic() < deadline and proc.poll() is None:
                if marker.exists():
                    debug = marker.read_text().splitlines()
                    if debug:
                        with urlopen('http://127.0.0.1:'+debug[0]+'/json/list', timeout=2) as response:
                            tabs = json.load(response)
                        tab = next((t for t in tabs if t['type']=='page' and t['url']==url), None)
                        if tab:
                            break
                time.sleep(.05)
            else:
                raise SettingsError('PROGRAM_AVATAR_BROWSER_START_FAILED')
            self.socket = websocket.create_connection(tab['webSocketDebuggerUrl'], suppress_origin=True, skip_utf8_validation=True, timeout=20)
            self.resources.callback(self.socket.close)
            while time.monotonic() < deadline:
                self.renderer = self.evaluate('window.programAvatar?.then(a=>a.renderer)')
                if self.renderer in ('rig','sprites'):
                    break
                time.sleep(.05)
            else:
                raise SettingsError('PROGRAM_AVATAR_NOT_READY')
            self.finished = self.draw(0, Stage(), None, None)
            self.worker = threading.Thread(target=self._run, name='ray-avatar', daemon=True)
            self.worker.start()
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _stop(proc):
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=4)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()

    def evaluate(self, expression):
        self.serial += 1
        self.socket.send(json.dumps({'id':self.serial, 'method':'Runtime.evaluate',
            'params':{'expression':expression, 'awaitPromise':True, 'returnByValue':True}}))
        while True:
            reply = json.loads(self.socket.recv())
            if reply.get('id') == self.serial:
                result = reply.get('result', {})
                if 'error' in reply or 'exceptionDetails' in result:
                    raise SettingsError('PROGRAM_AVATAR_DRAW_FAILED')
                return result['result'].get('value')

    def render(self, frame, stage, clip):
        clip = clip if stage.speaker == 'Ray' else None
        elapsed = max(0, clip.position-len(SILENCE)) / (2*RATE) if clip else None
        with self.condition:
            if self.failure:
                raise self.failure
            # Never build a backlog of poses or let GPU readback delay the AAC clock.
            self.pending = frame, stage, clip, elapsed
            self.condition.notify()
            result, self.finished = self.finished, None
            return result

    def _run(self):
        try:
            while True:
                with self.condition:
                    self.condition.wait_for(lambda: self.closed or self.pending is not None)
                    if self.closed:
                        return
                    request, self.pending = self.pending, None
                image = self.draw(*request)
                with self.condition:
                    self.finished = image
                    self.rendered += 1
        except Exception:
            with self.condition:
                self.failure = SettingsError('PROGRAM_AVATAR_DRAW_FAILED')

    def draw(self, frame, stage, clip, elapsed):
        pcm = None
        if clip is not None and clip is not self.clip:
            samples = np.frombuffer(clip.pcm, dtype='<i2')[::2]
            pcm = base64.b64encode(samples.tobytes()).decode('ascii')
        args = json.dumps([frame/FPS, elapsed, stage.expression, stage.motion, pcm])
        data = self.evaluate('window.programAvatar.then(a=>a.draw(...'+args+'))')
        self.clip = clip
        with Image.open(io.BytesIO(base64.b64decode(data,validate=True))) as image:
            return image.convert('RGB')

    def close(self):
        with self.condition:
            self.closed = True
            self.condition.notify_all()
        if self.worker:
            self.worker.join()
        self.resources.close()
