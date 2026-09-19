"""Copilot protocol and real local Git pre-push enforcement."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

CLI = Path(__file__).resolve().parents[1] / 'bin/exitzero'


class AdditionalHookTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.root = self.base / 'repo'
        self.root.mkdir()
        self.env = {**os.environ, 'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1'}
        self.cli('init')

    def cli(self, *args, expected=0, payload=None):
        p = subprocess.run([sys.executable, str(CLI), '--root', str(self.root), *args],
                           input=payload, capture_output=True, text=True, env=self.env, timeout=20)
        self.assertEqual(p.returncode, expected, p.stdout + p.stderr)
        return p

    def git(self, *args, expected=0):
        p = subprocess.run(['git', '-C', str(self.root), '-c', 'user.name=Gate Test',
                            '-c', 'user.email=gate@example.invalid', '-c', 'commit.gpgSign=false', *args],
                           capture_output=True, text=True, env=self.env, timeout=20)
        self.assertEqual(p.returncode, expected, p.stdout + p.stderr)
        return p.stdout.strip()

    def latest(self):
        path = max((self.root / '.exitzero/runs').glob('*.json'), key=lambda p: p.stat().st_mtime_ns)
        return json.loads(path.read_text())

    def test_copilot_install_preserves_foreign_entries_and_is_idempotent(self):
        self.cli('hooks', 'install', '--adapter', 'copilot')
        path = self.root / '.github/hooks/exitzero.json'
        data = json.loads(path.read_text())
        data['hooks']['agentStop'].append({'type': 'command', 'exec': 'foreign', 'args': []})
        path.write_text(json.dumps(data))
        self.cli('hooks', 'install', '--adapter', 'copilot')
        before = path.read_bytes()
        self.cli('hooks', 'install', '--adapter', 'copilot')
        self.assertEqual(path.read_bytes(), before)
        result = self.cli('doctor', '--adapter', 'copilot', '--format', 'json')
        self.assertEqual(next(d for d in json.loads(result.stdout)['diagnostics'] if d['target'] == 'copilot')['state'], 'configured')
        self.assertEqual(len(json.loads(path.read_text())['hooks']['agentStop']), 2)

    def test_copilot_json_blocks_violations_and_operational_errors(self):
        (self.root / 'broken.py').write_text('def broken(:\n')
        for event in ('stop', 'agentStop', 'preToolUse'):
            result = self.cli('hooks', 'run', '--adapter', 'copilot', '--event', event, payload='{}')
            response = json.loads(result.stdout)
            self.assertEqual(response.get('permissionDecision') if event == 'preToolUse' else response.get('decision'),
                             'deny' if event == 'preToolUse' else 'block')
            self.assertEqual(self.latest()['exit_code'], 1)
        invalid = self.cli('hooks', 'run', '--adapter', 'copilot', payload='invalid')
        self.assertEqual(json.loads(invalid.stdout)['decision'], 'block')
        self.assertEqual(self.latest()['exit_code'], 2)
        (self.root / 'broken.py').write_text('value = 1\n')
        passed = self.cli('hooks', 'run', '--adapter', 'copilot', payload='{}')
        self.assertEqual(json.loads(passed.stdout), {})

    def test_actual_pre_push_rejects_bad_commit_then_accepts_repair(self):
        self.git('init', '-q')
        self.git('add', '.')
        self.git('commit', '-qm', 'initial')
        target = self.base / 'remote.git'
        self.git('init', '--bare', str(target))
        self.cli('hooks', 'install', '--adapter', 'pre-push')
        self.git('push', str(target), 'HEAD:refs/heads/main')
        initial = self.git('rev-parse', 'HEAD')
        self.assertEqual(self.latest()['hook_slot'], 'pre-push')
        self.assertEqual(self.latest()['exit_code'], 0)
        bad = self.root / 'bad.py'
        bad.write_text('def bad(:\n')
        self.git('add', 'bad.py')
        self.git('commit', '-qm', 'bad syntax')
        self.git('push', str(target), 'HEAD:refs/heads/main', expected=1)
        self.assertEqual(self.latest()['exit_code'], 1)
        remote = subprocess.check_output(['git', '--git-dir', str(target), 'rev-parse', 'main'], env=self.env, text=True).strip()
        self.assertEqual(remote, initial)
        bad.write_text('value = 1\n')
        self.git('add', 'bad.py')
        self.git('commit', '-qm', 'repair')
        self.git('push', str(target), 'HEAD:refs/heads/main')
        self.assertEqual(self.latest()['exit_code'], 0)
        payload = f'refs/heads/old {initial} refs/heads/old {"0" * 40}\n'
        self.cli('hooks', 'run', '--slot', 'pre-push', payload=payload, expected=2)
        self.assertEqual(self.latest()['exit_code'], 2)
        bad.write_text('value = 2\n')
        self.git('add', 'bad.py')
        self.cli('hooks', 'run', '--slot', 'pre-push', payload='', expected=1)
        self.assertIn('core.index-mismatch', {f['rule'] for f in self.latest()['findings']})


if __name__ == '__main__':
    unittest.main()
