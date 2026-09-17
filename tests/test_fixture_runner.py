import importlib.util
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location("run_fixtures", ROOT / "scripts" / "run_fixtures.py")
runner = importlib.util.module_from_spec(_spec)
sys.modules[runner.__name__] = runner
_spec.loader.exec_module(runner)


def write_manifest(directory: Path, body: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "fixture.toml").write_text(body, encoding="utf-8")


class ManifestTests(unittest.TestCase):
    def test_valid_manifest_loads_expectations(self):
        with tempfile.TemporaryDirectory() as tmp:
            case_dir = Path(tmp) / "01-demo"
            write_manifest(case_dir, """
schema_version = 1
description = "demo"

[expect.check]
exit = 1
rules = ["syntax"]

[expect.lint]
exit = 0
rules = []
""")
            case = runner.load_manifest(case_dir)
            self.assertEqual(case["name"], "01-demo")
            self.assertEqual(case["check"], {"exit": 1, "rules": ["syntax"]})
            self.assertEqual(case["lint"], {"exit": 0, "rules": []})

    def test_missing_and_unparseable_manifests_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "no-manifest"
            missing.mkdir()
            with self.assertRaisesRegex(ValueError, "missing fixture.toml"):
                runner.load_manifest(missing)
            broken = Path(tmp) / "broken"
            write_manifest(broken, "[unclosed\n")
            with self.assertRaisesRegex(ValueError, "cannot be parsed"):
                runner.load_manifest(broken)

    def test_bad_schema_fields_fail(self):
        bad_bodies = [
            'schema_version = 2\n',
            'schema_version = 1\nexpect = {check = {exit = true, rules = []}, lint = {exit = 0, rules = []}}\n',
            'schema_version = 1\nextra = 1\n',
            'schema_version = 1\n\n[expect.check]\nexit = 0\nrules = "syntax"\n\n[expect.lint]\nexit = 0\nrules = []\n',
            'schema_version = 1\nskip = " "\n',
        ]
        with tempfile.TemporaryDirectory() as tmp:
            for index, body in enumerate(bad_bodies):
                case_dir = Path(tmp) / f"case-{index}"
                write_manifest(case_dir, body)
                with self.subTest(body=body):
                    with self.assertRaises(ValueError):
                        runner.load_manifest(case_dir)

    def test_skipped_case_still_validates_supplied_expectations(self):
        with tempfile.TemporaryDirectory() as tmp:
            case_dir = Path(tmp) / "skipped-bad"
            write_manifest(case_dir, """
schema_version = 1
skip = "live only"

[expect.check]
exit = true
rules = 123
""")
            with self.assertRaises(ValueError):
                runner.load_manifest(case_dir)

            ok_dir = Path(tmp) / "skipped-ok"
            write_manifest(ok_dir, 'schema_version = 1\nskip = "live only"\n')
            case = runner.load_manifest(ok_dir)
            self.assertEqual(case["skip"], "live only")
            self.assertNotIn("check", case)


class RunnerSummaryTests(unittest.TestCase):
    def test_fail_skip_and_pass_counts_and_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp).resolve()
            fixtures = tmp_root / "fixtures"
            write_manifest(fixtures / "pass-case", """
schema_version = 1

[expect.check]
exit = 0
rules = []

[expect.lint]
exit = 0
rules = []
""")
            write_manifest(fixtures / "skip-case", 'schema_version = 1\nskip = "needs a human"\n')
            write_manifest(fixtures / "broken-case", 'schema_version = "one"\n')

            runner.ROOT = tmp_root
            runner.FIXTURES = fixtures
            runner.run_case = lambda case, timeout=120.0: {"name": case["name"], "status": "PASS"}

            self.assertEqual(runner.main([]), 1)
            summary = json.loads((tmp_root / ".exitzero" / "fixture-results.json").read_text())
            self.assertEqual((summary["passed"], summary["failed"], summary["skipped"]), (1, 1, 1))
            self.assertEqual(summary["skipped_cases"],
                             [{"name": "skip-case", "reason": "needs a human"}])
            self.assertEqual(
                [r["status"] for r in summary["results"]],
                ["FAIL", "PASS", "SKIP"],
            )

            runner.FIXTURES = tmp_root / "only-skip"
            write_manifest(runner.FIXTURES / "skip-case", 'schema_version = 1\nskip = "needs a human"\n')
            self.assertEqual(runner.main([]), 0)


class TimeoutTests(unittest.TestCase):
    def test_non_finite_timeout_is_rejected(self):
        # nan/inf slip past a bare `timeout <= 0` check and would make
        # communicate() raise ValueError or wait forever.
        self.assertEqual(runner.main(["--timeout", "nan"]), 2)
        self.assertEqual(runner.main(["--timeout", "inf"]), 2)
        self.assertEqual(runner.main(["--timeout", "-1"]), 2)

    def test_timeout_kills_descendants_outside_the_gate_process_group(self):
        # Command checks start their own session, so a group kill on the gate
        # alone would orphan them; the sweep must find and kill them.
        runner.ROOT = ROOT  # earlier tests repoint the module-level repo root
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            marker = "ez-orphan-marker-7f3a"
            (root / "exitzero.toml").write_text(
                'version = 1\nplugins = ["exitzero_verify"]\n'
                '[[checks]]\nid = "hang"\nkind = "command"\npaths = ["*.py"]\n'
                '[checks.options]\n'
                f'argv = ["{sys.executable}", "-c", "import time; time.sleep(60) # {marker}"]\n'
                'timeout = 300\n', encoding="utf-8")
            with self.assertRaises(subprocess.TimeoutExpired):
                runner.invoke(root, "check", timeout=2)
            time.sleep(0.5)
            found = subprocess.run(["pgrep", "-f", marker],
                                   capture_output=True, text=True)
            self.assertNotEqual(found.returncode, 0,
                                f"orphaned command check survived: {found.stdout}")


if __name__ == "__main__":
    unittest.main()
