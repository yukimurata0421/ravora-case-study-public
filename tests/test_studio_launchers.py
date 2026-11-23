import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest


SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'


class StudioLauncherTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.runtime = self.root / 'runtime'
        self.runtime.mkdir()
        self.programs = self.root / 'bin'
        self.programs.mkdir()
        self.env = {**os.environ, 'PATH': str(self.programs) + ':' + os.environ['PATH'],
                    'XDG_RUNTIME_DIR': str(self.runtime), 'MOCK_ROOT': str(self.root)}
        for name in ('RAY_STUDIO_HOST_MIC', 'YOUTUBE_READ_ENABLED'):
            self.env.pop(name, None)

    def executable(self, path, script):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('#!/bin/sh\nset -eu\n' + script)
        path.chmod(0o700)

    def invoke(self, script, destination):
        return subprocess.run(['sh', str(SCRIPTS / script), destination], cwd=self.root,
                              env=self.env, capture_output=True, text=True, timeout=5)

    def test_service_routes_and_passes_private_link_location(self):
        self.executable(self.root / '.venv/bin/ravora-youtube-live',
                        "printf '%s\\n' \"$RAVORA_API_ENABLED\" \"${YOUTUBE_READ_ENABLED:-unset}\" \"$@\"\n")
        (self.root / 'studio.toml').touch()
        for kind, flag, port in (('youtube', '--auto-broadcast', '8790'), ('twitch', '--publish-twitch', '8793')):
            with self.subTest(kind=kind):
                self.env['RAY_STUDIO_HOST_MIC'] = 'dell'
                result = self.invoke('ray-studio-service.sh', kind)
                self.assertEqual(result.returncode, 0, result.stderr)
                values = result.stdout.splitlines()
                self.assertEqual(values[:2], ['true', 'true' if kind == 'youtube' else 'unset'])
                self.assertEqual(values[2:9], ['--mode', 'cloud', flag, '--until-stopped', '--port', port,
                                               '--operator-url-link'])
                self.assertEqual(Path(values[9]), self.runtime / f'ray-studio-{kind}/operator.url')
                self.assertIn('--dell-mic', values)
                self.assertIn('studio.toml', values)
        self.assertNotEqual(self.invoke('ray-studio-service.sh', 'invalid').returncode, 0)

    def test_console_starts_once_and_reuses_the_matching_active_service(self):
        url = 'http://127.0.0.1:8793/#' + 'a' * 43
        reference = self.root / 'url'
        reference.write_text(url + '\n')
        folder = self.runtime / 'ray-studio-twitch'
        folder.mkdir()
        (folder / 'operator.url').symlink_to(reference)
        self.executable(self.programs / 'systemctl', '''
test "$1" = --user
test "$3" = ray-studio@twitch.service
if [ "$2" = show ]; then
    if [ -f "$MOCK_ROOT/started" ]; then printf 'active\\n'; else printf 'inactive\\n'; fi
else
    printf 'start\\n' >> "$MOCK_ROOT/started"
fi
''')
        for _ in range(2):
            result = self.invoke('ray-studio-console.sh', 'twitch')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), url)
        self.assertEqual((self.root / 'started').read_text(), 'start\n')
        reference.write_text(url.replace('8793', '8790') + '\n')
        invalid = self.invoke('ray-studio-console.sh', 'twitch')
        self.assertNotEqual(invalid.returncode, 0)
        self.assertEqual(invalid.stdout, '')
