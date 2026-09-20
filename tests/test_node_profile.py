"""Node policy generation stays offline and captures code/config inputs."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from exitzero.policy import load_policy

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / 'bin/exitzero'


class NodeProfileTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def cli(self, *args, expected=0):
        result = subprocess.run([sys.executable, str(CLI), '--root', str(self.root), *args],
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return result

    def test_default_requires_local_package_and_does_not_create_python_sample(self):
        self.cli('init', '--profile', 'node', expected=2)
        self.assertFalse((self.root / 'exitzero.toml').exists())
        (self.root / 'package.json').write_text('{}')
        self.cli('init', '--profile', 'node')
        policy = load_policy(self.root / 'exitzero.toml')
        self.assertEqual(policy['checks'][0]['options']['argv'], ['npm', 'test'])
        self.assertFalse(policy['checks'][0]['reuse'])
        self.assertFalse((self.root / 'exitzero_sample.py').exists())
        self.assertIn('node_modules/', (self.root / '.gitignore').read_text())

    def test_generation_does_not_execute_any_command(self):
        command = '{python} -c "from pathlib import Path; Path(\'EXECUTED\').touch()"'
        self.cli('init', '--profile', 'node', '--test-command', command,
                 '--lint-command', command, '--typecheck-command', command, '--review-command', command)
        self.assertFalse((self.root / 'EXECUTED').exists())
        policy = load_policy(self.root / 'exitzero.toml')
        self.assertEqual([c['id'] for c in policy['checks']], ['test-command', 'lint-command', 'typecheck-command', 'review-1'])
        self.assertEqual({c['kind'] for c in policy['checks']}, {'command'})

    def test_receipt_includes_ts_js_lockfiles_configs_and_explicit_fixtures(self):
        names = ['src/main.ts', 'src/view.tsx', 'src/lib.mts', 'tests/app.test.js',
                 'test/smoke.cjs', 'eslint.config.mjs', 'package.json', 'package-lock.json',
                 'packages/child/package.json', 'pnpm-lock.yaml', 'pnpm-workspace.yaml',
                 'tsconfig.base.json', 'data/cases.json']
        for name in [*names, 'node_modules/ignored/index.js']:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('{}\n')
        self.cli('init', '--profile', 'node', '--source-root', 'src',
                 '--input-path', 'data/*.json', '--test-command', '{python} -c "raise SystemExit(0)"')
        self.cli('lint-config')
        result = self.cli('check', '--format', 'json')
        receipt = json.loads(result.stdout)
        self.assertTrue(set(names) <= set(receipt['inputs']))
        self.assertNotIn('node_modules/ignored/index.js', receipt['inputs'])
        self.assertEqual(json.loads((self.root / receipt['receipt']).read_text()), receipt)

    def test_pnpm_commands_and_options_roundtrip_without_shell(self):
        self.cli('init', '--profile', 'node', '--test-command', 'pnpm test -- --run',
                 '--lint-command', 'pnpm exec eslint "src/a b.ts"', '--typecheck-command', 'pnpm exec tsc --noEmit')
        checks = load_policy(self.root / 'exitzero.toml')['checks']
        self.assertEqual(checks[0]['options']['argv'], ['pnpm', 'test', '--', '--run'])
        self.assertEqual(checks[1]['options']['argv'], ['pnpm', 'exec', 'eslint', 'src/a b.ts'])
        self.assertEqual(checks[2]['options']['argv'], ['pnpm', 'exec', 'tsc', '--noEmit'])

    def test_invalid_flags_and_unsafe_paths_never_write_policy(self):
        cases = [('--allow-module', 'pytest'), ('--input-path', '../secret.json'),
                 ('--input-path', '.env'), ('--lint-command', 'npm run lint; echo injected'),
                 ('--source-root', 'src/*')]
        for options in cases:
            with self.subTest(options=options):
                self.cli('init', '--profile', 'node', '--test-command', 'node --test', *options, expected=2)
                self.assertFalse((self.root / 'exitzero.toml').exists())
        self.cli('init', '--profile', 'python', '--lint-command', 'npm run lint', expected=2)
        self.assertFalse((self.root / 'exitzero.toml').exists())

    def test_existing_policy_and_sync_remain_safe(self):
        self.cli('init', '--profile', 'node', '--test-command', 'node --test')
        path = self.root / 'exitzero.toml'
        before = path.read_bytes()
        self.cli('init', '--profile', 'node', '--test-command', 'node --test', expected=2)
        self.cli('init', '--sync', '--profile', 'node', expected=2)
        self.cli('init', '--sync')
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse((self.root / 'exitzero_sample.py').exists())

    def test_integrity_is_explicit_offline_and_uses_a_trusted_base(self):
        self.cli('init', '--profile', 'node', '--test-command', 'node --test',
                 '--test-integrity-base', 'origin/main')
        checks = load_policy(self.root / 'exitzero.toml')['checks']
        integrity = checks[-1]
        self.assertEqual(integrity['kind'], 'node.test-integrity')
        self.assertEqual(integrity['options'], {'base': 'origin/main'})
        self.assertFalse(integrity['reuse'])
        self.assertIn('**/*.test.ts', integrity['paths'])
        self.assertIn('**/test.js', integrity['paths'])
        # Init/lint require no Git checkout and never resolve or fetch a ref.
        self.cli('lint-config')

    def test_integrity_generation_rejects_invalid_flags_before_writing(self):
        for args in (('--profile', 'python', '--test-integrity-base', 'HEAD'),
                     ('--profile', 'node', '--test-integrity-base=--help'),
                     ('--profile', 'node', '--test-integrity-base', ''),
                     ('--sync', '--profile', 'node', '--test-integrity-base', 'HEAD')):
            with self.subTest(args=args):
                self.cli('init', '--test-command', 'node --test', *args, expected=2)
                self.assertFalse((self.root / 'exitzero.toml').exists())


if __name__ == '__main__':
    unittest.main()
