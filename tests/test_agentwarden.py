"""Focused contract tests for the optional AgentWarden verifier."""

import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import textwrap
import time
import unittest

from exitzero.api import CheckSpec, Context
from exitzero.runner import run
from exitzero.services import sha256_file
from exitzero_verify.agentwarden import (agentwarden_inputs, check_agentwarden,
                                         inspect_setup)


FAKE = textwrap.dedent('''\
    #!/usr/bin/env python3
    import hashlib
    import json
    import os
    import sys
    import time
    from pathlib import Path

    Path("invoked.marker").write_text("ran", encoding="utf-8")
    mode_path = Path("mode")
    mode = mode_path.read_text(encoding="utf-8").strip() if mode_path.exists() else "pass"
    if "--version" in sys.argv:
        print(Path("version").read_text(encoding="utf-8").strip() if Path("version").exists()
              else "agentwarden v0.3.2")
        raise SystemExit(0)
    if mode == "timeout":
        time.sleep(2)
    if mode == "orphan" and "scan" in sys.argv:
        child = os.fork()
        if child == 0:
            time.sleep(10)
            raise SystemExit(0)
        raise SystemExit(0)
    if mode == "flood" and "scan" in sys.argv:
        os.write(sys.stdout.fileno(), b"x" * (2 * 1024 * 1024))
        os.write(sys.stderr.fileno(), b"y" * (2 * 1024 * 1024))
        raise SystemExit(0)
    if mode == "malformed":
        print("secret-token-that-must-not-escape")
        raise SystemExit(0)
    if "audit" in sys.argv:
        lock = json.loads(Path("skills.lock").read_text(encoding="utf-8"))
        passed = mode != "risk"
        skills = {}
        for name, item in lock["skills"].items():
            skills[name] = {
                "version": item["version"], "source": item["source"],
                "sha256": item["sha256"], "exists": passed,
                "hashMatch": passed, "policyPassed": passed,
                "publisherPolicyPassed": passed,
            }
        print(json.dumps({"auditedAt": "now", "passed": passed, "skills": skills}))
        raise SystemExit(0 if passed else 1)
    target = str(Path("skill.md").resolve())
    passed = mode != "risk"
    result = {
        "filePath": target, "parsedSkill": {},
        "findings": [] if passed else [{"description": "secret-token-that-must-not-escape"}],
        "score": 100 if passed else 0, "passed": passed, "sha256": "0" * 64,
    }
    print(json.dumps(result))
    raise SystemExit(0 if passed else 1)
''')


