"""Black-box contracts for the CLI, policy, receipts and extension boundary."""
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
CLI = REPO / "bin" / "exitzero"

sys.path.insert(0, str(REPO / "packages" / "core" / "src"))
sys.path.insert(0, str(REPO / "packages" / "plugin-verify" / "src"))

from exitzero import cli as cli_module  # noqa: E402
from exitzero.files import _match_parts, select_files  # noqa: E402
from exitzero.runner import run  # noqa: E402


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

    def test_requirement_cli_records_actual_outcome(self):
        self.init()
        policy = self.root / "exitzero.toml"
        policy.write_text(policy.read_text() + '\n[[requirements]]\nid = "completion"\n'
                          'description = "private_description_marker"\nchecks = ["syntax"]\n')
        self.assertEqual(self.cli("init", "--sync").returncode, 0)
        result = self.cli("check", "--format", "json")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        receipt = json.loads(result.stdout)
        self.assertEqual(receipt["requirements"], [
            {"id": "completion", "checks": ["syntax"], "status": "checks_passed"}])
        self.assertEqual(json.loads((self.root / receipt["receipt"]).read_text()), receipt)
        self.assertNotIn("private_description_marker", result.stdout)

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

    def test_sarif_format_maps_findings_to_results(self):
        self.init()
        (self.root / "broken.py").write_text("def broken(:\n", encoding="utf-8")
        result = self.cli("check", "--format", "sarif")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        sarif = json.loads(result.stdout)
        self.assertEqual(sarif["version"], "2.1.0")
        run = sarif["runs"][0]
        self.assertEqual(run["tool"]["driver"]["name"], "exitzero")
        self.assertEqual(len(run["results"]), 1)
        finding = run["results"][0]
        self.assertEqual(finding["ruleId"], "syntax")
        self.assertEqual(finding["level"], "error")
        location = finding["locations"][0]["physicalLocation"]
        self.assertEqual(location["artifactLocation"]["uri"], "broken.py")
        self.assertEqual(location["region"]["startLine"], 1)
        self.assertEqual([rule["id"] for rule in run["tool"]["driver"]["rules"]], ["syntax"])

    def test_sarif_encodes_uris_and_report_reads_latest(self):
        self.init()
        (self.root / "a#b.py").write_text("def broken(:\n", encoding="utf-8")
        result = self.cli("check", "--format", "sarif")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        result_payload = json.loads(result.stdout)["runs"][0]["results"][0]
        uri = result_payload["locations"][0]["physicalLocation"]["artifactLocation"]["uri"]
        self.assertEqual(uri, "a%23b.py")
        report = self.cli("report", "--format", "sarif")
        self.assertEqual(report.returncode, 0, report.stdout + report.stderr)
        reported = json.loads(report.stdout)["runs"][0]["results"][0]
        self.assertEqual(reported["ruleId"], "syntax")

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

    def test_import_dependency_hashes_and_membership_through_normal_gate(self):
        (self.root / "src").mkdir()
        (self.root / "dependencies").mkdir()
        (self.root / "src/main.py").write_text("from dependency import value\n", encoding="utf-8")
        dependency = self.root / "dependencies/dependency.py"
        unreferenced = self.root / "dependencies/unreferenced.py"
        unreferenced.write_text("other = 3\n", encoding="utf-8")
        added = self.root / "dependencies/added.py"
        for operation in ("stable", "mutated", "added", "deleted"):
            with self.subTest(operation=operation):
                dependency.write_text("value = 1\n", encoding="utf-8")
                added.unlink(missing_ok=True)
                expected_hash = hashlib.sha256(dependency.read_bytes()).hexdigest()
                actions = {
                    "stable": "assert Path('dependencies/dependency.py').read_text() == 'value = 1\\n'",
                    "mutated": "Path('dependencies/dependency.py').write_text('value = 2\\n')",
                    "added": "Path('dependencies/added.py').write_text('added = 1\\n')",
                    "deleted": "Path('dependencies/dependency.py').unlink()",
                }
                argv = ["{python}", "-c", "from pathlib import Path; " + actions[operation]]
                (self.root / "exitzero.toml").write_text(
                    'version = 1\nplugins = ["exitzero_verify"]\n'
                    '[[checks]]\nid = "imports"\nkind = "python.imports"\n'
                    'paths = ["src/main.py"]\n[checks.options]\nroots = ["src", "dependencies"]\n'
                    '[[checks]]\nid = "action"\nkind = "command"\n'
                    '[checks.options]\nargv = ' + json.dumps(argv) + '\n', encoding="utf-8",
                )
                result = self.cli("check", "--format", "json")
                self.assertEqual(result.returncode, 0 if operation == "stable" else 1, result.stdout + result.stderr)
                receipt = json.loads(result.stdout)
                self.assertEqual(json.loads((self.root / receipt["receipt"]).read_text()), receipt)
                self.assertEqual(receipt["inputs"]["dependencies/dependency.py"], expected_hash)
                self.assertEqual(receipt["inputs"]["dependencies/unreferenced.py"],
                                 hashlib.sha256(unreferenced.read_bytes()).hexdigest())
                self.assertNotIn("dependencies/added.py", receipt["inputs"])
                self.assertEqual([check["status"] for check in receipt["checks"]], ["passed", "passed"])
                self.assertEqual([finding["rule"] for finding in receipt["findings"]],
                                 [] if operation == "stable" else ["core.inputs-changed"])

    def test_import_default_root_fingerprints_dependencies_outside_paths(self):
        (self.root / "main.py").write_text("from dependency import value\n", encoding="utf-8")
        dependency = self.root / "dependency.py"
        dependency.write_text("value = 1\n", encoding="utf-8")
        (self.root / "exitzero.toml").write_text(
            'version = 1\nplugins = ["exitzero_verify"]\n[[checks]]\n'
            'id = "imports"\nkind = "python.imports"\npaths = ["main.py"]\n', encoding="utf-8",
        )
        result = self.cli("check", "--format", "json")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        receipt = json.loads(result.stdout)
        self.assertEqual(receipt["inputs"]["dependency.py"], hashlib.sha256(dependency.read_bytes()).hexdigest())
        self.assertEqual(json.loads((self.root / receipt["receipt"]).read_text()), receipt)

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


class ReuseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def cli(self, *args):
        return subprocess.run([sys.executable, str(CLI), "--root", str(self.root), *args],
                              capture_output=True, text=True, timeout=30)

    def write_policy(self, suffix=""):
        (self.root / "exitzero.toml").write_text(
            'version = 1\nplugins = ["exitzero_verify"]\n'
            '[[checks]]\nid = "dir_a"\nkind = "python.syntax"\npaths = ["a/*.py"]\n'
            '[[checks]]\nid = "dir_b"\nkind = "python.syntax"\npaths = ["b/*.py"]\n'
            + suffix, encoding="utf-8")

    def setup_dirs(self):
        (self.root / "a").mkdir()
        (self.root / "b").mkdir()
        (self.root / "a/x.py").write_text("x = 1\n", encoding="utf-8")
        (self.root / "b/y.py").write_text("y = 1\n", encoding="utf-8")
        self.write_policy()

    def check_payload(self, *extra, expected=0):
        result = self.cli("check", *extra, "--format", "json")
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def statuses(self, payload):
        return {check["id"]: check["status"] for check in payload["checks"]}

    def test_reuse_records_inputs_and_reuses_unchanged_passes(self):
        self.setup_dirs()
        first = self.check_payload()
        self.assertEqual(self.statuses(first), {"dir_a": "passed", "dir_b": "passed"})
        for check in first["checks"]:
            self.assertEqual(check["input_files"],
                             [f"{check['id'][-1]}/{'x' if check['id'] == 'dir_a' else 'y'}.py"])
        second = self.check_payload("--reuse")
        self.assertEqual(self.statuses(second), {"dir_a": "reused", "dir_b": "reused"})
        self.assertEqual([check["reused_from"] for check in second["checks"]],
                         [first["run_id"], first["run_id"]])
        for check in second["checks"]:
            self.assertEqual(check["finding_count"], 0)
            self.assertEqual(check["input_files"], [f"{check['id'][-1]}/{'x' if check['id'] == 'dir_a' else 'y'}.py"])

    def test_reuse_reruns_only_the_changed_check(self):
        self.setup_dirs()
        self.check_payload()
        (self.root / "a/x.py").write_text("x = 2\n", encoding="utf-8")
        payload = self.check_payload("--reuse")
        self.assertEqual(self.statuses(payload), {"dir_a": "passed", "dir_b": "reused"})

    def test_reuse_detects_added_and_deleted_inputs(self):
        self.setup_dirs()
        self.check_payload()
        (self.root / "a/new.py").write_text("n = 1\n", encoding="utf-8")
        self.assertEqual(self.statuses(self.check_payload("--reuse")),
                         {"dir_a": "passed", "dir_b": "reused"})
        # A changed selection re-runs the check instead of reusing.
        (self.root / "b/extra.py").write_text("e = 1\n", encoding="utf-8")
        payload = self.check_payload("--reuse")
        self.assertEqual(self.statuses(payload), {"dir_a": "reused", "dir_b": "passed"})
        # Back to the original selection, the older passing receipt is evidence again.
        (self.root / "b/extra.py").unlink()
        payload = self.check_payload("--reuse")
        self.assertEqual(self.statuses(payload), {"dir_a": "reused", "dir_b": "reused"})
        # An emptied selection can never prove stable inputs; it errors, not reuses.
        (self.root / "b/y.py").unlink()
        payload = self.check_payload("--reuse", expected=2)
        self.assertEqual(self.statuses(payload), {"dir_a": "reused"})

    def test_reuse_never_reuses_failed_checks(self):
        self.setup_dirs()
        (self.root / "a/bad.py").write_text("def broken(:\n", encoding="utf-8")
        self.check_payload(expected=1)
        payload = self.check_payload("--reuse", expected=1)
        self.assertEqual(self.statuses(payload), {"dir_a": "failed", "dir_b": "reused"})

    def test_reuse_reruns_after_policy_change_and_empty_history(self):
        self.setup_dirs()
        first = self.check_payload("--reuse")
        self.assertEqual(self.statuses(first), {"dir_a": "passed", "dir_b": "passed"})
        self.assertNotIn("reused_from", first["checks"][0])
        policy = self.root / "exitzero.toml"
        policy.write_text(policy.read_text() + "\n# comment drift\n", encoding="utf-8")
        self.assertEqual(self.statuses(self.check_payload("--reuse")),
                         {"dir_a": "passed", "dir_b": "passed"})

    def test_reuse_satisfies_requirement_evidence(self):
        self.setup_dirs()
        self.write_policy('\n[[requirements]]\nid = "done"\ndescription = "Done"\n'
                          'checks = ["dir_a", "dir_b"]\n')
        self.check_payload()
        payload = self.check_payload("--reuse")
        self.assertEqual(payload["requirements"][0]["status"], "checks_passed")

    def test_reuse_human_output_lists_reused_checks(self):
        self.setup_dirs()
        self.assertEqual(self.cli("check").returncode, 0)
        result = self.cli("check", "--reuse")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("Reused passing evidence", result.stdout)

    def runs_dir(self):
        directory = self.root / ".exitzero" / "runs"
        self.assertTrue(directory.is_dir())
        return directory

    def craft_only_receipt(self, mutate):
        for stale in self.runs_dir().glob("*.json"):
            stale.unlink()
        self.check_payload()
        newest = max(self.runs_dir().glob("*.json"),
                     key=lambda path: path.stat().st_mtime_ns)
        receipt = json.loads(newest.read_text(encoding="utf-8"))
        for stale in self.runs_dir().glob("*.json"):
            stale.unlink()
        mutate(receipt)
        newest.write_text(json.dumps(receipt), encoding="utf-8")

    def test_reuse_multi_file_inputs(self):
        self.setup_dirs()
        (self.root / "a/extra.py").write_text("e = 1\n", encoding="utf-8")
        first = self.check_payload()
        entry = next(c for c in first["checks"] if c["id"] == "dir_a")
        self.assertEqual(entry["input_files"], ["a/extra.py", "a/x.py"])
        payload = self.check_payload("--reuse")
        self.assertEqual(self.statuses(payload), {"dir_a": "reused", "dir_b": "reused"})

    def test_reuse_skips_malformed_prior_receipts(self):
        self.setup_dirs()
        self.check_payload()
        self.craft_only_receipt(lambda receipt: receipt.pop("run_id"))
        payload = self.check_payload("--reuse")
        self.assertEqual(self.statuses(payload), {"dir_a": "passed", "dir_b": "passed"})
        self.craft_only_receipt(lambda receipt: receipt.update(checks=None))
        payload = self.check_payload("--reuse")
        self.assertEqual(self.statuses(payload), {"dir_a": "passed", "dir_b": "passed"})

    def test_reuse_rejects_tainted_or_mismatched_receipts(self):
        self.setup_dirs()
        self.check_payload()
        cases = [
            lambda receipt: receipt.update(tool_version="0.0.0-other"),
            lambda receipt: receipt["findings"].append(
                {"rule": "core.inputs-changed", "message": "x", "path": None,
                 "line": None, "severity": "warning"}),
            lambda receipt: [entry.update(finding_count=1) for entry in receipt["checks"]],
        ]
        for mutate in cases:
            self.craft_only_receipt(mutate)
            payload = self.check_payload("--reuse")
            self.assertEqual(self.statuses(payload), {"dir_a": "passed", "dir_b": "passed"})

    def test_reuse_hook_slot_runs_checks_fully(self):
        self.setup_dirs()
        self.check_payload()
        receipt = run(self.root, "exitzero.toml", "check", slot="CI", reuse=True)
        self.assertNotIn("reused", {check["status"] for check in receipt["checks"]})


class InputDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "exitzero.toml").write_text(
            'version = 1\nplugins = ["example"]\n[[checks]]\n'
            'id = "example"\nkind = "example"\npaths = ["main.py"]\n',
            encoding="utf-8",
        )
        (self.root / "main.py").write_text("value = 1\n", encoding="utf-8")

    def gate(self, registry):
        with mock.patch("exitzero.runner.discover", return_value=registry):
            receipt = run(self.root, "exitzero.toml", "check")
        saved = json.loads((self.root / receipt["receipt"]).read_text())
        self.assertEqual(saved, receipt)
        return receipt

    def test_registration_is_optional_keyword_only_and_atomic(self):
        from exitzero.api import Registry

        registry = Registry()
        handler = mock.Mock(return_value=[])
        provider = mock.Mock(return_value=[])
        registry.add_check("legacy", handler)
        self.assertIs(registry.checks["legacy"], handler)
        self.assertEqual(registry.check_inputs, {})
        registry.add_check("example", handler, inputs=provider)
        for name, check, inputs in (("", handler, provider), ([], handler, provider),
                                    (1, handler, provider), ("bad", None, provider),
                                    ("bad", handler, []), ("example", handler, provider)):
            with self.subTest(name=name, check=check, inputs=inputs):
                with self.assertRaises(ValueError):
                    registry.add_check(name, check, inputs=inputs)
                self.assertEqual(registry.checks, {"legacy": handler, "example": handler})
                self.assertEqual(registry.check_inputs, {"example": provider})
        with self.assertRaises(TypeError):
            registry.add_check("positional", handler, provider)

    def test_discovery_accepts_list_tuple_and_empty_patterns_in_both_snapshots(self):
        from exitzero.api import Registry

        dependency = self.root / "dependency.py"
        dependency.write_text("value = 2\n", encoding="utf-8")
        for patterns in (["dependency.py"], ("dependency.py",), [], ()):
            with self.subTest(patterns=patterns):
                registry = Registry()
                provider = mock.Mock(return_value=patterns)
                registry.add_check("example", mock.Mock(return_value=[]), inputs=provider)
                receipt = self.gate(registry)
                self.assertEqual(receipt["exit_code"], 0, receipt)
                self.assertEqual(provider.call_count, 2)
                context, spec = provider.call_args.args
                self.assertEqual(context.root, self.root)
                self.assertEqual(spec.paths, ("main.py",))
                self.assertIn("main.py", receipt["inputs"])
                self.assertEqual("dependency.py" in receipt["inputs"], bool(patterns))

    def test_invalid_discovery_results_are_operational_errors_in_either_snapshot(self):
        from exitzero.api import Registry

        for invalid in (None, "*.py", {"*.py"}, {"paths": []}, 1, [1], [""], [Path("main.py")]):
            for snapshot in (1, 2):
                with self.subTest(invalid=invalid, snapshot=snapshot):
                    registry = Registry()
                    handler = mock.Mock(return_value=[])
                    provider = mock.Mock(side_effect=[invalid] if snapshot == 1 else [[], invalid])
                    registry.add_check("example", handler, inputs=provider)
                    receipt = self.gate(registry)
                    self.assertEqual(receipt["exit_code"], 2, receipt)
                    self.assertEqual([finding["rule"] for finding in receipt["findings"]], ["core.error"])
                    self.assertEqual(handler.call_count, snapshot - 1)
                    self.assertEqual(provider.call_count, snapshot)

    def test_discovery_exceptions_are_operational_errors(self):
        from exitzero.api import Registry

        registry = Registry()
        handler = mock.Mock(return_value=[])
        registry.add_check("example", handler, inputs=mock.Mock(side_effect=OSError("discovery unavailable")))
        receipt = self.gate(registry)
        self.assertEqual(receipt["exit_code"], 2, receipt)
        handler.assert_not_called()
        self.assertIn("OSError", receipt["findings"][0]["message"])

    def test_scandir_errors_propagate_and_fail_gate_discovery(self):
        from exitzero.api import Registry

        source = self.root / "dependencies"
        nested = source / "nested"
        nested.mkdir(parents=True)
        (nested / "module.py").write_text("value = 2\n", encoding="utf-8")
        original_scandir = os.scandir
        for blocked in (source, nested):
            with self.subTest(blocked=blocked.name):
                def scandir(path):
                    if Path(path) == blocked:
                        raise OSError("directory unavailable")
                    return original_scandir(path)

                registry = Registry()
                handler = mock.Mock(return_value=[])
                registry.add_check("example", handler, inputs=lambda context, spec: ["dependencies/**/*.py"])
                with mock.patch("exitzero.files.os.scandir", side_effect=scandir):
                    with self.assertRaisesRegex(OSError, "directory unavailable"):
                        select_files(self.root, ["dependencies/**/*.py"])
                    receipt = self.gate(registry)
                self.assertEqual(receipt["exit_code"], 2, receipt)
                self.assertIn("OSError", receipt["findings"][0]["message"])
                handler.assert_not_called()


class RequirementTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.policy_path = self.root / "exitzero.toml"
        self.base = 'version = 1\nplugins = ["exitzero_verify"]\n[[checks]]\nid = "syntax"\nkind = "python.syntax"\npaths = ["*.py"]\n'
        (self.root / "example.py").write_text("value = 1\n")

    def configure(self, checks='["syntax"]'):
        self.policy_path.write_text(self.base + '\n[[requirements]]\nid = "complete"\n'
                                    'description = "A verified result"\nchecks = ' + checks + '\n')

    def test_failed_and_unmapped_requirements_block_completion(self):
        self.configure()
        (self.root / "example.py").write_text("def broken(:\n")
        receipt = run(self.root, "exitzero.toml", "check")
        self.assertEqual(receipt["exit_code"], 1)
        self.assertEqual(receipt["requirements"][0]["status"], "failed")
        (self.root / "example.py").write_text("value = 1\n")
        self.configure("[]")
        receipt = run(self.root, "exitzero.toml", "check")
        self.assertEqual(receipt["exit_code"], 1)
        self.assertEqual(receipt["requirements"][0]["status"], "unverified")
        self.assertIn("core.requirement-unverified", [finding["rule"] for finding in receipt["findings"]])

    def test_lint_does_not_claim_verification(self):
        from exitzero.api import Registry
        self.configure("[]")
        registry = Registry()
        handler = mock.Mock(return_value=[])
        registry.add_check("python.syntax", handler)
        registry.add_linter("syntax", lambda context: [])
        with mock.patch("exitzero.runner.discover", return_value=registry):
            receipt = run(self.root, "exitzero.toml", "lint-config")
        self.assertEqual(receipt["exit_code"], 0)
        self.assertEqual(receipt["requirements"][0]["status"], "unverified")
        handler.assert_not_called()

    def test_invalid_requirement_contracts_fail_policy(self):
        invalid = ['checks = ["unknown"]', 'checks = ["syntax", "syntax"]',
                   'checks = "syntax"', 'checks = [1]', 'checks = []\nextra = true']
        for fields in invalid:
            with self.subTest(fields=fields):
                self.policy_path.write_text(self.base + '\n[[requirements]]\nid = "complete"\ndescription = "Result"\n' + fields)
                receipt = run(self.root, "exitzero.toml", "check")
                self.assertEqual(receipt["exit_code"], 2)
                self.assertTrue((self.root / receipt["receipt"]).is_file())

    def test_early_error_retains_unverified_requirement(self):
        self.configure()
        with mock.patch("exitzero.runner.discover", side_effect=ValueError("unavailable")):
            receipt = run(self.root, "exitzero.toml", "check")
        self.assertEqual(receipt["exit_code"], 2)
        self.assertEqual(receipt["requirements"][0]["status"], "unverified")

    def test_changed_inputs_and_missing_receipt_invalidate_pass(self):
        self.configure()
        with mock.patch("exitzero.runner._snapshot", side_effect=[{"example.py": "before"}, {"example.py": "after"}]):
            receipt = run(self.root, "exitzero.toml", "check")
        self.assertEqual(receipt["exit_code"], 1)
        self.assertEqual(receipt["requirements"][0]["status"], "unverified")
        with mock.patch("exitzero.runner.persist", side_effect=OSError("unavailable")):
            receipt = run(self.root, "exitzero.toml", "check")
        self.assertEqual(receipt["exit_code"], 2)
        self.assertIsNone(receipt["receipt"])
        self.assertEqual(receipt["requirements"][0]["status"], "unverified")

    def test_linter_id_cannot_supply_verification_result(self):
        from exitzero.api import Registry
        self.configure()
        registry = Registry()
        registry.add_linter("syntax", lambda context: [])
        registry.add_check("python.syntax", mock.Mock(side_effect=ValueError("unavailable")))
        with mock.patch("exitzero.runner.discover", return_value=registry):
            receipt = run(self.root, "exitzero.toml", "check")
        self.assertEqual(receipt["exit_code"], 2)
        self.assertEqual(receipt["requirements"][0]["status"], "unverified")


    def test_warning_only_mapped_check_stays_checks_passed(self):
        from exitzero.api import Finding, Registry
        self.configure()
        registry = Registry()
        registry.add_check("python.syntax", mock.Mock(side_effect=[
            [Finding("advice", "consider renaming", severity="warning")],
            [],
        ]))
        with mock.patch("exitzero.runner.discover", return_value=registry):
            warned = run(self.root, "exitzero.toml", "check")
            self.assertEqual(warned["exit_code"], 0)
            self.assertEqual(warned["requirements"][0]["status"], "checks_passed")
            clean = run(self.root, "exitzero.toml", "check")
        self.assertEqual(clean["exit_code"], 0)
        self.assertEqual(clean["requirements"][0]["status"], "checks_passed")

    def test_mixed_severity_mapped_check_fails_requirement(self):
        from exitzero.api import Finding, Registry
        self.configure()
        registry = Registry()
        registry.add_check("python.syntax", mock.Mock(return_value=[
            Finding("advice", "consider renaming", severity="warning"),
            Finding("broken", "cannot resolve symbol"),
        ]))
        with mock.patch("exitzero.runner.discover", return_value=registry):
            receipt = run(self.root, "exitzero.toml", "check")
        self.assertEqual(receipt["exit_code"], 1)
        self.assertEqual(receipt["requirements"][0]["status"], "failed")
        self.assertIn("core.requirement-failed", [finding["rule"] for finding in receipt["findings"]])


