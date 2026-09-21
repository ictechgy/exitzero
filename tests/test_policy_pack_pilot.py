"""Pilot safety/evidence regressions independent of local archived artifacts."""
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import pilot_policy_pack as pilot


class PolicyPackPilotTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.snapshot = self.root / "snapshot"
        self.snapshot.mkdir()
        originals = {f"source/file_{index}.py": f"value = {index}\n".encode() for index in range(55)}
        originals["AGENTS.md"] = b"# Original instructions\n"
        for name, raw in originals.items():
            path = self.snapshot / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
        self.manifest = {name: pilot.sha256(raw) for name, raw in originals.items()}
        self.manifest_path = self.root / "source-manifest.json"
        self.manifest_path.write_text(json.dumps(self.manifest), encoding="utf-8")
        pin = patch.object(pilot, "MANIFEST_SHA256", pilot.sha256(pilot.canonical_manifest(self.manifest)))
        pin.start()
        self.addCleanup(pin.stop)
        with (self.snapshot / "AGENTS.md").open("ab") as stream:
            stream.write(b"\n<!-- aidd-gate:begin -->\nGenerated suffix\n<!-- aidd-gate:end -->\n")

    def test_manifest_pin_and_strict_hashes_reject_changed_input(self):
        self.assertEqual(pilot.manifest_data(self.manifest_path), self.manifest)
        changed = dict(self.manifest)
        changed["source/file_0.py"] = "0" * 64
        self.manifest_path.write_text(json.dumps(changed), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "reviewed 56-file manifest"):
            pilot.manifest_data(self.manifest_path)
        changed["source/file_0.py"] = "A" * 64
        self.manifest_path.write_text(json.dumps(changed), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "strict SHA-256"):
            pilot.manifest_data(self.manifest_path)

    def test_manifest_traversal_rejected(self):
        self.manifest_path.write_text(json.dumps({"../outside.py": "0" * 64}), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Unsafe source-manifest path"):
            pilot.manifest_data(self.manifest_path)

    def test_manifest_and_source_symlinks_rejected(self):
        link = self.root / "manifest-link.json"
        link.symlink_to(self.manifest_path)
        with self.assertRaisesRegex(ValueError, "Symlink"):
            pilot.manifest_data(link)
        source = self.snapshot / "source/file_0.py"
        source.unlink()
        source.symlink_to(self.snapshot / "source/file_1.py")
        with self.assertRaisesRegex(ValueError, "symlink"):
            pilot.validate_archive(self.snapshot, self.manifest_path)

    def test_undeclared_sensitive_input_is_rejected_without_reading(self):
        sentinel = self.snapshot / ".env.private"
        sentinel.write_text("synthetic canary", encoding="utf-8")
        original_open = Path.open
        def guarded_open(path, *args, **kwargs):
            if path == sentinel:
                raise AssertionError("Undeclared input was read")
            return original_open(path, *args, **kwargs)
        with patch.object(Path, "open", guarded_open):
            with self.assertRaisesRegex(ValueError, "undeclared file"):
                pilot.validate_archive(self.snapshot, self.manifest_path)

    def test_agents_recovery_and_copy_match_original_hashes(self):
        manifest, before, after = pilot.validate_archive(self.snapshot, self.manifest_path)
        self.assertEqual(before, after)
        destination = self.root / "copy"
        pilot.copy_originals(self.snapshot, manifest, destination)
        self.assertEqual({name: pilot.sha256((destination / name).read_bytes()) for name in manifest}, manifest)
        self.assertEqual((destination / "AGENTS.md").read_bytes(), b"# Original instructions\n")

    def test_copy_revalidates_source_and_agents_prefix(self):
        source = self.snapshot / "source/file_0.py"
        source.write_text("value = 'tampered'\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            pilot.copy_originals(self.snapshot, self.manifest, self.root / "copy")
        agents = self.snapshot / "AGENTS.md"
        agents.write_text("Changed instructions\n<!-- aidd-gate:begin -->\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            pilot.original_bytes(agents, "AGENTS.md", expected=self.manifest["AGENTS.md"])

    def test_copy_and_artifact_parent_symlinks_do_not_write_outside(self):
        outside = self.root / "outside"
        outside.mkdir()
        alias = self.root / "alias"
        alias.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "Symlink"):
            pilot.copy_originals(self.snapshot, self.manifest, alias / "case")
        checkout = self.root / "checkout"
        checkout.mkdir()
        (checkout / ".exitzero").symlink_to(outside, target_is_directory=True)
        with patch.object(pilot, "ROOT", checkout):
            with self.assertRaisesRegex(ValueError, "Symlink"):
                pilot.make_artifact()
        self.assertEqual(list(outside.iterdir()), [])

    def test_environment_filters_injection_and_preserves_home(self):
        inherited = {"HOME": str(self.root), "CODEX_HOME": str(self.root / "codex"),
                     "PATH": os.defpath, "PYTHONPATH": "untrusted", "GIT_CONFIG_COUNT": "1",
                     "PILOT_SYNTHETIC_TOKEN": "synthetic", "PIP_INDEX_URL": "https://invalid.example"}
        with patch.dict(os.environ, inherited, clear=True):
            env = pilot.isolated_env(self.root / "venv")
        self.assertEqual(env["HOME"], inherited["HOME"])
        self.assertEqual(env["CODEX_HOME"], inherited["CODEX_HOME"])
        self.assertEqual(env["PYTHONNOUSERSITE"], "1")
        self.assertEqual(env["GIT_CONFIG_GLOBAL"], os.devnull)
        self.assertEqual(env["PIP_NO_INDEX"], "1")
        self.assertFalse(set(env) & {"PYTHONPATH", "GIT_CONFIG_COUNT", "PILOT_SYNTHETIC_TOKEN", "PIP_INDEX_URL"})

    def test_upstream_evidence_rejects_wrong_skip_ids_and_command_exits(self):
        selection = {"tests_run": 214, "failures": 0, "errors": 0,
                     "skipped": [{"test": name, "reason": "declared"} for name in pilot.EXPECTED_SKIP_IDS]}
        results = {"tests": {"exit_code": 0, "selection": selection},
                   "policy-lint": {"exit_code": 0}, "policy-cases": {"exit_code": 0}}
        self.assertTrue(pilot.upstream_ok(results))
        selection["tests_run"] = 215
        self.assertFalse(pilot.upstream_ok(results))
        selection["tests_run"] = 214
        selection["errors"] = 1
        self.assertFalse(pilot.upstream_ok(results))
        selection["errors"] = 0
        selection["skipped"][0]["test"] = "unexpected.skipped.test"
        self.assertFalse(pilot.upstream_ok(results))

        selection["skipped"][0]["test"] = pilot.EXPECTED_SKIP_IDS[0]
        for name in results:
            results[name]["exit_code"] = 2
            self.assertFalse(pilot.upstream_ok(results), name)
            results[name]["exit_code"] = 0
        selection["failures"] = 1
        results["tests"]["exit_code"] = 1
        self.assertTrue(pilot.upstream_ok(results, failing_tests=True))
        self.assertFalse(pilot.upstream_ok(results))

    def test_timeout_retains_partial_output_and_nonzero_exit(self):
        log = self.root / "timeout.log"
        failure = subprocess.TimeoutExpired(["synthetic-command"], 1, output=b"partial\n", stderr=b"diagnostic\n")
        with patch.object(pilot.subprocess, "run", side_effect=failure):
            result = pilot.run_process(None, ["synthetic-command"], log, {}, timeout=1)
        self.assertEqual(result["exit_code"], 124)
        self.assertTrue(result["timed_out"])
        self.assertEqual(log.read_text(), "partial\ndiagnostic\n\n[timeout]\n")

    def test_runtime_mismatch_cannot_certify_source(self):
        wheel = {f"exitzero/module_{i}.py": pilot.sha256(str(i).encode()) for i in range(25)}
        pilot.verify_runtime_hashes(wheel, dict(wheel), dict(wheel))
        changed = {**wheel, "exitzero/module_0.py": pilot.sha256(b"changed")}
        for installed, source in ((wheel, changed), (changed, wheel), (wheel, {})):
            with self.assertRaisesRegex(RuntimeError, "must all match"):
                pilot.verify_runtime_hashes(wheel, installed, source)


if __name__ == "__main__":
    unittest.main()
