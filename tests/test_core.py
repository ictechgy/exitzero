"""Black-box contracts for the CLI, policy, receipts and extension boundary."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
CLI = REPO / "bin" / "aidd-gate"


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def cli(self, *args):
        return subprocess.run([sys.executable, str(CLI), "--root", str(self.root), *args],
                              capture_output=True, text=True, timeout=30)

    def init(self):
        result = self.cli("init")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_init_check_lint_and_each_run_receipt(self):
        self.init()
        for command in ("check", "lint-config"):
            result = self.cli(command, "--format", "json")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["status"], "passed")
            receipt = json.loads((self.root / payload["receipt"]).read_text())
            self.assertEqual(receipt["exit_code"], 0)
            self.assertEqual(receipt["schema_version"], 1)
            self.assertEqual(receipt["command"], command)
            self.assertEqual(len(receipt["policy_sha256"]), 64)
        self.assertEqual(len(list((self.root / ".aidd-gate/runs").glob("*.json"))), 2)

    def test_policy_error_still_has_receipt(self):
        (self.root / "aidd-gate.toml").write_text("version = [broken")
        result = self.cli("check", "--format", "json")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue((self.root / payload["receipt"]).is_file())
        self.assertEqual(payload["status"], "error")

    def test_drift_fails_check_and_lint_then_sync_preserves_notes(self):
        self.init()
        agents = self.root / "AGENTS.md"
        agents.write_text("# My notes\n\n" + agents.read_text())
        policy = self.root / "aidd-gate.toml"
        policy.write_text(policy.read_text().replace('id = "syntax"', 'id = "syntax-v2"'))
        for command in ("check", "lint-config"):
            self.assertEqual(self.cli(command).returncode, 1)
        self.assertEqual(self.cli("init", "--sync").returncode, 0)
        self.assertTrue(agents.read_text().startswith("# My notes"))
        self.assertEqual(self.cli("lint-config").returncode, 0)

    def test_syntax_finding_exit_one_and_no_source_in_receipt(self):
        self.init()
        source = self.root / "broken.py"
        source.write_text("SECRET_EXAMPLE = 'not-a-real-secret'\ndef broken(:\n")
        result = self.cli("check", "--format", "json")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(any(f["rule"] == "syntax" for f in payload["findings"]))
        receipt = (self.root / payload["receipt"]).read_text()
        self.assertNotIn("SECRET_EXAMPLE", receipt)
        self.assertNotIn("not-a-real-secret", receipt)

    def test_empty_checks_and_unknown_rules_are_errors(self):
        for policy in ('version = 1\nplugins = ["aidd_gate_verify"]\nchecks = []\n',
                       'version = 1\nplugins = ["aidd_gate_verify"]\n[[checks]]\nid="x"\nkind="unknown"\n'):
            (self.root / "aidd-gate.toml").write_text(policy)
            result = self.cli("check", "--format", "json")
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertTrue(json.loads(result.stdout)["receipt"])

    def test_init_does_not_overwrite_existing_policy(self):
        self.init()
        policy = self.root / "aidd-gate.toml"
        original = policy.read_bytes()
        self.assertEqual(self.cli("init").returncode, 2)
        self.assertEqual(policy.read_bytes(), original)

    def test_symlink_receipt_directory_cannot_escape(self):
        self.init()
        with tempfile.TemporaryDirectory() as outside:
            (self.root / ".aidd-gate").symlink_to(outside, target_is_directory=True)
            result = self.cli("check", "--format", "json")
            self.assertEqual(result.returncode, 2)
            self.assertIsNone(json.loads(result.stdout)["receipt"])
            self.assertFalse(list(Path(outside).iterdir()))

    def test_external_plugin_registers_check_hook_and_command(self):
        plugin = self.root / "test_extension.py"
        plugin.write_text('''from aidd_gate.api import Finding
API_VERSION = 1
def check(ctx, spec):
    return []
def hook(ctx, slot):
    return [Finding("extension.hook", "Denied by fixture hook")]
def command(ctx, argv):
    return 0 if argv == ["ok"] else 1
def register(registry):
    registry.add_check("extension.check", check)
    registry.add_hook("CI", hook)
    registry.add_command("extension-command", command)
''')
        (self.root / "aidd-gate.toml").write_text('''version = 1
plugins = ["test_extension"]
[[checks]]
id = "extension"
kind = "extension.check"
''')
        env = dict(os.environ, PYTHONPATH=str(self.root))
        base = [sys.executable, str(CLI), "--root", str(self.root)]
        for args, expected in ((["check"], 0), (["hooks", "run", "--slot", "CI"], 1),
                               (["plugin", "extension-command", "ok"], 0)):
            result = subprocess.run(base + args, env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, expected, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
