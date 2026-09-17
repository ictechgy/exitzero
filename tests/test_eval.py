import json
import os
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "core" / "src"))
sys.path.insert(0, str(ROOT / "packages" / "plugin-harness" / "src"))
sys.path.insert(0, str(ROOT / "packages" / "plugin-verify" / "src"))

from exitzero.api import Context  # noqa: E402
from exitzero.policy import load_policy, render_agents  # noqa: E402
from exitzero_harness import harness_eval  # noqa: E402


POLICY = """version = 1
plugins = ["exitzero_verify", "exitzero_harness"]

[[checks]]
id = "syntax"
kind = "python.syntax"
paths = ["src/**/*.py"]

[harness]
config_files = []
rules = []
"""


def make_context(root: Path) -> Context:
    return Context(root=root, policy={}, policy_path=root / "exitzero.toml")


def write_base(scenario: Path) -> None:
    base = scenario / "base"
    (base / "src").mkdir(parents=True)
    (base / "exitzero.toml").write_text(POLICY, encoding="utf-8")
    (base / "src/tool.py").write_text("def add(a: int, b: int) -> int:\n    return a + b\n",
                                     encoding="utf-8")
    (base / "AGENTS.md").write_text(
        render_agents(load_policy(base / "exitzero.toml")), encoding="utf-8")


def write_turn(scenario: Path, name: str, expect_exit: int, rules: list[str],
               overlays: dict[str, str] | None = None, delete: list[str] | None = None) -> None:
    turn = scenario / "turns" / name
    turn.mkdir(parents=True)
    rules_toml = "[" + ", ".join(json.dumps(rule) for rule in rules) + "]"
    delete_toml = ""
    if delete:
        delete_toml = "delete = [" + ", ".join(json.dumps(path) for path in delete) + "]\n"
    (turn / "turn.toml").write_text(
        f"{delete_toml}[expect]\nexit = {expect_exit}\nrules = {rules_toml}\n", encoding="utf-8")
    for relative, content in (overlays or {}).items():
        target = turn / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def eval_report(root: Path) -> dict:
    reports = sorted((root / ".exitzero" / "evals").glob("harness-eval-*.json"))
    assert reports, "eval report was not persisted"
    return json.loads(reports[-1].read_text(encoding="utf-8"))


