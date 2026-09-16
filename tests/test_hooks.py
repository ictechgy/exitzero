import json
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
CLI = REPO / "bin/exitzero"


class HookTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="exitzero hook space ")
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
        self.assertEqual(hooks[1]["loop_limit"], 1)
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
        receipts = [json.loads(p.read_text()) for p in (self.root / ".exitzero/runs").glob("*.json")]
        self.assertEqual(len(receipts), 4)
        self.assertTrue(all(r["exit_code"] == 1 for r in receipts))

    def test_invalid_cursor_input_has_error_receipt(self):
        result = self.cli("hooks", "run", "--adapter", "cursor", input="invalid-json")
        self.assertEqual(result.returncode, 2)
        receipts = list((self.root / ".exitzero/runs").glob("*.json"))
        self.assertEqual(len(receipts), 1)
        receipt = json.loads(receipts[0].read_text())
        self.assertEqual(receipt["exit_code"], 2)
        self.assertEqual(receipt["status"], "error")

    def test_cursor_native_tool_events_preserve_gate_outcomes(self):
        for event, slot in (("preToolUse", "PreToolUse"), ("postToolUse", "PostToolUse")):
            with self.subTest(event=event):
                result = self.cli("hooks", "run", "--adapter", "cursor", "--event", event,
                                  input=json.dumps({"hook_event_name": event, "tool_name": "Shell", "tool_input": {}}))
                self.assertEqual(result.returncode, 0, result.stderr)
                expected = {"permission": "allow", "user_message": "exitzero: passed", "agent_message": ""} if event == "preToolUse" else {}
                self.assertEqual(json.loads(result.stdout), expected)
                receipt = json.loads(max((self.root / ".exitzero/runs").glob("*.json"), key=lambda p: p.stat().st_mtime_ns).read_text())
                self.assertEqual(receipt["hook_slot"], slot)
                self.assertEqual(receipt["exit_code"], 0)
        (self.root / "broken.py").write_text("def broken(:\n")
        for event in ("preToolUse", "beforeShellExecution", "beforeMCPExecution", "postToolUse"):
            with self.subTest(failed_event=event):
                result = self.cli("hooks", "run", "--adapter", "cursor", "--event", event, input='{}')
                self.assertEqual(result.returncode, 0, result.stderr)
                response = json.loads(result.stdout)
                if event == "postToolUse":
                    self.assertIn("additional_context", response)
                else:
                    self.assertEqual(response["permission"], "deny")
        receipts = [json.loads(path.read_text()) for path in (self.root / ".exitzero/runs").glob("*.json")]
        self.assertEqual(sum(r["exit_code"] == 1 for r in receipts), 4)

    def test_cursor_aborted_stop_does_not_request_another_turn(self):
        (self.root / "broken.py").write_text("def broken(:\n")
        result = self.cli("hooks", "run", "--adapter", "cursor", "--event", "stop",
                          input='{"status":"aborted","loop_count":0}')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {})
        receipt = json.loads(next((self.root / ".exitzero/runs").glob("*.json")).read_text())
        self.assertEqual(receipt["exit_code"], 1)

    def test_cursor_install_preserves_prompt_hooks(self):
        directory = self.root / ".cursor"
        directory.mkdir()
        config = directory / "hooks.json"
        prompt = {"type": "prompt", "prompt": "Check completion requirements.", "timeout": 10}
        config.write_text(json.dumps({"version": 1, "hooks": {"stop": [prompt]}}))
        for _ in range(2):
            result = self.cli("hooks", "install")
            self.assertEqual(result.returncode, 0, result.stderr)
        hooks = json.loads(config.read_text())["hooks"]["stop"]
        self.assertEqual(hooks[0], prompt)
        self.assertEqual(len(hooks), 2)
        self.assertEqual(self.cli("lint-config").returncode, 0)

    def test_install_rejects_invalid_existing_config_without_modifying_it(self):
        directory = self.root / ".cursor"
        directory.mkdir()
        path = directory / "hooks.json"
        for config in ({"version": True, "hooks": {}},
                       {"version": 1, "hooks": {"stop": [{"command": " "}]}}):
            original = json.dumps(config)
            path.write_text(original)
            result = self.cli("hooks", "install")
            self.assertEqual(result.returncode, 2, result.stdout)
            self.assertEqual(path.read_text(), original)

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
        sample = self.root / "exitzero_sample.py"
        sample.write_text(sample.read_text() + "# unstaged edit\n")
        result = subprocess.run([str(hook)], cwd=self.root, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("core.index-mismatch", result.stdout)


if __name__ == "__main__":
    unittest.main()
