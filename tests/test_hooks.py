import json
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
CLI = REPO / "bin/aidd-gate"


class HookTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aidd hook space ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.assertEqual(self.cli("init").returncode, 0)

    def cli(self, *args, input=None):
        return subprocess.run([sys.executable, str(CLI), "--root", str(self.root), *args],
                              input=input, capture_output=True, text=True, timeout=30)

    def test_cursor_install_preserves_hooks_and_is_idempotent(self):
        directory = self.root / ".cursor"
        directory.mkdir()
        config = directory / "hooks.json"
        config.write_text(json.dumps({"version": 1, "hooks": {"stop": [{"command": "echo existing"}]}}))
        for _ in range(2):
            result = self.cli("hooks", "install")
            self.assertEqual(result.returncode, 0, result.stderr)
        hooks = json.loads(config.read_text())["hooks"]["stop"]
        self.assertEqual(len(hooks), 2)
        self.assertEqual(hooks[0]["command"], "echo existing")
        installed = subprocess.run(shlex.split(hooks[1]["command"]), input='{"loop_count":0}',
                                   capture_output=True, text=True, timeout=30, cwd=self.root)
        self.assertEqual(installed.returncode, 0, installed.stderr)
        self.assertEqual(json.loads(installed.stdout), {})
        self.assertEqual(self.cli("lint-config").returncode, 0)
        config.write_text(config.read_text() + " ")
        self.assertEqual(self.cli("lint-config").returncode, 1)

    def test_cursor_failure_and_bounded_stop_loop(self):
        (self.root / "broken.py").write_text("def broken(:\n")
        first = self.cli("hooks", "run", "--adapter", "cursor", "--event", "stop", input='{"loop_count":0}')
        self.assertIn("followup_message", json.loads(first.stdout))
        second = self.cli("hooks", "run", "--adapter", "cursor", "--event", "stop", input='{"loop_count":1}')
        self.assertEqual(json.loads(second.stdout), {})
        pre = self.cli("hooks", "run", "--adapter", "cursor", "--event", "beforeShellExecution", input='{}')
        self.assertEqual(json.loads(pre.stdout)["permission"], "deny")
        generic = self.cli("hooks", "run", "--slot", "CI")
        self.assertEqual(generic.returncode, 1)
        receipts = [json.loads(p.read_text()) for p in (self.root / ".aidd-gate/runs").glob("*.json")]
        self.assertEqual(len(receipts), 4)
        self.assertTrue(all(r["exit_code"] == 1 for r in receipts))

    def test_precommit_preserves_existing_hook_and_checks_index_consistency(self):
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        hook = self.root / ".git/hooks/pre-commit"
        hook.write_text("#!/bin/sh\necho existing\n")
        self.assertEqual(self.cli("hooks", "install", "--adapter", "pre-commit").returncode, 2)
        self.assertIn("echo existing", hook.read_text())
        hook.unlink()
        self.assertEqual(self.cli("hooks", "install", "--adapter", "pre-commit").returncode, 0)
        subprocess.run(["git", "-C", str(self.root), "add", "."], check=True)
        result = subprocess.run([str(hook)], cwd=self.root, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        sample = self.root / "aidd_gate_sample.py"
        sample.write_text(sample.read_text() + "# unstaged edit\n")
        result = subprocess.run([str(hook)], cwd=self.root, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("core.index-mismatch", result.stdout)


if __name__ == "__main__":
    unittest.main()
