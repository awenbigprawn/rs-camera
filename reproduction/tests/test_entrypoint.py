import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ENTRYPOINT = Path(__file__).resolve().parents[2] / 'reproduce.sh'


class EntrypointTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / 'repo with spaces'
        (self.root / 'scripts').mkdir(parents=True)
        (self.root / 'reproduction').mkdir()
        shutil.copy2(ENTRYPOINT, self.root / 'reproduce.sh')
        self.log = self.root / 'calls.jsonl'
        self.venv = self.root / '.venv'
        self.python = self.root / 'fake-python'
        self.python.write_text(f'#!{sys.executable}\n' + '''import json, os, sys
from pathlib import Path
checking = len(sys.argv) > 1 and sys.argv[1].endswith('/check_dependencies.py')
with Path(os.environ['CALL_LOG']).open('a') as stream:
    stream.write(json.dumps(['check' if checking else 'pipeline', sys.argv[1:]]) + '\\n')
if checking:
    raise SystemExit(int(os.environ.get('CHECK_STATUS', '0')))
''')
        self.python.chmod(0o755)
        shutil.copy2(self.python, self.root / 'reproduction/pipeline.py')
        (self.root / 'scripts/install_python.sh').write_text('''#!/bin/sh
set -eu
printf '%s\\n' '["install"]' >> "$CALL_LOG"
[ "${INSTALL_STATUS:-0}" -eq 0 ] || exit "$INSTALL_STATUS"
mkdir -p "$VENV_DIR/bin"
cp "$FAKE_PYTHON" "$VENV_DIR/bin/python"
''')
        self.env = {**os.environ, 'CALL_LOG': str(self.log),
                    'FAKE_PYTHON': str(self.python), 'VENV_DIR': str(self.venv),
                    'CHECK_STATUS': '0', 'INSTALL_STATUS': '0'}

    def existing_venv(self):
        (self.venv / 'bin').mkdir(parents=True)
        shutil.copy2(self.python, self.venv / 'bin/python')

    def run_entrypoint(self, *arguments):
        result = subprocess.run(['sh', str(self.root / 'reproduce.sh'), *arguments],
                                cwd='/tmp', env=self.env, text=True, capture_output=True)
        calls = [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []
        return result, calls

    def test_missing_venv_is_created_at_custom_location_before_dispatch(self):
        self.venv = self.root / 'custom environment'
        self.env['VENV_DIR'] = str(self.venv)
        result, calls = self.run_entrypoint('--output', 'result path', 'raw', '--no-pdf')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([call[0] for call in calls], ['install', 'pipeline'])
        self.assertTrue((self.venv / 'bin/python').is_file())
        self.assertEqual(calls[-1][1][1:], ['--output', 'result path', 'raw', '--no-pdf'])

    def test_valid_venv_is_reused_without_installation(self):
        self.existing_venv()
        result, calls = self.run_entrypoint('full', '--smoke')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([call[0] for call in calls], ['check', 'pipeline'])
        self.assertEqual(calls[0][1][1:], ['--python', '--build-tools'])

    def test_mismatched_venv_is_repaired(self):
        self.existing_venv()
        self.env['CHECK_STATUS'] = '1'
        result, calls = self.run_entrypoint('raw')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([call[0] for call in calls], ['check', 'install', 'pipeline'])

    def test_install_failure_does_not_start_the_pipeline(self):
        self.env['INSTALL_STATUS'] = '17'
        result, calls = self.run_entrypoint('full')
        self.assertEqual(result.returncode, 17)
        self.assertEqual([call[0] for call in calls], ['install'])

    def test_controls_and_help_do_not_install(self):
        for arguments in [('plan',), ('status', '--output', 'raw'),
                          ('stop', '--config', 'full'), ('resume',),
                          ('raw', '--help'), ('--help', 'full')]:
            with self.subTest(arguments=arguments):
                self.log.unlink(missing_ok=True)
                result, calls = self.run_entrypoint(*arguments)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual([call[0] for call in calls], ['pipeline'])


if __name__ == '__main__':
    unittest.main()
