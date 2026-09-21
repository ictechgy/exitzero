"""A changed dependency must recheck unchanged consumers under --diff."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

CLI = Path(__file__).resolve().parents[1] / "bin/exitzero"


class DependencyScopeTests(unittest.TestCase):
    def test_removed_export_breaks_unchanged_consumer_in_diff_mode(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                   "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
                   "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.invalid"}
            (root / "provider.py").write_text("def answer():\n    return 42\n")
            (root / "consumer.py").write_text("from provider import answer\n")
            (root / "exitzero.toml").write_text('version = 1\nplugins = ["exitzero_verify"]\n'
                '[[checks]]\nid = "imports"\nkind = "python.imports"\npaths = ["consumer.py"]\n'
                '[checks.options]\nroots = ["."]\nallow_modules = []\n')
            for args in (("init", "-q"), ("add", "."), ("-c", "commit.gpgsign=false", "commit", "-qm", "baseline")):
                subprocess.run(["git", *args], cwd=root, env=env, check=True, capture_output=True)
            (root / "provider.py").write_text("def renamed():\n    return 42\n")
            result = subprocess.run([sys.executable, str(CLI), "--root", str(root),
                                     "check", "--diff", "HEAD", "--format", "json"],
                                    capture_output=True, text=True, timeout=20)
            receipt = json.loads(result.stdout)
            self.assertEqual(result.returncode, 1, receipt)
            self.assertIn("consumer.py", {finding["path"] for finding in receipt["findings"]})
            self.assertEqual(receipt["checks"][0]["status"], "failed")
            self.assertEqual(receipt, json.loads((root / receipt["receipt"]).read_text()))

    def test_untracked_shadow_package_rechecks_existing_connection(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                   "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
                   "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.invalid"}
            (root / "provider.py").write_text("def answer():\n    return 42\n")
            (root / "consumer.py").write_text("from provider import answer\nanswer()\n")
            (root / "exitzero.toml").write_text('version = 1\nplugins = ["exitzero_verify"]\n'
                '[[checks]]\nid = "connected"\nkind = "python.connections"\npaths = ["consumer.py"]\n'
                '[checks.options]\nroots = ["."]\n[[checks.options.connections]]\n'
                'source = "consumer.py"\ntarget = "provider.py"\nsymbol = "answer"\n'
                'within = "<module>"\nusage = "call"\n')
            for args in (("init", "-q"), ("add", "."), ("-c", "commit.gpgsign=false", "commit", "-qm", "baseline")):
                subprocess.run(["git", *args], cwd=root, env=env, check=True, capture_output=True)
            command = [sys.executable, str(CLI), "--root", str(root), "check", "--format", "json"]
            initial = subprocess.run(command, capture_output=True, text=True, timeout=20)
            self.assertEqual(initial.returncode, 0, initial.stdout)
            (root / "provider").mkdir()
            (root / "provider/__init__.py").write_text("def answer():\n    return 0\n")
            result = subprocess.run([*command, "--diff", "HEAD"], capture_output=True, text=True, timeout=20)
            receipt = json.loads(result.stdout)
            self.assertEqual(result.returncode, 1, receipt)
            self.assertIn("provider/__init__.py", receipt["inputs"])
            self.assertEqual(receipt["checks"][0]["status"], "failed")


if __name__ == "__main__":
    unittest.main()