class HarnessEvalTests(unittest.TestCase):
    def test_usage_errors_exit_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context = make_context(root)
            self.assertEqual(harness_eval(context, []), 2)
            self.assertEqual(harness_eval(context, ["--scenario"]), 2)
            self.assertEqual(harness_eval(context, ["--unknown", "x"]), 2)
            self.assertEqual(harness_eval(context, ["--scenario", "missing-dir"]), 2)
            (root / "empty").mkdir()
            self.assertEqual(harness_eval(context, ["--scenario", "empty"]), 2)

    def test_matching_scenario_passes_and_persists_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scenario = root / "scenarios" / "repair"
            write_base(scenario)
            (scenario / "scenario.toml").write_text(
                'schema_version = 1\ndescription = "demo"\n', encoding="utf-8")
            write_turn(scenario, "01-break", 1, ["syntax"],
                       {"src/tool.py": "def add(a: int, b: int) -> int:\n    return a +\n"})
            write_turn(scenario, "02-fix", 0, [],
                       {"src/tool.py": "def add(a: int, b: int) -> int:\n    return a + b\n"})
            result = harness_eval(make_context(root), ["--scenario", "scenarios"])
            self.assertEqual(result, 0)
            report = eval_report(root)
            self.assertEqual(report["status"], "passed")
            self.assertEqual((report["passed"], report["failed"], report["skipped"]), (1, 0, 0))
            turns = report["scenarios"][0]["turns"]
            self.assertEqual([turn["name"] for turn in turns], ["01-break", "02-fix"])
            self.assertTrue(all(turn["matched"] for turn in turns))
            self.assertTrue(all(turn["receipt"]["run_id"] for turn in turns))
            # Reports keep findings and ids but never the per-file input maps.
            self.assertTrue(all("inputs" not in turn["receipt"] for turn in turns))

    def test_mismatched_turn_fails_scenario(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scenario = root / "wrong"
            write_base(scenario)
            (scenario / "scenario.toml").write_text("schema_version = 1\n", encoding="utf-8")
            write_turn(scenario, "01-expected-clean", 0, [],
                       {"src/tool.py": "def add(a: int, b: int) -> int:\n    return a +\n"})
            result = harness_eval(make_context(root), ["--scenario", "wrong"])
            self.assertEqual(result, 1)
            report = eval_report(root)
            self.assertEqual(report["failed"], 1)
            self.assertFalse(report["scenarios"][0]["turns"][0]["matched"])

    def test_skip_and_manifest_defects_report_separately(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            container = root / "scenarios"
            skipped = container / "live-only"
            skipped.mkdir(parents=True)
            (skipped / "scenario.toml").write_text(
                'schema_version = 1\nskip = "needs a live session"\n', encoding="utf-8")
            broken = container / "broken"
            broken.mkdir()
            (broken / "scenario.toml").write_text('schema_version = "one"\n', encoding="utf-8")
            result = harness_eval(make_context(root), ["--scenario", "scenarios"])
            self.assertEqual(result, 1)
            report = eval_report(root)
            self.assertEqual((report["passed"], report["failed"], report["skipped"]), (0, 1, 1))
            self.assertEqual(report["skipped_scenarios"],
                             [{"name": "live-only", "reason": "needs a live session"}])

    def test_turn_bound_is_enforced(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scenario = root / "loop"
            write_base(scenario)
            (scenario / "scenario.toml").write_text(
                "schema_version = 1\nmax_turns = 1\n", encoding="utf-8")
            write_turn(scenario, "01", 0, [])
            write_turn(scenario, "02", 0, [])
            result = harness_eval(make_context(root), ["--scenario", "loop"])
            self.assertEqual(result, 1)
            report = eval_report(root)
            self.assertTrue(any("bound" in error for error in report["scenarios"][0]["errors"]))

    def test_delete_applies_and_missing_delete_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scenario = root / "deleter"
            write_base(scenario)
            (scenario / "scenario.toml").write_text("schema_version = 1\n", encoding="utf-8")
            # Deleting every selected file is a policy error, not a quiet pass.
            write_turn(scenario, "01-remove-source", 2, ["core.error"], delete=["src/tool.py"])
            result = harness_eval(make_context(root), ["--scenario", "deleter"])
            self.assertEqual(result, 0)

            bad = root / "bad-delete"
            write_base(bad)
            (bad / "scenario.toml").write_text("schema_version = 1\n", encoding="utf-8")
            write_turn(bad, "01-remove-ghost", 0, [], delete=["src/ghost.py"])
            self.assertEqual(harness_eval(make_context(root), ["--scenario", "bad-delete"]), 1)

    def test_missing_turn_manifest_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scenario = root / "no-turn-manifest"
            write_base(scenario)
            (scenario / "scenario.toml").write_text("schema_version = 1\n", encoding="utf-8")
            (scenario / "turns" / "01").mkdir(parents=True)
            self.assertEqual(harness_eval(make_context(root), ["--scenario", "no-turn-manifest"]), 1)

    def test_container_reports_dirs_without_manifests(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            container = root / "scenarios"
            good = container / "good"
            write_base(good)
            (good / "scenario.toml").write_text("schema_version = 1\n", encoding="utf-8")
            write_turn(good, "01", 0, [])
            (container / "forgotten").mkdir()
            result = harness_eval(make_context(root), ["--scenario", "scenarios"])
            self.assertEqual(result, 1)
            report = eval_report(root)
            self.assertEqual((report["passed"], report["failed"]), (1, 1))
            forgotten = next(s for s in report["scenarios"] if s["name"] == "forgotten")
            self.assertTrue(any("scenario.toml" in error for error in forgotten["errors"]))

    def test_overlay_symlink_sources_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scenario = root / "linky"
            write_base(scenario)
            (scenario / "scenario.toml").write_text("schema_version = 1\n", encoding="utf-8")
            write_turn(scenario, "01", 0, [])
            link_parent = scenario / "turns" / "01" / "src"
            link_parent.mkdir()
            os.symlink(root / "outside.txt", link_parent / "link.py")
            result = harness_eval(make_context(root), ["--scenario", "linky"])
            self.assertEqual(result, 1)
            report = eval_report(root)
            self.assertTrue(any("symlink" in error
                                for error in report["scenarios"][0]["errors"]))

    def test_receipt_persistence_failure_is_operational_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scenario = root / "blocker"
            write_base(scenario)
            (scenario / "scenario.toml").write_text("schema_version = 1\n", encoding="utf-8")
            # A regular file named .exitzero makes receipt persistence fail; the
            # matching expectation must still surface as an eval error, not a pass.
            write_turn(scenario, "01", 2, ["core.receipt"], {".exitzero": "occupied\n"})
            result = harness_eval(make_context(root), ["--scenario", "blocker"])
            self.assertEqual(result, 2)
            report = eval_report(root)
            self.assertEqual(report["status"], "error")
            self.assertEqual(report["errors"], 1)


if __name__ == "__main__":
    unittest.main()