class SecurityRegressionTests(unittest.TestCase):
    """Review findings: special files, link write-through, terminal escaping."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def cli(self, *args):
        return subprocess.run([sys.executable, str(CLI), "--root", str(self.root), *args],
                              capture_output=True, text=True, timeout=30)

    def init(self):
        self.assertEqual(self.cli("init").returncode, 0)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFOs need POSIX mkfifo")
    def test_fifo_policy_fails_instead_of_blocking(self):
        os.mkfifo(self.root / "exitzero.toml")
        result = self.cli("check", "--format", "json")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "error")

    def test_hardlinked_agents_md_is_not_written_through(self):
        self.init()
        outside = self.root / "outside.txt"
        outside.write_text("external content\n", encoding="utf-8")
        agents = self.root / "AGENTS.md"
        agents.unlink()
        os.link(outside, agents)
        self.assertEqual(self.cli("init", "--sync").returncode, 0)
        self.assertEqual(outside.read_text(encoding="utf-8"), "external content\n")
        self.assertNotEqual(os.stat(agents).st_ino, os.stat(outside).st_ino)
        self.assertIn("exitzero:begin", agents.read_text(encoding="utf-8"))

    def test_human_output_escapes_control_characters(self):
        self.init()
        (self.root / "evil\x1b[2Jfile.py").write_text("def broken(:\n", encoding="utf-8")
        result = self.cli("check")
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("\x1b", result.stdout)
        self.assertIn("\\x1b", result.stdout)

    def test_hook_input_recursion_error_still_persists_receipt(self):
        self.init()
        payload = "[" * 5000 + "1" + "]" * 5000
        with mock.patch.object(sys, "stdin", io.StringIO(payload)):
            code = cli_module.main(["--root", str(self.root), "hooks", "run",
                                    "--adapter", "cursor", "--event", "stop"])
        self.assertEqual(code, 2)
        receipts = list((self.root / ".exitzero" / "runs").glob("*.json"))
        self.assertTrue(receipts, "a hook invocation must still persist a receipt")

    def test_pre_commit_git_timeout_is_operational_error(self):
        self.init()
        with mock.patch("subprocess.run",
                        side_effect=subprocess.TimeoutExpired(["git"], 15)):
            receipt = run(self.root, "exitzero.toml", "check", "pre-commit")
        self.assertEqual(receipt["exit_code"], 2)
        self.assertIn("core.error", [f["rule"] for f in receipt["findings"]])

    def test_select_files_prunes_excluded_and_matches_globstar(self):
        (self.root / "node_modules" / "pkg").mkdir(parents=True)
        (self.root / "node_modules" / "pkg" / "x.py").write_text("", encoding="utf-8")
        (self.root / "src" / "deep").mkdir(parents=True)
        (self.root / "src" / "deep" / "a.py").write_text("", encoding="utf-8")
        (self.root / "src" / "top.py").write_text("", encoding="utf-8")
        (self.root / "tests").mkdir()
        (self.root / "tests" / "test_a.py").write_text("", encoding="utf-8")
        (self.root / "tests" / "helper.py").write_text("", encoding="utf-8")
        names = {p.relative_to(self.root).as_posix()
                 for p in select_files(self.root, ["src/**/*.py", "tests/test_*.py"])}
        self.assertEqual(names, {"src/deep/a.py", "src/top.py", "tests/test_a.py"})
        self.assertIn("src/top.py", {p.relative_to(self.root).as_posix()
                                     for p in select_files(self.root, ["**/*.py"])})
        self.assertEqual(select_files(self.root, ["src/top.py"]),
                         [self.root / "src" / "top.py"])

    @unittest.skipUnless(hasattr(os, "symlink") or os.name != "nt", "needs symlink support")
    def test_select_files_rejects_symlinked_directory(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "x.py").write_text("", encoding="utf-8")
        (self.root / "src").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            select_files(self.root, ["src/**/*.py"])

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFOs need POSIX mkfifo")
    def test_fifo_policy_blocks_init_sync_too(self):
        # load_policy's regular-file guard covers every caller, not just check.
        os.mkfifo(self.root / "exitzero.toml")
        result = self.cli("init", "--sync")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFOs need POSIX mkfifo")
    def test_fifo_manifest_is_an_operational_error(self):
        self.init()
        manifest = self.root / ".exitzero" / "hooks.json"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.unlink(missing_ok=True)
        os.mkfifo(manifest)
        # A non-regular manifest must surface as an error; treating it as an
        # empty manifest would silently disable every recorded drift check.
        result = self.cli("lint-config", "--format", "json")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(hasattr(os, "symlink") or os.name != "nt", "needs symlink support")
    def test_select_files_literal_excluded_and_unreachable_symlink(self):
        # A literal path inside an excluded directory stays excluded.
        (self.root / "node_modules" / "pkg").mkdir(parents=True)
        (self.root / "node_modules" / "pkg" / "x.py").write_text("", encoding="utf-8")
        self.assertEqual(select_files(self.root, ["node_modules/pkg/x.py"]), [])
        # A symlinked directory no pattern can reach is pruned, not fatal.
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "x.py").write_text("", encoding="utf-8")
        (self.root / "src").mkdir()
        (self.root / "src" / "a.py").write_text("", encoding="utf-8")
        (self.root / "src" / "assets").mkdir()
        (self.root / "src" / "assets" / "link").symlink_to(outside, target_is_directory=True)
        self.assertEqual([p.name for p in select_files(self.root, ["src/*.py"])], ["a.py"])
        # A pattern that could match beneath the link still fails the run.
        with self.assertRaises(ValueError):
            select_files(self.root, ["src/**/*.py"])
        # Directory-only patterns never select regular files.
        self.assertEqual(select_files(self.root, ["src/*.py/"]), [])

    def test_repeated_globstar_match_is_polynomial(self):
        # Twelve '**' segments against twelve path parts branched
        # exponentially; the index-keyed memo keeps it O(pattern * path).
        pattern = ("**",) * 12 + ("missing.py",)
        candidate = ("a",) * 12 + ("actual.py",)
        self.assertFalse(_match_parts(pattern, candidate))
        self.assertTrue(_match_parts(("**", "*.py"), ("a", "b", "c.py")))

    def test_invalid_harness_config_is_linted_not_core_error(self):
        self.init()
        policy = self.root / "exitzero.toml"
        policy.write_text(policy.read_text().replace(
            "config_files = []", "config_files = 123"))
        result = self.cli("lint-config", "--format", "json")
        payload = json.loads(result.stdout)
        # The harness linter reports the shape error; core must not die first.
        self.assertEqual(payload["exit_code"], 1, payload)
        rules = [f["rule"] for f in payload["findings"]]
        self.assertIn("harness.config", rules)


class AdapterHookTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def cli(self, *args, stdin=None):
        return subprocess.run([sys.executable, str(CLI), "--root", str(self.root), *args],
                              input=stdin, capture_output=True, text=True, timeout=30)

    def init(self):
        result = self.cli("init")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def install(self, adapter):
        result = self.cli("hooks", "install", "--adapter", adapter)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def run_hook(self, adapter, payload, *extra):
        return self.cli("hooks", "run", "--adapter", adapter, "--event", "stop",
                        *extra, stdin=json.dumps(payload))

    def break_source(self):
        (self.root / "broken.py").write_text("def broken(:\n", encoding="utf-8")

    def lint_rules(self):
        result = self.cli("lint-config", "--format", "json")
        payload = json.loads(result.stdout)
        return payload, [f["rule"] for f in payload["findings"]]

    def stop_entries(self, path):
        data = json.loads(path.read_text(encoding="utf-8"))
        return [entry for group in data["hooks"]["Stop"] for entry in group["hooks"]]

    def test_claude_install_writes_nested_stop_entry_and_manifest_digest(self):
        self.init()
        result = self.install("claude")
        settings = self.root / ".claude/settings.json"
        data = json.loads(settings.read_text(encoding="utf-8"))
        entries = self.stop_entries(settings)
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry["type"], "command")
        self.assertEqual(entry["timeout"], 120)
        self.assertIn("hooks run --adapter claude --event stop", entry["command"])
        self.assertIn("allowManagedHooksOnly", result.stdout)
        manifest = json.loads((self.root / ".exitzero/hooks.json").read_text(encoding="utf-8"))
        self.assertIn(".claude/settings.json", manifest["entries"])
        self.assertNotIn(".claude/settings.json", manifest["files"])
        self.assertEqual(len(manifest["entries"][".claude/settings.json"]), 64)
        self.assertFalse(data.get("allowManagedHooksOnly"))

    def test_codex_install_writes_dedicated_hooks_file_with_file_digest(self):
        self.init()
        result = self.install("codex")
        self.assertIn("trust", result.stdout.lower())
        entries = self.stop_entries(self.root / ".codex/hooks.json")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["timeout"], 120)
        self.assertIn("--adapter codex", entries[0]["command"])
        manifest = json.loads((self.root / ".exitzero/hooks.json").read_text(encoding="utf-8"))
        self.assertIn(".codex/hooks.json", manifest["files"])
        self.assertNotIn(".codex/hooks.json", manifest["entries"])

    def test_cursor_install_includes_loop_limit_fail_closed_and_timeout(self):
        self.init()
        # Existing foreign entries must survive installation.
        hooks_path = self.root / ".cursor/hooks.json"
        hooks_path.parent.mkdir()
        hooks_path.write_text(json.dumps({"version": 1, "hooks": {"stop": [
            {"command": "foreign-tool --gate", "loop_limit": 3}]}}), encoding="utf-8")
        self.install("cursor")
        data = json.loads(hooks_path.read_text(encoding="utf-8"))
        stops = data["hooks"]["stop"]
        self.assertEqual(len(stops), 2)
        self.assertEqual(stops[0], {"command": "foreign-tool --gate", "loop_limit": 3})
        owned = stops[1]
        self.assertEqual(owned["loop_limit"], 1)
        self.assertIs(owned["failClosed"], True)
        self.assertEqual(owned["timeout"], 120)
        manifest = json.loads((self.root / ".exitzero/hooks.json").read_text(encoding="utf-8"))
        self.assertIn(".cursor/hooks.json", manifest["files"])

    def test_reinstall_is_idempotent_and_drops_stale_entries(self):
        self.init()
        self.install("claude")
        settings = self.root / ".claude/settings.json"
        first = settings.read_text(encoding="utf-8")
        self.install("claude")
        self.assertEqual(settings.read_text(encoding="utf-8"), first)
        # A stale exitzero entry from an older root must be pruned on reinstall;
        # foreign entries stay untouched.
        data = json.loads(first)
        data["hooks"]["Stop"].append({"hooks": [
            {"type": "command", "command": "/old/root exitzero hooks run --adapter claude --event stop"},
            {"type": "command", "command": "foreign --check"}]})
        settings.write_text(json.dumps(data), encoding="utf-8")
        self.install("claude")
        entries = self.stop_entries(settings)
        commands = [entry["command"] for entry in entries]
        self.assertEqual(len(entries), 2)
        self.assertIn("foreign --check", commands)
        self.assertNotIn("/old/root exitzero hooks run --adapter claude --event stop", commands)

    def test_reinstall_preserves_foreign_hooklike_entries_and_empty_groups(self):
        self.init()
        settings = self.root / ".claude/settings.json"
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text(json.dumps({"hooks": {"Stop": [
            {"matcher": "", "hooks": [
                {"type": "command",
                 "command": "othertool hooks run --adapter claude"}]},
            {"matcher": "legacy", "hooks": []}]}}), encoding="utf-8")
        self.install("claude")
        groups = json.loads(settings.read_text(encoding="utf-8"))["hooks"]["Stop"]
        commands = [entry["command"] for group in groups for entry in group["hooks"]]
        # A foreign entry that merely looks hook-shaped is not exitzero's to prune.
        self.assertIn("othertool hooks run --adapter claude", commands)
        # A foreign group that was already empty is user data and stays.
        self.assertTrue(any(group["hooks"] == [] and group.get("matcher") == "legacy"
                            for group in groups), groups)

    def test_install_preserves_unrelated_settings_keys(self):
        self.init()
        settings = self.root / ".claude/settings.json"
        settings.parent.mkdir()
        settings.write_text(json.dumps({"theme": "dark", "permissions": {"allow": ["Read"]}}),
                            encoding="utf-8")
        self.install("claude")
        data = json.loads(settings.read_text(encoding="utf-8"))
        self.assertEqual(data["theme"], "dark")
        self.assertEqual(data["permissions"], {"allow": ["Read"]})
        self.assertEqual(len(self.stop_entries(settings)), 1)

    def test_claude_failure_returns_decision_block_and_pass_returns_empty(self):
        self.init()
        self.break_source()
        result = self.run_hook("claude", {})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["decision"], "block")
        self.assertIn("Receipt:", payload["reason"])
        receipt = payload["reason"].split("Receipt: ")[1].split(". ")[0]
        self.assertTrue((self.root / receipt).is_file())
        (self.root / "broken.py").write_text("def broken():\n    return 1\n")
        result = self.run_hook("claude", {})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout), {})

    def test_codex_failure_returns_decision_block(self):
        self.init()
        self.break_source()
        result = self.run_hook("codex", {"session_id": "abc"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["decision"], "block")
        self.assertIn("exitzero failed", payload["reason"])

    def test_invalid_json_persists_receipt_and_exits_two(self):
        self.init()
        for adapter in ("claude", "codex"):
            result = self.cli("hooks", "run", "--adapter", adapter, "--event", "stop",
                              stdin="{not json")
            self.assertEqual(result.returncode, 2, adapter + result.stdout + result.stderr)
            payload = json.loads(result.stdout)
            self.assertIn("error", payload)
            self.assertTrue((self.root / payload["receipt"]).is_file())

    def test_adapter_rejects_unsupported_events(self):
        self.init()
        for adapter in ("claude", "codex"):
            result = self.cli("hooks", "run", "--adapter", adapter, "--event", "preToolUse",
                              stdin="{}")
            self.assertEqual(result.returncode, 2, adapter)
            self.assertIn("does not handle event", result.stderr)

    def test_lint_ignores_unrelated_shared_settings_edits(self):
        self.init()
        self.install("claude")
        settings = self.root / ".claude/settings.json"
        data = json.loads(settings.read_text(encoding="utf-8"))
        data["theme"] = "light"
        data["hooks"]["Stop"].append({"hooks": [{"type": "command", "command": "foreign"}]})
        settings.write_text(json.dumps(data), encoding="utf-8")
        payload, rules = self.lint_rules()
        self.assertEqual(payload["exit_code"], 0, payload)
        self.assertNotIn("harness.hooks-drift", rules)

    def test_lint_flags_managed_entry_edit_and_removal(self):
        self.init()
        self.install("claude")
        settings = self.root / ".claude/settings.json"
        data = json.loads(settings.read_text(encoding="utf-8"))
        data["hooks"]["Stop"][0]["hooks"][0]["timeout"] = 30
        settings.write_text(json.dumps(data), encoding="utf-8")
        payload, rules = self.lint_rules()
        self.assertEqual(payload["exit_code"], 1)
        self.assertIn("harness.hooks-drift", rules)
        # Deleting the managed entry also drifts.
        data["hooks"]["Stop"][0]["hooks"] = []
        settings.write_text(json.dumps(data), encoding="utf-8")
        payload, rules = self.lint_rules()
        self.assertIn("harness.hooks-drift", rules)

    def test_lint_warns_when_managed_hooks_only_disables_gate(self):
        self.init()
        self.install("claude")
        settings = self.root / ".claude/settings.json"
        data = json.loads(settings.read_text(encoding="utf-8"))
        data["allowManagedHooksOnly"] = True
        settings.write_text(json.dumps(data), encoding="utf-8")
        payload, rules = self.lint_rules()
        self.assertIn("harness.hooks-managed-only", rules)
        self.assertEqual(payload["exit_code"], 0, payload)

    def test_invalid_existing_settings_block_install(self):
        self.init()
        settings = self.root / ".claude/settings.json"
        settings.parent.mkdir()
        settings.write_text("{broken json", encoding="utf-8")
        result = self.cli("hooks", "install", "--adapter", "claude")
        self.assertEqual(result.returncode, 2)
        self.assertIn("exitzero:", result.stderr + result.stdout)


class TestIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for args in (["init"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
            subprocess.run(["git", "-C", str(self.root), *args],
                           capture_output=True, check=True, timeout=15)
        (self.root / "exitzero.toml").write_text(
            'version = 1\nplugins = ["exitzero_verify"]\n'
            '[[checks]]\nid = "integrity"\nkind = "python.test-integrity"\n'
            'paths = ["tests/**/*.py"]\nreuse = false\n', encoding="utf-8")

    def cli(self, *args):
        return subprocess.run([sys.executable, str(CLI), "--root", str(self.root), *args],
                              capture_output=True, text=True, timeout=30)

    def commit(self, *paths):
        for path in paths:
            full = self.root / path
            full.parent.mkdir(parents=True, exist_ok=True)
            if not full.exists():
                full.write_text("", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "-A"],
                       capture_output=True, check=True, timeout=15)
        subprocess.run(["git", "-C", str(self.root), "commit", "-m", "baseline", "--no-gpg-sign"],
                       capture_output=True, check=True, timeout=15)

    def check(self, expected):
        result = self.cli("check", "--format", "json")
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def write_test(self, name="tests/test_app.py", body=None):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body if body is not None else
                        "def test_one():\n    assert app() == 1\n\ndef test_two():\n    assert app() == 2\n",
                        encoding="utf-8")
        return path

    def test_deleted_test_file_is_flagged(self):
        self.write_test()
        self.commit()
        (self.root / "tests/test_app.py").unlink()
        payload = self.check(1)
        messages = [f["message"] for f in payload["findings"]]
        self.assertTrue(any("deleted" in message for message in messages), messages)

    def test_removed_test_case_and_assertions_are_flagged(self):
        self.write_test()
        self.commit()
        self.write_test(body="def test_one():\n    assert app() == 1\n")
        payload = self.check(1)
        messages = [f["message"] for f in payload["findings"]]
        self.assertTrue(any("test_two" in message for message in messages), messages)
        self.assertTrue(any("reduced from 2 to 1" in message for message in messages), messages)

    def test_new_skip_markers_are_flagged(self):
        self.write_test()
        self.commit()
        self.write_test(body=(
            "import pytest\n\n@pytest.mark.skip(reason='later')\n"
            "def test_one():\n    assert app() == 1\n\ndef test_two():\n    pytest.xfail('broken')\n"))
        payload = self.check(1)
        messages = [f["message"] for f in payload["findings"]]
        self.assertTrue(any("pytest.mark.skip" in message for message in messages), messages)
        self.assertTrue(any("pytest.xfail" in message for message in messages), messages)

    def test_strengthened_tests_pass_and_unrelated_deletions_ignored(self):
        self.write_test()
        (self.root / "docs").mkdir()
        (self.root / "docs/note.md").write_text("x", encoding="utf-8")
        self.commit()
        self.write_test(body="def test_one():\n    assert app() == 1\n"
                             "def test_two():\n    assert app() == 2\n    assert app() == 3\n")
        (self.root / "docs/note.md").unlink()
        self.check(0)

    def test_allow_options_relax_findings(self):
        policy = self.root / "exitzero.toml"
        policy.write_text(policy.read_text() +
                          '[checks.options]\nallow_deletions = true\nallow_skip_markers = true\n'
                          'max_removed_assertions = 5\n')
        self.write_test()
        self.commit()
        (self.root / "tests/test_app.py").unlink()
        self.check(0)

    def test_missing_git_or_bad_base_is_operational_error(self):
        self.write_test()
        payload = self.check(2)
        self.assertIn("core.error", [f["rule"] for f in payload["findings"]])
        self.commit()
        policy = self.root / "exitzero.toml"
        policy.write_text(policy.read_text() + '[checks.options]\nbase = "nosuchref"\n')
        payload = self.check(2)
        self.assertIn("core.error", [f["rule"] for f in payload["findings"]])

    def test_reuse_false_check_is_never_reused(self):
        self.write_test()
        self.commit()
        first = self.check(0)
        # spec.paths still record as inputs for evidence; the reuse opt-out
        # keeps a moved baseline from serving a stale pass.
        self.assertEqual(first["checks"][0]["input_files"], ["tests/test_app.py"])
        second = self.check(0)
        self.assertEqual(second["checks"][0]["status"], "passed")
        payload = self.cli("check", "--reuse", "--format", "json")
        self.assertEqual(payload.returncode, 0)
        self.assertEqual(json.loads(payload.stdout)["checks"][0]["status"], "passed")

    def test_diff_scoped_run_still_flags_deleted_tests(self):
        # Deleted paths must survive --diff narrowing: dropping them would let
        # a PR delete its tests and pass the scoped integrity check.
        self.write_test()
        self.commit()
        (self.root / "tests/test_app.py").unlink()
        result = self.cli("check", "--diff", "HEAD", "--format", "json")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        messages = [f["message"] for f in json.loads(result.stdout)["findings"]]
        self.assertTrue(any("deleted" in message for message in messages), messages)

    def test_subdirectory_root_uses_repo_prefix(self):
        sub = self.root / "pkg"
        sub.mkdir()
        (sub / "exitzero.toml").write_text(
            'version = 1\nplugins = ["exitzero_verify"]\n'
            '[[checks]]\nid = "integrity"\nkind = "python.test-integrity"\n'
            'paths = ["tests/**/*.py"]\n', encoding="utf-8")
        (sub / "tests").mkdir()
        (sub / "tests/test_sub.py").write_text("def test_a():\n    assert True\n", encoding="utf-8")
        self.commit()
        (sub / "tests/test_sub.py").write_text("def test_a():\n    pass\n", encoding="utf-8")
        result = subprocess.run([sys.executable, str(CLI), "--root", str(sub),
                                 "check", "--format", "json"],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        messages = [f["message"] for f in json.loads(result.stdout)["findings"]]
        self.assertTrue(any("reduced from 1 to 0" in message for message in messages), messages)


class DiffScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for args in (["init"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
            subprocess.run(["git", "-C", str(self.root), *args],
                           capture_output=True, check=True, timeout=15)

    def cli(self, *args):
        return subprocess.run([sys.executable, str(CLI), "--root", str(self.root), *args],
                              capture_output=True, text=True, timeout=30)

    def commit(self):
        subprocess.run(["git", "-C", str(self.root), "add", "-A"],
                       capture_output=True, check=True, timeout=15)
        subprocess.run(["git", "-C", str(self.root), "commit", "-m", "base", "--no-gpg-sign"],
                       capture_output=True, check=True, timeout=15)

    def init_and_commit(self):
        result = self.cli("init")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.commit()

    def test_diff_scopes_checks_to_changed_files(self):
        self.init_and_commit()
        # A committed broken file must not block a diff-scoped run that did
        # not touch it; untracked files are outside `git diff HEAD` too.
        (self.root / "old.py").write_text("def broken(:\n", encoding="utf-8")
        self.commit()
        (self.root / "note.txt").write_text("unrelated\n", encoding="utf-8")
        result = self.cli("check", "--diff", "HEAD", "--format", "json")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["diff"], "HEAD")
        inputs = {check["id"]: check["input_files"] for check in payload["checks"]
                  if check["kind"] != "config-lint"}
        self.assertTrue(all(files == [] for files in inputs.values()), inputs)
        # Now the broken file itself changes — it enters scope and fails.
        (self.root / "old.py").write_text("def still_broken(:\n", encoding="utf-8")
        result = self.cli("check", "--diff", "HEAD", "--format", "json")
        self.assertEqual(result.returncode, 1)
        payload = json.loads(result.stdout)
        syntax = next(check for check in payload["checks"] if check["id"] == "syntax")
        self.assertEqual(syntax["input_files"], ["old.py"])
        self.assertEqual(syntax["status"], "failed")

    def test_diff_skips_deleted_and_unresolvable_ref_errors(self):
        self.init_and_commit()
        (self.root / "exitzero_sample.py").unlink()
        result = self.cli("check", "--diff", "HEAD", "--format", "json")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        result = self.cli("check", "--diff", "nosuchref", "--format", "json")
        self.assertEqual(result.returncode, 2)
        self.assertIn("core.error", [f["rule"] for f in json.loads(result.stdout)["findings"]])

    def test_diff_narrows_literal_paths_with_glob_metacharacters(self):
        self.init_and_commit()
        # A committed file literally named test[1].py must be checked when it
        # changes — an unescaped pattern would match test1.py instead.
        (self.root / "weird[1].py").write_text("def ok(): return 1\n", encoding="utf-8")
        self.commit()
        (self.root / "weird[1].py").write_text("def broken(:\n", encoding="utf-8")
        result = self.cli("check", "--diff", "HEAD", "--format", "json")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        payload = json.loads(result.stdout)
        syntax = next(check for check in payload["checks"] if check["id"] == "syntax")
        self.assertEqual(syntax["status"], "failed")
        self.assertEqual(syntax["input_files"], ["weird[1].py"])

    def test_reuse_field_must_be_boolean(self):
        self.init_and_commit()
        policy = self.root / "exitzero.toml"
        policy.write_text(policy.read_text().replace(
            'id = "syntax"', 'id = "syntax"\nreuse = "yes"'))
        result = self.cli("check", "--format", "json")
        self.assertEqual(result.returncode, 2)
        self.assertIn("core.error", [f["rule"] for f in json.loads(result.stdout)["findings"]])

    def test_report_intoto_wraps_latest_receipt(self):
        self.init_and_commit()
        check = self.cli("check", "--format", "json")
        self.assertEqual(check.returncode, 0)
        result = self.cli("report", "--format", "intoto")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        statement = json.loads(result.stdout)
        self.assertEqual(statement["_type"], "https://in-toto.io/Statement/v1")
        self.assertTrue(statement["predicateType"].startswith("https://"))
        predicate = statement["predicate"]
        self.assertEqual(predicate["run_id"], json.loads(check.stdout)["run_id"])
        subject = statement["subject"][0]
        canonical = json.dumps(predicate, sort_keys=True, separators=(",", ":"),
                               ensure_ascii=False).encode("utf-8")
        self.assertEqual(subject["digest"]["sha256"], hashlib.sha256(canonical).hexdigest())


if __name__ == "__main__":
    unittest.main()