@unittest.skipUnless(os.name == "posix", "AgentWarden execution requires POSIX pipe controls")
class AgentWardenTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.context = Context(self.root, {}, self.root / "exitzero.toml")
        self.config = self.root / "warden.json"
        self.config.write_text("{}\n", encoding="utf-8")
        self.fake = self.root / "fake-agentwarden"
        self.fake.write_text(FAKE, encoding="utf-8")
        self.fake.chmod(self.fake.stat().st_mode | stat.S_IXUSR)

    def options(self, *, timeout=5, version="0.3.2", config="warden.json", tool_paths=None):
        return {"argv": [str(self.fake)], "config": config,
                "version": version, "timeout": timeout,
                **({"tool_paths": tool_paths} if tool_paths is not None else {})}

    def audit_spec(self, **options):
        values = self.options(**options)
        return CheckSpec("warden", "agentwarden.audit", (), values, reuse=False)

    def scan_spec(self, **options):
        values = self.options(**options)
        return CheckSpec("warden", "agentwarden.scan", ("skill.md",), values, reuse=False)

    def write_lock(self, source="skill.md"):
        digest = hashlib.sha256((self.root / source).read_bytes()).hexdigest()
        lock = {
            "lockfileVersion": 1,
            "skills": {
                "demo": {
                    "name": "demo", "version": "1.0.0", "source": source,
                    "sha256": digest, "installedAt": "now", "verifiedScore": 100,
                }
            },
        }
        (self.root / "skills.lock").write_text(json.dumps(lock), encoding="utf-8")

    def test_setup_diagnosis_never_executes_and_audit_passes_or_fails_generically(self):
        (self.root / "skill.md").write_text("# Demo\n", encoding="utf-8")
        self.write_lock()
        state = inspect_setup(self.context, self.audit_spec())
        self.assertEqual(state[0], "configured")
        self.assertFalse((self.root / "invoked.marker").exists())
        self.assertEqual(check_agentwarden(self.context, self.audit_spec()), [])
        self.assertTrue((self.root / "invoked.marker").exists())

        (self.root / "mode").write_text("risk", encoding="utf-8")
        findings = check_agentwarden(self.context, self.audit_spec())
        self.assertEqual(len(findings), 1)
        self.assertNotIn("demo", findings[0].message)
        self.assertNotIn("secret", findings[0].message)

    def test_scan_pass_fail_and_malformed_output_are_bounded(self):
        (self.root / "skill.md").write_text("# Demo\n", encoding="utf-8")
        self.assertEqual(agentwarden_inputs(self.context, self.scan_spec()),
                         ["warden.json", "skill.md"])
        self.assertEqual(check_agentwarden(self.context, self.scan_spec()), [])
        (self.root / "mode").write_text("risk", encoding="utf-8")
        findings = check_agentwarden(self.context, self.scan_spec())
        self.assertEqual(len(findings), 1)
        self.assertNotIn("secret-token", findings[0].message)
        (self.root / "mode").write_text("malformed", encoding="utf-8")
        with self.assertRaises(ValueError):
            check_agentwarden(self.context, self.scan_spec())

    def test_unknown_version_and_timeout_are_operational_errors(self):
        (self.root / "skill.md").write_text("# Demo\n", encoding="utf-8")
        (self.root / "version").write_text("agentwarden v9.9.9", encoding="utf-8")
        with self.assertRaises(ValueError):
            check_agentwarden(self.context, self.scan_spec())
        (self.root / "version").unlink()
        (self.root / "mode").write_text("timeout", encoding="utf-8")
        with self.assertRaises(ValueError):
            check_agentwarden(self.context, self.scan_spec(timeout=0.05))

    def test_inherited_pipes_and_output_flood_are_bounded(self):
        (self.root / "skill.md").write_text("# Demo\n", encoding="utf-8")
        (self.root / "mode").write_text("orphan", encoding="utf-8")
        started = time.monotonic()
        with self.assertRaises(ValueError):
            check_agentwarden(self.context, self.scan_spec(timeout=0.2))
        self.assertLess(time.monotonic() - started, 2.0)
        (self.root / "mode").write_text("flood", encoding="utf-8")
        started = time.monotonic()
        with self.assertRaises(ValueError):
            check_agentwarden(self.context, self.scan_spec(timeout=5))
        self.assertLess(time.monotonic() - started, 2.0)

    def test_missing_empty_and_malformed_lock_fail_closed(self):
        (self.root / "skill.md").write_text("# Demo\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            agentwarden_inputs(self.context, self.audit_spec())
        (self.root / "skills.lock").write_text(json.dumps({"lockfileVersion": 1, "skills": {}}), encoding="utf-8")
        self.assertEqual(inspect_setup(self.context, self.audit_spec())[0], "misconfigured")
        (self.root / "skills.lock").write_text("not-json", encoding="utf-8")
        with self.assertRaises(ValueError):
            agentwarden_inputs(self.context, self.audit_spec())

    def test_config_baseline_is_an_input_and_unsafe_paths_are_rejected(self):
        (self.root / "skill.md").write_text("# Demo\n", encoding="utf-8")
        self.write_lock()
        (self.root / "baseline.json").write_text("{}\n", encoding="utf-8")
        self.config.write_text(json.dumps({"baseline": "baseline.json"}), encoding="utf-8")
        self.assertEqual(agentwarden_inputs(self.context, self.audit_spec()),
                         ["warden.json", "baseline.json", "skills.lock", "skill.md"])
        self.config.write_text(json.dumps({"extends": "outside.json"}), encoding="utf-8")
        with self.assertRaises(ValueError):
            agentwarden_inputs(self.context, self.audit_spec())
        self.config.write_text("{}", encoding="utf-8")
        with self.assertRaises(ValueError):
            agentwarden_inputs(self.context, self.audit_spec(config="../secret.json"))
        with self.assertRaises(ValueError):
            agentwarden_inputs(self.context, self.audit_spec(config=".env"))
        (self.root / "warden[1].json").write_text("{}", encoding="utf-8")
        with self.assertRaises(ValueError):
            agentwarden_inputs(self.context, self.audit_spec(config="warden[1].json"))
        with self.assertRaises(ValueError):
            agentwarden_inputs(self.context, self.audit_spec(tool_paths=["node_modules/**/*"]))
        outside = self.root.parent / "warden-outside.json"
        outside.write_text("{}", encoding="utf-8")
        self.addCleanup(outside.unlink)
        self.config.unlink()
        self.config.symlink_to(outside)
        with self.assertRaises(ValueError):
            agentwarden_inputs(self.context, self.audit_spec())

    def test_tool_paths_are_fingerprinted_but_not_scan_targets(self):
        (self.root / "skill.md").write_text("# Demo\n", encoding="utf-8")
        runtime = self.root / "tools/agentwarden/src/cli.ts"
        runtime.parent.mkdir(parents=True)
        runtime.write_text("export const version = '0.3.2';\n", encoding="utf-8")
        spec = self.scan_spec(tool_paths=["tools/agentwarden/**/*"])
        self.assertEqual(agentwarden_inputs(self.context, spec),
                         ["warden.json", "tools/agentwarden/**/*", "skill.md"])
        before = sha256_file(runtime)
        self.assertEqual(check_agentwarden(self.context, spec), [])
        runtime.write_text("export const version = 'changed';\n", encoding="utf-8")
        self.assertNotEqual(before, sha256_file(runtime))
        self.assertEqual(agentwarden_inputs(self.context, spec),
                         ["warden.json", "tools/agentwarden/**/*", "skill.md"])

    def test_doctor_persists_structured_setup_diagnostics_without_execution(self):
        policy = self.root / "exitzero.toml"

        def write_policy(argv, config="warden.json"):
            policy.write_text(
                "version = 1\n"
                "plugins = [\"exitzero_verify\", \"exitzero_harness\"]\n\n"
                "[[checks]]\n"
                "id = \"warden\"\n"
                "kind = \"agentwarden.audit\"\n"
                "reuse = false\n"
                "paths = []\n\n"
                "[checks.options]\n"
                f"argv = {json.dumps(argv)}\n"
                f"config = {json.dumps(config)}\n"
                "version = \"0.3.2\"\n",
                encoding="utf-8",
            )

        def doctor_row():
            receipt = run(self.root, "exitzero.toml", "doctor")
            self.assertEqual(receipt["command"], "doctor")
            persisted = json.loads((self.root / receipt["receipt"]).read_text(encoding="utf-8"))
            self.assertEqual(persisted, receipt)
            return receipt, next(item for item in receipt["diagnostics"] if item["target"] == "check.warden")

        write_policy([str(self.root / "missing-agentwarden")])
        (self.root / "warden.json").write_text("{}", encoding="utf-8")
        receipt, row = doctor_row()
        self.assertEqual(row["state"], "missing")
        self.assertFalse((self.root / "invoked.marker").exists())
        self.assertTrue(receipt["receipt"])

        write_policy([str(self.fake)], config="missing.json")
        receipt, row = doctor_row()
        self.assertEqual(row["state"], "missing")
        self.assertFalse((self.root / "invoked.marker").exists())

        write_policy([str(self.fake)])
        (self.root / "skills.lock").write_text(
            json.dumps({"lockfileVersion": 1, "skills": {}}), encoding="utf-8")
        receipt, row = doctor_row()
        self.assertEqual(row["state"], "misconfigured")
        self.assertFalse((self.root / "invoked.marker").exists())

    def test_duplicate_config_and_lock_members_are_rejected(self):
        (self.root / "skill.md").write_text("# Demo\n", encoding="utf-8")
        self.write_lock()
        self.config.write_text('{"baseline":"a.json","baseline":"b.json"}', encoding="utf-8")
        with self.assertRaises(ValueError):
            agentwarden_inputs(self.context, self.audit_spec())
        self.config.write_text("{}", encoding="utf-8")
        (self.root / "skills.lock").write_text(
            '{"lockfileVersion":1,"lockfileVersion":1,"skills":{}}', encoding="utf-8")
        with self.assertRaises(ValueError):
            agentwarden_inputs(self.context, self.audit_spec())

    def test_package_sources_include_complete_directory_and_reject_escape(self):
        package = self.root / "skills/pkg"
        package.mkdir(parents=True)
        (package / "SKILL.md").write_text("# Demo\n", encoding="utf-8")
        (package / "script.sh").write_text("echo ok\n", encoding="utf-8")
        manifest = []
        for relative in ("SKILL.md", "script.sh"):
            data = (package / relative).read_bytes()
            manifest.append({"path": relative, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)})
        lock = {"lockfileVersion": 1, "skills": {"demo": {
            "name": "demo", "version": "1", "source": "skills/pkg/SKILL.md", "sha256": "0" * 64,
            "installedAt": "now", "verifiedScore": 100, "packageFormat": "tar.gz",
            "packageSha256": "1" * 64, "packageEntry": "SKILL.md", "packageFiles": manifest,
        }}}
        (self.root / "skills.lock").write_text(json.dumps(lock), encoding="utf-8")
        inputs = agentwarden_inputs(self.context, self.audit_spec())
        self.assertIn("skills/pkg/**/*", inputs)
        self.assertIn("skills/pkg/SKILL.md", inputs)
        lock["skills"]["demo"]["packageEntry"] = "../SKILL.md"
        (self.root / "skills.lock").write_text(json.dumps(lock), encoding="utf-8")
        with self.assertRaises(ValueError):
            agentwarden_inputs(self.context, self.audit_spec())


if __name__ == "__main__":
    unittest.main()
