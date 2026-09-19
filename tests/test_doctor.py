"""Setup diagnosis must stay separate from execution and enforcement evidence."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from exitzero.doctor import diagnose
from exitzero.runner import run

REPO = Path(__file__).resolve().parents[1]
CLI = REPO / "bin" / "exitzero"


class DoctorTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.cli("init")

    def cli(self, *args, expected=0):
        result = subprocess.run([sys.executable, str(CLI), "--root", str(self.root), *args],
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return result

    def doctor(self, *args, expected=0):
        result = self.cli("doctor", *args, "--format", "json", expected=expected)
        receipt = json.loads(result.stdout)
        self.assertEqual(receipt["command"], "doctor")
        self.assertEqual(json.loads((self.root / receipt["receipt"]).read_text()), receipt)
        return receipt

    def row(self, receipt, target):
        return next(item for item in receipt["diagnostics"] if item["target"] == target)

    def edit_cursor(self, **changes):
        path = self.root / ".cursor/hooks.json"
        data = json.loads(path.read_text())
        data["hooks"]["stop"][0].update(changes)
        path.write_text(json.dumps(data))

    def test_optional_adapters_and_ci_never_claim_runtime_verification(self):
        receipt = self.doctor()
        self.assertEqual(self.row(receipt, "cursor")["state"], "missing")
        self.assertEqual(self.row(receipt, "CI")["state"], "unknown")
        self.assertEqual({item["runtime"] for item in receipt["diagnostics"]}, {"unverified"})
        self.assertEqual({check["kind"] for check in receipt["checks"]}, {"config-lint"})
        human = self.cli("doctor").stdout
        self.assertIn("verification checks were not run", human)
        self.assertIn("not merge protection", human)

    def test_required_adapter_missing_then_installed(self):
        missing = self.doctor("--adapter", "cursor", expected=1)
        self.assertIn("doctor.required-adapter", {f["rule"] for f in missing["findings"]})
        self.cli("hooks", "install", "--adapter", "cursor")
        installed = self.doctor("--adapter", "cursor")
        self.assertEqual(self.row(installed, "cursor")["state"], "configured")
        self.assertIn(".cursor/hooks.json", installed["inputs"])
        self.assertEqual(self.row(installed, "cursor")["runtime"], "unverified")

    def test_reinstalled_fail_open_hook_still_fails_diagnosis(self):
        self.cli("hooks", "install", "--adapter", "cursor")
        self.edit_cursor(failClosed=False)
        self.cli("hooks", "install", "--adapter", "cursor")
        self.cli("lint-config")
        receipt = self.doctor(expected=1)
        self.assertIn("doctor.fail-closed", {f["rule"] for f in receipt["findings"]})
        self.assertEqual(self.row(receipt, "cursor")["state"], "misconfigured")

    def test_reinstalled_disabled_loop_and_bad_timeout_are_errors(self):
        self.cli("hooks", "install", "--adapter", "cursor")
        self.edit_cursor(loop_limit=0, timeout=True)
        self.cli("hooks", "install", "--adapter", "cursor")
        receipt = self.doctor(expected=1)
        self.assertTrue({"doctor.loop-limit", "doctor.hook-timeout"} <= {f["rule"] for f in receipt["findings"]})

    def test_commands_are_not_executed_and_requirements_stay_unverified(self):
        policy = self.root / "exitzero.toml"
        policy.write_text(policy.read_text() + '\n[[checks]]\nid = "side-effect"\nkind = "command"\n'
                          'paths = ["**/*.py"]\n[checks.options]\nargv = ["{python}", "side_effect.py"]\n'
                          '\n[[requirements]]\nid = "done"\ndescription = "private-description"\nchecks = ["side-effect"]\n')
        (self.root / "side_effect.py").write_text('from pathlib import Path\nPath("EXECUTED").touch()\n')
        self.cli("init", "--sync")
        receipt = self.doctor()
        self.assertFalse((self.root / "EXECUTED").exists())
        self.assertEqual(receipt["requirements"][0]["status"], "unverified")
        self.assertNotIn("private-description", json.dumps(receipt))

    def test_all_installed_client_adapters_are_recognized(self):
        for adapter in ("claude", "codex", "gemini", "agy"):
            with self.subTest(adapter=adapter):
                self.cli("hooks", "install", "--adapter", adapter)
                receipt = self.doctor("--adapter", adapter)
                self.assertEqual(self.row(receipt, adapter)["state"], "configured")

    def test_managed_only_is_error_for_installed_project_hook(self):
        self.cli("hooks", "install", "--adapter", "claude")
        path = self.root / ".claude/settings.json"
        data = json.loads(path.read_text())
        data["allowManagedHooksOnly"] = True
        data["unrelated"] = "private-settings-marker"
        path.write_text(json.dumps(data))
        receipt = self.doctor(expected=1)
        self.assertIn("doctor.managed-only", {f["rule"] for f in receipt["findings"]})
        self.assertNotIn("private-settings-marker", json.dumps(receipt))

    def test_drift_and_wrong_policy_are_reported(self):
        self.cli("hooks", "install", "--adapter", "cursor")
        self.edit_cursor(command="private-command-marker")
        receipt = self.doctor(expected=1)
        self.assertTrue({"harness.hooks-drift", "doctor.hook-command"} <= {f["rule"] for f in receipt["findings"]})
        self.assertNotIn("private-command-marker", json.dumps(receipt))

    def test_malformed_unmanaged_config_fails_without_leaking_contents(self):
        path = self.root / ".gemini/settings.json"
        path.parent.mkdir()
        path.write_text('{"private-value": bad}')
        receipt = self.doctor(expected=1)
        self.assertEqual(self.row(receipt, "gemini")["state"], "misconfigured")
        self.assertNotIn("private-value", json.dumps(receipt))

    def test_unsafe_and_nonregular_configs_fail_with_receipts(self):
        path = self.root / ".gemini/settings.json"
        path.parent.mkdir()
        path.symlink_to(self.root / "exitzero.toml")
        self.doctor(expected=2)
        path.unlink()
        path.mkdir()
        self.doctor(expected=2)

    def test_input_mutation_invalidates_setup_diagnosis(self):
        self.cli("hooks", "install", "--adapter", "cursor")
        def mutate(context, adapter):
            result = diagnose(context, adapter)
            self.edit_cursor(failClosed=False)
            return result
        with patch("exitzero.runner.diagnose", side_effect=mutate):
            receipt = run(self.root, "exitzero.toml", "doctor")
        self.assertEqual(receipt["exit_code"], 1)
        self.assertIn("core.inputs-changed", {f["rule"] for f in receipt["findings"]})
        self.assertEqual({item["state"] for item in receipt["diagnostics"]}, {"unknown"})

    def test_invalid_policy_and_persistence_failure_cannot_pass(self):
        (self.root / "exitzero.toml").write_text("invalid = [")
        self.doctor(expected=2)
        with patch("exitzero.runner.persist", side_effect=OSError):
            receipt = run(self.root, "exitzero.toml", "doctor")
        self.assertEqual(receipt["exit_code"], 2)
        self.assertIsNone(receipt["receipt"])

    def test_precommit_executable_and_changed_active_directory(self):
        env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
        init = subprocess.run(["git", "init", str(self.root)], env=env, capture_output=True)
        self.assertEqual(init.returncode, 0)
        self.cli("hooks", "install", "--adapter", "pre-commit")
        receipt = self.doctor("--adapter", "pre-commit")
        hook = self.root / self.row(receipt, "pre-commit")["path"]
        (self.root / "other.toml").write_text((self.root / "exitzero.toml").read_text())
        other_policy = run(self.root, "other.toml", "doctor")
        self.assertEqual(other_policy["exit_code"], 1)
        self.assertIn("doctor.hook-command", {f["rule"] for f in other_policy["findings"]})
        hook.chmod(0o644)
        receipt = self.doctor(expected=1)
        self.assertIn("doctor.hook-executable", {f["rule"] for f in receipt["findings"]})
        changed = subprocess.run(["git", "-C", str(self.root), "config", "core.hooksPath", "other-hooks"],
                                 env=env, capture_output=True)
        self.assertEqual(changed.returncode, 0)
        receipt = self.doctor("--adapter", "pre-commit", expected=1)
        self.assertEqual(self.row(receipt, "pre-commit")["state"], "misconfigured")
        self.assertIn("doctor.hook-inactive", {f["rule"] for f in receipt["findings"]})

    def test_external_git_hooks_are_not_read(self):
        with patch("exitzero.doctor.subprocess.run", return_value=subprocess.CompletedProcess(
                [], 0, stdout=str(self.root.parent / "external-hooks/pre-commit"))):
            receipt = run(self.root, "exitzero.toml", "doctor")
        self.assertEqual(receipt["exit_code"], 0)
        self.assertEqual(self.row(receipt, "pre-commit")["state"], "unknown")
        self.assertNotIn("external-hooks", json.dumps(receipt))

    def test_agents_drift_is_not_hidden_by_configured_hook(self):
        self.cli("hooks", "install", "--adapter", "cursor")
        agents = self.root / "AGENTS.md"
        agents.write_text(agents.read_text().replace("Required checks:", "Wrong checks:"))
        receipt = self.doctor(expected=1)
        self.assertIn("harness.agents.drift", {f["rule"] for f in receipt["findings"]})
        self.assertEqual(self.row(receipt, "cursor")["runtime"], "unverified")


if __name__ == "__main__":
    unittest.main()
