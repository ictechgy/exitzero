#!/usr/bin/env python3
"""Evaluate a pinned real repository and four independent fault injections."""
import argparse
import ast
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
PIN = "62fc59bd5899c25e422d070915f2d2cd8d55ec51"
CLI = ROOT / "bin/exitzero"
PROFILE = ROOT / "examples/riskgate"
sys.path.insert(0, str(ROOT / "packages/core/src"))
from exitzero.files import safe_path


from pilot_support import digest, execute, export, gate, tracked_state


def mutate(case: Path, name: str) -> str | None:
    if name == "baseline":
        return None
    if name == "agents-drift":
        relative, old, new = "AGENTS.md", "Policy SHA-256:", "Stale policy SHA-256:"
    elif name == "broken-import":
        relative, old, new = "riskgate/cli.py", "from .policy import check_policy, load_policy", "from .missing_policy import check_policy, load_policy"
    elif name == "wrong-exit-code":
        relative, old, new = "riskgate/cli.py", "return 1 if failures else 0", "return 0  # Deliberately incorrect pilot mutation."
    elif name == "empty-test":
        relative = "tests/test_cli.py"
        path = case / relative
        text = path.read_text()
        tree = ast.parse(text)
        cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "TestCommandTest")
        method = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == "test_failing_case_exits_one")
        lines = text.splitlines(keepends=True)
        lines[method.body[0].lineno - 1:method.end_lineno] = ["        pass  # Deliberately empty pilot test.\n"]
        path.write_text("".join(lines), encoding="utf-8")
        return relative
    else:
        raise ValueError("Unknown pilot case")
    path = case / relative
    text = path.read_text()
    if text.count(old) != 1:
        raise AssertionError("Pinned mutation anchor is not unique")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")
    return relative


def run(source: Path) -> tuple[dict, Path]:
    before = tracked_state(source)
    if before["head"] != PIN:
        raise ValueError("Source HEAD differs from the reviewed pilot revision")
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    artifact = safe_path(ROOT, f".exitzero/pilots/riskgate-{timestamp}-{uuid.uuid4().hex[:8]}")
    artifact.mkdir(parents=True)
    summary = {"schema_version": 1, "source_commit": PIN, "status": "failed", "cases": [],
               "artifact": artifact.relative_to(ROOT).as_posix()}
    try:
        template = artifact / "template"
        manifest = export(source, template, PIN)
        (artifact / "source-manifest.json").write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
        summary["source_file_count"] = len(manifest)
        summary["gate_code_sha256"] = {p.relative_to(ROOT).as_posix(): digest(p.read_bytes()) for p in sorted([
            *ROOT.glob("packages/*/src/**/*.py"), CLI, Path(__file__).resolve(), ROOT / "scripts/pilot_support.py", *PROFILE.glob("*")]) if p.is_file()}
        original_agents = (template / "AGENTS.md").read_text()
        shutil.copy2(PROFILE / "exitzero.toml", template / "exitzero.toml")
        shutil.copy2(PROFILE / "run_tests.py", template / "_exitzero_pilot_tests.py")
        sync = execute(template, [sys.executable, str(CLI), "--root", str(template), "init", "--sync"], artifact / "sync.log")
        if sync["exit_code"] != 0 or not (template / "AGENTS.md").read_text().startswith(original_agents.rstrip()):
            raise AssertionError("Policy sync failed or overwrote original agent guidance")
        summary["original_agent_guidance_preserved"] = True
        summary["upstream"] = {}
        for name, argv in (("tests", [sys.executable, "_exitzero_pilot_tests.py"]),
                           ("policy-lint", [sys.executable, "-m", "riskgate", "lint", "riskgate.yaml"]),
                           ("policy-cases", [sys.executable, "-m", "riskgate", "test", "riskgate.yaml"])):
            result = execute(template, argv, artifact / f"upstream-{name}.log")
            summary["upstream"][name] = result
            if result["exit_code"] != 0:
                raise AssertionError("Upstream baseline failed; inspect its recorded log")
        summary["upstream_tests"] = json.loads((template / ".exitzero/riskgate-tests.json").read_text())
        expectations = (
            ("baseline", 0, set(), 0),
            ("broken-import", 1, {"imports", "regression-suite", "policy-lint", "policy-cases"}, 0),
            ("empty-test", 1, {"test-quality"}, 0),
            ("wrong-exit-code", 1, {"regression-suite"}, 0),
            ("agents-drift", 1, {"harness.agents.drift"}, 1),
        )
        for name, expected, rules, lint_exit in expectations:
            case = artifact / name
            shutil.copytree(template, case, ignore=shutil.ignore_patterns(".exitzero", "__pycache__", "*.pyc"))
            changed = mutate(case, name)
            check = gate(case, "check", artifact / f"{name}-check.log")
            lint = gate(case, "lint-config", artifact / f"{name}-lint.log")
            found = {f["rule"] for f in check["receipt"]["findings"]}
            checks = {c["id"]: c["status"] for c in check["receipt"]["checks"]}
            passed = check["exit_code"] == expected and found == rules and lint["exit_code"] == lint_exit
            if name == "empty-test":
                passed = passed and checks.get("regression-suite") == "passed"
            result = {"name": name, "status": "passed" if passed else "failed", "changed_file": changed,
                      "expected_exit_code": expected, "expected_rules": sorted(rules), "check": check, "lint": lint}
            tests_path = case / ".exitzero/riskgate-tests.json"
            if tests_path.exists():
                result["upstream_tests"] = json.loads(tests_path.read_text())
            summary["cases"].append(result)
            print(f"{name}: {'PASS' if passed else 'FAIL'} (check={check['exit_code']}, lint={lint['exit_code']}, findings={','.join(sorted(found)) or 'none'})", flush=True)
        summary["passed"] = sum(case["status"] == "passed" for case in summary["cases"])
        summary["status"] = "passed" if summary["passed"] == len(expectations) else "failed"
    except Exception as error:
        summary["error"] = f"Pilot did not complete ({type(error).__name__}); inspect preserved logs."
    finally:
        summary["source_unchanged"] = tracked_state(source) == before
        if not summary["source_unchanged"]:
            summary["status"] = "failed"
        output = artifact / "summary.json"
        output.write_text(json.dumps(summary, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return summary, output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="Local riskgate checkout at the reviewed commit; no fetch is performed")
    args = parser.parse_args()
    summary, output = run(args.source.resolve())
    print(f"Pilot: {summary['status']}; source unchanged: {summary['source_unchanged']}")
    print("Evidence: " + output.relative_to(ROOT).as_posix())
    return 0 if summary["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
