"""Black-box contracts for the CLI, policy, receipts and extension boundary."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
CLI = REPO / "bin" / "exitzero"


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
        self.assertEqual(len(list((self.root / ".exitzero/runs").glob("*.json"))), 2)

    def test_policy_error_still_has_receipt(self):
        (self.root / "exitzero.toml").write_text("version = [broken")
        result = self.cli("check", "--format", "json")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue((self.root / payload["receipt"]).is_file())
        self.assertEqual(payload["status"], "error")

    def test_drift_fails_check_and_lint_then_sync_preserves_notes(self):
        self.init()
        agents = self.root / "AGENTS.md"
        agents.write_text("# My notes\n\n" + agents.read_text())
        policy = self.root / "exitzero.toml"
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
        for policy in ('version = 1\nplugins = ["exitzero_verify"]\nchecks = []\n',
                       'version = 1\nplugins = ["exitzero_verify"]\n[[checks]]\nid="x"\nkind="unknown"\n'):
            (self.root / "exitzero.toml").write_text(policy)
            result = self.cli("check", "--format", "json")
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertTrue(json.loads(result.stdout)["receipt"])

    def test_init_does_not_overwrite_existing_policy(self):
        self.init()
        policy = self.root / "exitzero.toml"
        original = policy.read_bytes()
        self.assertEqual(self.cli("init").returncode, 2)
        self.assertEqual(policy.read_bytes(), original)

    def test_lint_does_not_execute_command_and_rejects_no_linter(self):
        self.init()
        policy = self.root / "exitzero.toml"
        policy.write_text('''version = 1
plugins = ["exitzero_verify", "exitzero_harness"]
[[checks]]
id = "command"
kind = "command"
[checks.options]
argv = ["{python}", "-c", "from pathlib import Path; Path('sentinel').touch()"]
''')
        self.assertEqual(self.cli("init", "--sync").returncode, 0)
        self.assertEqual(self.cli("lint-config").returncode, 0)
        self.assertFalse((self.root / "sentinel").exists())
        policy.write_text(policy.read_text().replace(', "exitzero_harness"', ''))
        result = self.cli("lint-config", "--format", "json")
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertFalse((self.root / "sentinel").exists())

    def test_symlink_receipt_directory_cannot_escape(self):
        self.init()
        with tempfile.TemporaryDirectory() as outside:
            (self.root / ".exitzero").symlink_to(outside, target_is_directory=True)
            result = self.cli("check", "--format", "json")
            self.assertEqual(result.returncode, 2)
            self.assertIsNone(json.loads(result.stdout)["receipt"])
            self.assertFalse(list(Path(outside).iterdir()))

    def test_external_plugin_registers_check_hook_and_command(self):
        plugin = self.root / "test_extension.py"
        plugin.write_text('''from exitzero.api import Finding
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
        (self.root / "exitzero.toml").write_text('''version = 1
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

    def test_plugin_exit_and_invalid_result_cannot_skip_receipt(self):
        (self.root / "exitzero.toml").write_text('''version = 1
plugins = ["broken_extension"]
[[checks]]
id = "broken"
kind = "broken"
''')
        for body in ("raise SystemExit(0)", "return [Finding('broken', object())]"):
            (self.root / "broken_extension.py").write_text(
                "from exitzero.api import Finding\nAPI_VERSION = 1\n"
                "def check(ctx, spec):\n    " + body + "\n"
                "def register(registry):\n    registry.add_check('broken', check)\n")
            env = dict(os.environ, PYTHONPATH=str(self.root), PYTHONDONTWRITEBYTECODE="1")
            result = subprocess.run([sys.executable, str(CLI), "--root", str(self.root), "check", "--format", "json"],
                                    env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            payload = json.loads(result.stdout)
            self.assertTrue((self.root / payload["receipt"]).is_file())
            self.assertEqual(payload["status"], "error")

    def test_installed_hook_changes_during_run_are_detected(self):
        import hashlib
        self.init()
        hook = self.root / "hook.sh"
        hook.write_text("#!/bin/sh\nexit 0\n")
        ledger = self.root / ".exitzero"
        ledger.mkdir()
        (ledger / "hooks.json").write_text(json.dumps({"version": 1, "files": {
            "hook.sh": hashlib.sha256(hook.read_bytes()).hexdigest()}}))
        (self.root / "exitzero.toml").write_text('''version = 1
plugins = ["exitzero_verify", "exitzero_harness"]
[[checks]]
id = "mutation"
kind = "command"
[checks.options]
argv = ["{python}", "-c", "from pathlib import Path; Path('hook.sh').write_text('changed')"]
''')
        self.assertEqual(self.cli("init", "--sync").returncode, 0)
        result = self.cli("check", "--format", "json")
        self.assertEqual(result.returncode, 1, result.stdout)
        payload = json.loads(result.stdout)
        self.assertIn("core.inputs-changed", [f["rule"] for f in payload["findings"]])
        self.assertIn("hook.sh", payload["inputs"])
        self.assertIn(".exitzero/hooks.json", payload["inputs"])


if __name__ == "__main__":
    unittest.main()
