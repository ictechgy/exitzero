import json
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
CLI = REPO / "bin/exitzero"

for source in sorted((REPO / "packages").glob("*/src")):
    sys.path.insert(0, str(source))

from exitzero.hooks import cursor_response


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

    def test_cursor_stop_identifies_actual_receipt_and_syntax_location(self):
        (self.root / "broken.py").write_text("def private_source_marker(:\n")
        result = self.cli("hooks", "run", "--adapter", "cursor", "--event", "stop", input='{}')
        self.assertEqual(result.returncode, 0, result.stderr)
        message = json.loads(result.stdout)["followup_message"]
        receipts = list((self.root / ".exitzero/runs").glob("*.json"))
        self.assertEqual(len(receipts), 1)
        receipt = json.loads(receipts[0].read_text())
        self.assertIn("Receipt: " + receipt["receipt"], message)
        self.assertEqual(receipt["exit_code"], 1)
        self.assertIn('"id": "syntax"', message)
        self.assertIn('"path": "broken.py", "line": 1', message)
        self.assertIn("Correct Python syntax", message)
        self.assertNotIn("private_source_marker", message)
        self.assertNotIn("Run exitzero check", message)

    def test_cursor_command_failure_omits_output_arguments_and_input_without_rerun(self):
        argv = ["{python}", "-c", "import sys; from pathlib import Path; "
                "path = Path('invocations'); path.write_text(path.read_text() + 'x' if path.exists() else 'x'); "
                "print('stdout_marker'); print('stderr_marker', file=sys.stderr); sys.exit(1)", "argv_marker"]
        (self.root / "exitzero.toml").write_text(
            'version = 1\nplugins = ["exitzero_verify"]\n[[checks]]\n'
            'id = "regression"\nkind = "command"\n[checks.options]\nargv = ' + json.dumps(argv) + '\n')
        result = self.cli("hooks", "run", "--adapter", "cursor", "--event", "stop",
                          input=json.dumps({"tool_input": "hook_input_marker", "env": "env_marker"}))
        self.assertEqual(result.returncode, 0, result.stderr)
        message = json.loads(result.stdout)["followup_message"]
        self.assertIn('"id": "regression", "kind": "command", "status": "failed"', message)
        self.assertIn("Review the configured check", message)
        self.assertEqual((self.root / "invocations").read_text(), "x")
        for private in ("stdout_marker", "stderr_marker", "argv_marker", "hook_input_marker", "env_marker", argv[2]):
            self.assertNotIn(private, result.stdout + result.stderr)
        receipt = json.loads(next((self.root / ".exitzero/runs").glob("*.json")).read_text())
        self.assertEqual(receipt["exit_code"], 1)
        self.assertIn(receipt["receipt"], message)

    def test_cursor_receipt_persistence_failure_is_explicit(self):
        directory = self.root / ".exitzero"
        directory.mkdir(exist_ok=True)
        (directory / "runs").write_text("not a directory")
        for event, field in (("stop", "followup_message"), ("preToolUse", "agent_message"),
                             ("postToolUse", "additional_context")):
            with self.subTest(event=event):
                result = self.cli("hooks", "run", "--adapter", "cursor", "--event", event, input='{}')
                self.assertEqual(result.returncode, 2, result.stderr)
                response = json.loads(result.stdout)
                self.assertIn("Receipt unavailable: persistence failed", response[field])
                self.assertIn("core.receipt", response[field])
                self.assertNotIn(".exitzero/runs/", response[field])
                if event == "preToolUse":
                    self.assertEqual(response["permission"], "deny")
        self.assertEqual((directory / "runs").read_text(), "not a directory")

    def test_cursor_feedback_bounds_diagnostics_and_omits_messages(self):
        receipt = {
            "exit_code": 1,
            "receipt": ".exitzero/runs/" + "a" * 32 + ".json",
            "checks": [{"id": "検" * 200, "kind": "command", "status": "failed"}] * 12,
            "findings": [{"rule": "rule\n" + "x" * 200, "path": "source\tname.py",
                          "line": 3, "message": "private_diagnostic_marker"}] * 12,
        }
        message = cursor_response(receipt, "stop", {})["followup_message"]
        summary = json.loads(message.split("messages omitted): ", 1)[1])
        self.assertEqual(len(summary["checks"]), 5)
        self.assertEqual(len(summary["findings"]), 5)
        self.assertEqual(summary["omitted_checks"], 7)
        self.assertEqual(summary["omitted_findings"], 7)
        self.assertLessEqual(len(summary["checks"][0]["id"]), 99)
        self.assertEqual(summary["findings"][0]["path"], "source?name.py")
        self.assertNotIn("private_diagnostic_marker", message)
        self.assertNotIn("\n", message)
        self.assertLess(len(message.encode("utf-8")), 10_000)

    def test_cursor_feedback_omits_sensitive_paths_and_invalid_receipt_reference(self):
        receipt = {
            "exit_code": 2, "receipt": "not-a-receipt.json", "checks": [],
            "findings": [{"rule": "core.receipt", "path": ".env.local", "line": 1},
                         {"rule": "custom", "path": "config/credentials.json"}],
        }
        message = cursor_response(receipt, "postToolUse", {})["additional_context"]
        summary = json.loads(message.split("messages omitted): ", 1)[1])
        self.assertEqual(summary["findings"], [{"rule": "core.receipt"}, {"rule": "custom"}])
        self.assertIn("Receipt unavailable", message)
        self.assertNotIn("not-a-receipt.json", message)
        self.assertNotIn(".env.local", message)
        self.assertNotIn("credentials.json", message)

    def test_unmapped_requirement_is_in_hook_feedback(self):
        policy = self.root / "exitzero.toml"
        policy.write_text(policy.read_text() + '\n[[requirements]]\nid = "acceptance"\n'
                          'description = "private_requirement_description"\nchecks = []\n')
        self.assertEqual(self.cli("init", "--sync").returncode, 0)
        result = self.cli("hooks", "run", "--adapter", "cursor", input='{}')
        self.assertEqual(result.returncode, 0)
        message = json.loads(result.stdout)["followup_message"]
        summary = json.loads(message.split("messages omitted): ", 1)[1])
        self.assertEqual(summary["requirements"], [{"id": "acceptance", "status": "unverified"}])
        self.assertNotIn("private_requirement_description", message)
        self.assertIn("not semantic proof", message)

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
