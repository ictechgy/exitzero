import json
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages/core/src"))

from exitzero.pack import compile_pack


class PolicyPackTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.policy_path = self.root / "exitzero.toml"
        self.policy_path.write_text("version = 1\n", encoding="utf-8")

    def policy(self, adapters=("cursor", "claude"), **extra):
        policy = {
            "version": 1,
            "plugins": ["exitzero_verify"],
            "checks": [{"id": "syntax", "kind": "python.syntax", "paths": ["**/*.py"]}],
            "harness": {"config_files": [], "rules": []},
            "clients": {"adapters": list(adapters)},
        }
        policy.update(extra)
        permission_text = ""
        if "permissions" in policy:
            permissions = policy["permissions"]
            permission_text = ("\n[permissions]\n"
                               + "editable = " + json.dumps(permissions["editable"]) + "\n"
                               + "protected = " + json.dumps(permissions["protected"]) + "\n"
                               + "immutable = " + json.dumps(permissions["immutable"]) + "\n")
        self.policy_path.write_text(
            "version = 1\nplugins = [\"exitzero_verify\"]\n\n"
            "[[checks]]\nid = \"syntax\"\nkind = \"python.syntax\"\npaths = [\"**/*.py\"]\n\n"
            "[harness]\nconfig_files = []\nrules = []\n\n"
            "[clients]\nadapters = " + json.dumps(list(adapters)) + permission_text,
            encoding="utf-8")
        return policy

    def test_preview_is_read_only_deterministic_and_sanitized(self):
        policy = self.policy()
        first = compile_pack(self.root, self.policy_path, policy)
        second = compile_pack(self.root, self.policy_path, policy)
        self.assertEqual(first, second)
        self.assertFalse(first["applied"])
        self.assertEqual(set(first), {"schema_version", "applied", "changes", "adapters", "limitations"})
        self.assertFalse((self.root / ".cursor/hooks.json").exists())
        self.assertTrue(all(set(change) == {"path", "state", "before_sha256", "after_sha256"}
                            for change in first["changes"]))
        self.assertNotIn("content", json.dumps(first))

    def test_stale_policy_dict_is_rejected_before_generating_any_output(self):
        policy = self.policy(adapters=("cursor",))
        stale = dict(policy)
        stale["clients"] = {"adapters": ["claude"]}
        with self.assertRaises(ValueError):
            compile_pack(self.root, self.policy_path, stale, apply=True)
        self.assertFalse((self.root / "AGENTS.md").exists())
        self.assertFalse((self.root / ".cursor/hooks.json").exists())

    def test_multi_adapter_apply_repeat_and_foreign_content(self):
        cursor = self.root / ".cursor/hooks.json"
        cursor.parent.mkdir()
        cursor.write_text(json.dumps({"version": 1, "hooks": {"stop": [{"command": "foreign"}]}}), encoding="utf-8")
        agents = self.root / "AGENTS.md"
        agents.write_text("Manual project guidance.\n", encoding="utf-8")
        policy = self.policy()
        applied = compile_pack(self.root, self.policy_path, policy, apply=True)
        self.assertTrue(applied["applied"])
        before = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        repeated = compile_pack(self.root, self.policy_path, policy, apply=True)
        self.assertTrue(all(change["state"] == "unchanged" for change in repeated["changes"]))
        after = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(before, after)
        self.assertEqual(json.loads(cursor.read_text())["hooks"]["stop"][0]["command"], "foreign")
        self.assertIn("Manual project guidance.", agents.read_text())

    def test_later_invalid_document_causes_zero_early_writes(self):
        agents = self.root / "AGENTS.md"
        agents.write_text("manual\n", encoding="utf-8")
        settings = self.root / ".claude/settings.json"
        settings.parent.mkdir()
        settings.write_text("{broken", encoding="utf-8")
        with self.assertRaises(ValueError):
            compile_pack(self.root, self.policy_path, self.policy(), apply=True)
        self.assertEqual(agents.read_text(), "manual\n")
        self.assertFalse((self.root / ".cursor/hooks.json").exists())
        self.assertEqual(settings.read_text(), "{broken")

    def test_nonregular_and_symlink_targets_are_refused(self):
        cursor = self.root / ".cursor/hooks.json"
        cursor.parent.mkdir()
        cursor.mkdir()
        with self.assertRaises(ValueError):
            compile_pack(self.root, self.policy_path, self.policy(adapters=("cursor",)))
        cursor.rmdir()
        outside = self.root.parent / (self.root.name + "-outside")
        outside.write_text("{}", encoding="utf-8")
        self.addCleanup(outside.unlink)
        cursor.symlink_to(outside)
        with self.assertRaises(ValueError):
            compile_pack(self.root, self.policy_path, self.policy(adapters=("cursor",)))

    def test_cursor_false_fail_closed_and_bad_timeout_are_refused(self):
        policy = self.policy(adapters=("cursor",))
        compile_pack(self.root, self.policy_path, policy, apply=True)
        path = self.root / ".cursor/hooks.json"
        data = json.loads(path.read_text())
        data["hooks"]["stop"][0]["failClosed"] = False
        path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(ValueError):
            compile_pack(self.root, self.policy_path, policy)
        data["hooks"]["stop"][0]["failClosed"] = True
        data["hooks"]["stop"][0]["timeout"] = 0
        path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(ValueError):
            compile_pack(self.root, self.policy_path, policy)

    def test_custom_git_path_and_foreign_hook_are_safe(self):
        subprocess.run(["git", "-C", str(self.root), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(self.root), "config", "core.hooksPath", ".githooks"], check=True)
        hook = self.root / ".githooks/pre-push"
        hook.parent.mkdir()
        hook.write_text("#!/bin/sh\necho foreign\n", encoding="utf-8")
        hook.chmod(0o755)
        with self.assertRaises(ValueError):
            compile_pack(self.root, self.policy_path, self.policy(adapters=("pre-push",)), apply=True)
        self.assertIn("foreign", hook.read_text())
        self.assertFalse((self.root / "AGENTS.md").exists())
        hook.unlink()
        result = compile_pack(self.root, self.policy_path, self.policy(adapters=("pre-push",)), apply=True)
        self.assertTrue(result["applied"])
        self.assertTrue(os.access(hook, os.X_OK))

    def test_existing_git_hook_mode_drift_is_reported_and_repaired(self):
        subprocess.run(["git", "-C", str(self.root), "init", "-q"], check=True)
        policy = self.policy(adapters=("pre-push",))
        compile_pack(self.root, self.policy_path, policy, apply=True)
        hook = self.root / ".git/hooks/pre-push"
        content = hook.read_bytes()
        hook.chmod(0o644)
        preview = compile_pack(self.root, self.policy_path, policy)
        change = next(item for item in preview["changes"] if item["path"].endswith("pre-push"))
        self.assertEqual(change["state"], "update")
        self.assertEqual(change["before_sha256"], change["after_sha256"])
        compile_pack(self.root, self.policy_path, policy, apply=True)
        self.assertEqual(hook.read_bytes(), content)
        self.assertTrue(os.access(hook, os.X_OK))

    def test_trust_pin_update_rotates_owned_hooks_and_preserves_foreign(self):
        subprocess.run(["git", "-C", str(self.root), "init", "-q"], check=True)
        policy = self.policy(adapters=("cursor", "pre-push"))
        env = {**os.environ, "GIT_AUTHOR_NAME": "Pack Test", "GIT_AUTHOR_EMAIL": "pack@example.invalid",
               "GIT_COMMITTER_NAME": "Pack Test", "GIT_COMMITTER_EMAIL": "pack@example.invalid"}
        subprocess.run(["git", "-C", str(self.root), "add", "exitzero.toml"], check=True)
        subprocess.run(["git", "-C", str(self.root), "commit", "-qm", "baseline-a"], check=True, env=env)
        cursor = self.root / ".cursor/hooks.json"
        cursor.parent.mkdir()
        cursor.write_text(json.dumps({"version": 1, "hooks": {"stop": [{"command": "foreign"}]}}), encoding="utf-8")
        compile_pack(self.root, self.policy_path, policy, apply=True, trust_base="HEAD")
        old_hash = subprocess.check_output(["git", "-C", str(self.root), "rev-parse", "HEAD"], text=True).strip()
        cursor_before = cursor.read_bytes()
        git_before = (self.root / ".git/hooks/pre-push").read_bytes()
        marker = self.root / "marker.txt"
        marker.write_text("baseline-b\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "marker.txt"], check=True)
        subprocess.run(["git", "-C", str(self.root), "commit", "-qm", "baseline-b"], check=True, env=env)
        compile_pack(self.root, self.policy_path, policy, apply=True, trust_base="HEAD")
        self.assertEqual(cursor.read_bytes(), cursor_before)
        self.assertEqual((self.root / ".git/hooks/pre-push").read_bytes(), git_before)
        stops = json.loads(cursor.read_text())["hooks"]["stop"]
        self.assertEqual(stops[0], {"command": "foreign"})
        owned = [entry for entry in stops if entry.get("command", "").find("--adapter cursor") >= 0]
        self.assertEqual(len(owned), 1)
        self.assertIn("--use-installed-authority", owned[0]["command"])
        self.assertNotIn(old_hash, owned[0]["command"])
        git_hook = self.root / ".git/hooks/pre-push"
        git_text = git_hook.read_text()
        self.assertIn("--use-installed-authority", git_text)
        self.assertNotIn(old_hash, git_text)

    def test_concurrent_edit_is_rejected_before_publication(self):
        policy = self.policy(adapters=("cursor",))
        original = __import__("exitzero.pack", fromlist=["_same_snapshot"])._same_snapshot

        def race(root, target):
            if target["relative"] == ".cursor/hooks.json":
                path = root / target["relative"]
                path.parent.mkdir(exist_ok=True)
                path.write_text("{\"version\": 1, \"hooks\": {}}", encoding="utf-8")
                return False
            return original(root, target)

        with mock.patch("exitzero.pack._same_snapshot", side_effect=race):
            with self.assertRaises(RuntimeError):
                compile_pack(self.root, self.policy_path, policy, apply=True)
        self.assertFalse((self.root / "AGENTS.md").exists())

    def test_concurrent_edit_between_publications_is_not_overwritten(self):
        policy = self.policy(adapters=("cursor",))
        import exitzero.pack as pack
        real_write = pack.write_atomic

        def writer(path, content):
            result = real_write(path, content)
            if path.name == "AGENTS.md":
                cursor = self.root / ".cursor/hooks.json"
                cursor.parent.mkdir(exist_ok=True)
                cursor.write_text("concurrent", encoding="utf-8")
            return result

        with mock.patch("exitzero.pack.write_atomic", side_effect=writer):
            with self.assertRaises(RuntimeError):
                compile_pack(self.root, self.policy_path, policy, apply=True)
        self.assertFalse((self.root / "AGENTS.md").exists())
        self.assertEqual((self.root / ".cursor/hooks.json").read_text(), "concurrent")

    def test_write_failure_rolls_back_prior_publications(self):
        policy = self.policy()
        import exitzero.pack as pack
        real_write = pack.write_atomic

        def fail_settings(path, content):
            if path.name == "settings.json":
                raise OSError("simulated publication failure")
            return real_write(path, content)

        with mock.patch("exitzero.pack.write_atomic", side_effect=fail_settings):
            with self.assertRaises(RuntimeError):
                compile_pack(self.root, self.policy_path, policy, apply=True)
        self.assertFalse((self.root / "AGENTS.md").exists())
        self.assertFalse((self.root / ".cursor/hooks.json").exists())

    def test_permission_baseline_is_pinned_and_required(self):
        subprocess.run(["git", "-C", str(self.root), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(self.root), "add", "exitzero.toml"], check=True)
        env = {**os.environ, "GIT_AUTHOR_NAME": "Pack Test", "GIT_AUTHOR_EMAIL": "pack@example.invalid",
               "GIT_COMMITTER_NAME": "Pack Test", "GIT_COMMITTER_EMAIL": "pack@example.invalid"}
        subprocess.run(["git", "-C", str(self.root), "commit", "-qm", "policy"], check=True, env=env)
        policy = self.policy(permissions={"editable": [], "protected": [], "immutable": []})
        with self.assertRaises(ValueError):
            compile_pack(self.root, self.policy_path, policy)
        result = compile_pack(self.root, self.policy_path, policy, apply=True, trust_base="HEAD")
        self.assertTrue(result["applied"])
        manifest = json.loads((self.root / ".exitzero/hooks.json").read_text())
        self.assertRegex(manifest["trust_base"], r"^[0-9a-f]{40,64}$")
        command = json.loads((self.root / ".cursor/hooks.json").read_text())["hooks"]["stop"][0]["command"]
        self.assertIn("--use-installed-authority", command)
        self.assertNotIn(manifest["trust_base"], command)


if __name__ == "__main__":
    unittest.main()
