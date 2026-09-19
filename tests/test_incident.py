"""A generated incident kit must stay red until real tests and code are repaired."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from exitzero.policy import load_policy

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "bin/exitzero"


class IncidentKitTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.cli("init")

    def cli(self, *args, expected=0):
        result = subprocess.run([sys.executable, str(CLI), "--root", str(self.root), *args],
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return result

    def kit(self, *args, expected=0):
        return self.cli("plugin", "incident-kit", "--id", "wrong-sum", "--description",
                        'description is data: "quoted"\nnot code', *args, expected=expected)

    def gate(self, expected):
        result = self.cli("check", "--format", "json", expected=expected)
        receipt = json.loads(result.stdout)
        self.assertEqual(json.loads((self.root / receipt['receipt']).read_text()), receipt)
        return receipt

    def test_placeholder_then_real_regression_then_repair(self):
        agents = self.root / "AGENTS.md"
        agents.write_text("Manual team notes\n" + agents.read_text())
        self.kit()
        self.assertTrue(agents.read_text().startswith("Manual team notes\n"))
        self.cli("lint-config")
        unfinished = self.gate(1)
        self.assertEqual(unfinished['requirements'][0]['status'], 'failed')
        self.assertNotIn('description is data', json.dumps(unfinished))
        source = self.root / "exitzero_sample.py"
        source.write_text('def add(left, right):\n    return left - right\n')
        test = self.root / "tests/incidents/wrong-sum/test_incident.py"
        test.write_text('from unittest import TestCase\nfrom exitzero_sample import add\n'
                        'class Regression(TestCase):\n'
                        '    def test_sum(self):\n        self.assertEqual(add(2, 2), 4)\n'
                        '    def test_zero(self):\n        self.assertEqual(add(0, 0), 0)\n')
        broken = self.gate(1)
        self.assertIn('incident.wrong-sum.regression', {f['rule'] for f in broken['findings']})
        source.write_text('def add(left, right):\n    return left + right\n')
        repaired = self.gate(0)
        self.assertEqual(repaired['requirements'][0]['status'], 'checks_passed')
        self.assertNotEqual(broken['inputs']['exitzero_sample.py'], repaired['inputs']['exitzero_sample.py'])
        test.unlink()
        missing = self.gate(2)
        self.assertIn('core.error', {f['rule'] for f in missing['findings']})
        self.assertEqual(missing['requirements'][0]['status'], 'unverified')

    def test_existing_kit_and_duplicate_policy_ids_do_not_mutate(self):
        self.kit()
        before = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        self.kit(expected=2)
        self.assertEqual(before, {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob('*') if p.is_file()})

    def test_collision_without_directory_is_validated_before_writes(self):
        policy = self.root / 'exitzero.toml'
        policy.write_text(policy.read_text().replace('id = "syntax"', 'id = "incident.wrong-sum.quality"'))
        self.cli('init', '--sync')
        before = policy.read_bytes()
        self.kit(expected=2)
        self.assertEqual(policy.read_bytes(), before)
        self.assertFalse((self.root / 'tests').exists())

    def test_bad_markers_and_unsafe_paths_do_not_write(self):
        agents = self.root / 'AGENTS.md'
        agents.write_text('<!-- exitzero:begin -->\n')
        policy = self.root / 'exitzero.toml'
        before = policy.read_bytes()
        self.kit(expected=2)
        self.assertEqual(policy.read_bytes(), before)
        self.assertFalse((self.root / 'tests').exists())
        self.kit('--path', '../outside.py', expected=2)
        self.kit('--path', '.env', expected=2)
        self.assertFalse((self.root / 'tests').exists())

    def test_symlinked_output_parent_is_rejected(self):
        (self.root / 'other').mkdir()
        (self.root / 'tests').symlink_to(self.root / 'other', target_is_directory=True)
        self.kit(expected=2)
        self.assertEqual(list((self.root / 'other').iterdir()), [])

    def test_input_fixtures_and_description_roundtrip(self):
        (self.root / 'case.json').write_text('{"value": 1}')
        self.kit('--path', '*.py', '--path', 'case.json')
        policy = load_policy(self.root / 'exitzero.toml')
        command = next(c for c in policy['checks'] if c['kind'] == 'command')
        self.assertIn('case.json', command['paths'])
        self.assertFalse(command['reuse'])
        manifest = json.loads((self.root / 'tests/incidents/wrong-sum/incident.json').read_text())
        self.assertEqual(manifest['description'], 'description is data: "quoted"\nnot code')
        self.assertIn('case.json', self.gate(1)['inputs'])


if __name__ == '__main__':
    unittest.main()
