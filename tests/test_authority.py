"""Trusted permission policy must survive candidate edits and index tricks."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from exitzero.api import Context, Registry
from exitzero.policy import parse_policy, sync_agents
from exitzero.runner import run
from exitzero_ledger import _build_record, _load_receipts, _render_markdown

CLI = Path(__file__).resolve().parents[1] / "bin/exitzero"
POLICY = '''version = 1
plugins = ["exitzero_verify", "exitzero_harness"]
[[checks]]
id = "syntax"
kind = "python.syntax"
paths = ["app.py"]
[permissions]
editable = ["app.py", "scratch/**"]
protected = ["tests/**"]
immutable = ["config/**"]
'''


class AuthorityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                    "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
                    "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.invalid"}
        self.git("init", "-q")
        self.write(".gitignore", ".exitzero/\n__pycache__/\n")
        self.write("exitzero.toml", POLICY)
        self.write("app.py", "value = 1\n")
        self.write("tests/test_app.py", "value = 1\n")
        self.write("config/frozen.json", "{}\n")
        sync_agents(self.root, parse_policy(POLICY))
        self.git("add", ".")
        self.git("-c", "commit.gpgsign=false", "commit", "-qm", "baseline")
        self.base = self.git("rev-parse", "HEAD").strip()

    def git(self, *args):
        return subprocess.check_output(["git", *args], cwd=self.root, env=self.env, text=True)

    def write(self, name, text):
        target = self.root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)

    def gate(self, expected, **kwargs):
        receipt = run(self.root, "exitzero.toml", "check", trust_base=self.base, **kwargs)
        self.assertEqual(receipt["exit_code"], expected, receipt)
        self.assertEqual(receipt, json.loads((self.root / receipt["receipt"]).read_text()))
        return receipt

    def test_unchanged_then_editable_change_is_bound_to_baseline_and_bytes(self):
        self.assertEqual(self.gate(0)["permissions"]["changes"], [])
        self.write("app.py", "value = 2\n")
        receipt = self.gate(0)
        permissions = receipt["permissions"]
        self.assertEqual(permissions["base_commit"], self.base)
        self.assertEqual(permissions["status"], "passed")
        self.assertEqual(permissions["changes"][0]["decision"], "allowed")
        self.assertEqual(permissions["changes"][0]["after_sha256"], receipt["inputs"]["app.py"])

    def test_protected_immutable_and_unclassified_changes_fail(self):
        for path, zone, decision in (("tests/test_app.py", "protected", "review_required"),
                                     ("config/frozen.json", "immutable", "denied"),
                                     ("unknown.py", "unclassified", "denied")):
            with self.subTest(zone=zone):
                self.write(path, "value = 2\n")
                receipt = self.gate(1)
                change = next(c for c in receipt["permissions"]["changes"] if c["path"] == path)
                self.assertEqual((change["zone"], change["decision"]), (zone, decision))
                self.assertEqual(receipt["checks"], [])

    def test_deleted_and_new_protected_files_cannot_disappear(self):
        (self.root / "tests/test_app.py").unlink()
        self.write("tests/new.py", "value = 1\n")
        changes = self.gate(1)["permissions"]["changes"]
        self.assertEqual({c["change"] for c in changes}, {"added", "deleted"})
        self.assertTrue(all(c["zone"] == "protected" for c in changes))

    def test_candidate_cannot_remove_authority_or_load_new_plugin_first(self):
        self.write("exitzero.toml", 'version = 1\nplugins = ["hostile"]\n')
        with patch("exitzero.runner.discover") as discover:
            receipt = self.gate(1)
        discover.assert_not_called()
        self.assertEqual(receipt["permissions"]["changes"][0]["zone"], "immutable")
        self.assertNotIn("hostile", json.dumps(receipt))

    def test_deleted_policy_is_a_violation_before_any_candidate_load(self):
        (self.root / "exitzero.toml").unlink()
        self.assertEqual(self.gate(1)["permissions"]["changes"][0]["change"], "deleted")

    def test_missing_or_invalid_independent_baseline_never_passes(self):
        self.assertEqual(run(self.root, "exitzero.toml", "check")["exit_code"], 2)
        for ref in ("missing", "--help"):
            with self.subTest(ref=ref):
                receipt = run(self.root, "exitzero.toml", "check", trust_base=ref)
                self.assertEqual(receipt["exit_code"], 2)
                self.assertIsNotNone(receipt["receipt"])

    def test_diff_and_reuse_do_not_bypass_permission_inspection(self):
        self.gate(0)
        self.write("tests/test_app.py", "value = 2\n")
        self.assertEqual(self.gate(1, diff=self.base, reuse=True)["permissions"]["status"], "failed")

    def test_assume_unchanged_index_bit_does_not_hide_edits(self):
        self.git("update-index", "--assume-unchanged", "tests/test_app.py")
        self.write("tests/test_app.py", "value = 9\n")
        self.assertEqual(self.git("diff", "--name-only"), "")
        self.assertEqual(self.gate(1)["permissions"]["changes"][0]["path"], "tests/test_app.py")

    def test_mode_changes_are_recorded(self):
        target = self.root / "config/frozen.json"
        target.chmod(0o755)
        self.assertEqual(self.gate(1)["permissions"]["changes"][0]["change"], "modified")

    def test_midrun_mutation_outside_check_scope_invalidates_authority_evidence(self):
        registry = Registry()
        def mutate(context, spec):
            self.write("tests/test_app.py", "value = 99\n")
            return []
        registry.add_check("python.syntax", mutate)
        with patch("exitzero.runner.discover", return_value=registry):
            receipt = self.gate(1)
        self.assertEqual(receipt["permissions"]["status"], "unverified")
        self.assertIn("core.authority-changed", {f["rule"] for f in receipt["findings"]})

    def test_symlink_cannot_read_outside_root(self):
        (self.root / "app.py").unlink()
        (self.root / "app.py").symlink_to(self.root.parent / "private-marker")
        receipt = self.gate(2)
        self.assertNotIn("private-marker", json.dumps(receipt))

    def test_schema_rejects_incomplete_or_unsafe_zones(self):
        for bad in ('editable = ["../x"]\nprotected = []\nimmutable = []',
                    'editable = []', 'editable = [".env"]\nprotected = []\nimmutable = []'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_policy(POLICY.split("[permissions]")[0] + "[permissions]\n" + bad)

    def test_cli_check_and_generic_hook_forward_independent_base(self):
        for args in (("check",), ("hooks", "run", "--slot", "CI")):
            command = [sys.executable, str(CLI), "--root", str(self.root), *args,
                       "--trust-base", self.base, "--format", "json"]
            result = subprocess.run(command, capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(json.loads(result.stdout)["permissions"]["base_commit"], self.base)

    def test_installed_authority_flag_requires_a_valid_pin(self):
        manifest = self.root / ".exitzero/hooks.json"
        manifest.parent.mkdir()
        manifest.write_text(json.dumps({"version": 1, "files": {}, "entries": {}, "trust_base": self.base}))
        command = [sys.executable, str(CLI), "--root", str(self.root), "hooks", "run", "--slot", "CI",
                   "--use-installed-authority", "--format", "json"]
        passed = subprocess.run(command, capture_output=True, text=True, timeout=20)
        self.assertEqual(passed.returncode, 0, passed.stdout + passed.stderr)
        self.assertEqual(json.loads(passed.stdout)["permissions"]["base_commit"], self.base)
        manifest.unlink()
        failed = subprocess.run(command, capture_output=True, text=True, timeout=20)
        self.assertEqual(failed.returncode, 2, failed.stdout + failed.stderr)
        receipt = json.loads(failed.stdout)
        self.assertEqual(receipt["checks"], [])
        self.assertIn("core.hook-input", {f["rule"] for f in receipt["findings"]})

    def test_doctor_reports_missing_external_authority_without_executing_checks(self):
        receipt = run(self.root, "exitzero.toml", "doctor")
        self.assertEqual(receipt["exit_code"], 0)
        item = next(d for d in receipt["diagnostics"] if d["target"] == "permissions")
        self.assertEqual(item["state"], "unknown")
        self.assertEqual(item["runtime"], "unverified")

    def test_ledger_carries_permission_decisions_and_trusted_commit(self):
        self.write("tests/test_app.py", "value = 2\n")
        self.gate(1)
        receipts, corrupt = _load_receipts(self.root, None)
        record = _build_record(Context(self.root, {}, self.root / "exitzero.toml"), receipts, corrupt,
                               {"base": None, "since": None})
        self.assertEqual(record["permission_runs"][0]["base_commit"], self.base)
        self.assertEqual(record["permission_runs"][0]["changes"][0]["decision"], "review_required")
        self.assertIn("review_required", _render_markdown(record))

    def test_ledger_handles_malformed_zone_metadata_without_crashing(self):
        receipts = [{"status": "failed", "permissions": {
            "base_commit": self.base, "status": {}, "changes": [
                {"path": "app.py", "zone": {}, "decision": []}]}}]
        record = _build_record(Context(self.root, {}, self.root / "exitzero.toml"), receipts, [],
                               {"base": None, "since": None})
        self.assertEqual(record["permission_runs"][0]["status"], "unverified")
        self.assertEqual(record["permission_runs"][0]["changes"], [])


if __name__ == "__main__":
    unittest.main()
