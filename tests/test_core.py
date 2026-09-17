"""Black-box contracts for the CLI, policy, receipts and extension boundary."""
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


if __name__ == "__main__":
    unittest.main()
