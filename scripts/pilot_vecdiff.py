#!/usr/bin/env python3
"""Exercise generated Python policy against vecdiff and independent mutations."""
import argparse
import ast
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import sys
import uuid

from pilot_support import CLI, ROOT, digest, execute, export, gate, safe_path, tracked_state

PIN = "fd561f863aabc3937d866bf5d7d1f4090c185284"
PROFILE = ROOT / "examples/vecdiff"


def mutate(case: Path, name: str) -> str | None:
    if name == "baseline":
        return None
    if name == "empty-test":
        relative = "tests/test_cli.py"
        path = case / relative
        text = path.read_text()
        method = next(node for node in ast.parse(text).body if isinstance(node, ast.FunctionDef)
                      and node.name == "test_cli_gate_red_on_heavy_regression")
        lines = text.splitlines(keepends=True)
        lines[method.body[0].lineno - 1:method.end_lineno] = ["    pass  # Deliberately empty pilot test.\n"]
        path.write_text("".join(lines), encoding="utf-8")
        return relative
    replacements = {
        "broken-import": ("src/vecdiff/knn.py", "import numpy as np", "import numpy_typo as np"),
        "hallucinated-api": ("src/vecdiff/knn.py", "v = np.asarray(vectors, dtype=np.float32)", "v = np.as_array(vectors, dtype=np.float32)"),
        "wrong-exit-code": ("src/vecdiff/cli.py", "return checks.gate_exit_code(findings)", "return 0  # Deliberately incorrect pilot verdict."),
        "numeric-regression": ("src/vecdiff/knn.py", "return v / safe", "return v  # Deliberately omit normalization."),
        "agents-drift": ("AGENTS.md", "Policy SHA-256:", "Stale policy SHA-256:"),
    }
    relative, before, after = replacements[name]
    path = case / relative
    text = path.read_text()
    if text.count(before) != 1:
        raise AssertionError("Pinned mutation anchor is not unique")
    path.write_text(text.replace(before, after, 1), encoding="utf-8")
    return relative


def run(source: Path, python: str) -> tuple[dict, Path]:
    before = tracked_state(source)
    if before["head"] != PIN:
        raise ValueError("Source HEAD differs from the reviewed pilot revision")
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    artifact = safe_path(ROOT, f".aidd-gate/pilots/vecdiff-{timestamp}-{uuid.uuid4().hex[:8]}")
    artifact.mkdir(parents=True)
    summary = {"schema_version": 1, "source_commit": PIN, "status": "failed", "cases": [],
               "artifact": artifact.relative_to(ROOT).as_posix()}
    try:
        template = artifact / "template"
        manifest = export(source, template, PIN)
        (artifact / "source-manifest.json").write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
        summary["source_file_count"] = len(manifest)
        summary["gate_code_sha256"] = {p.relative_to(ROOT).as_posix(): digest(p.read_bytes()) for p in sorted([
            *ROOT.glob("packages/*/src/**/*.py"), CLI, Path(__file__).resolve(),
            ROOT / "scripts/pilot_support.py", *PROFILE.glob("*.py")]) if p.is_file()}
        original_agents = (template / "AGENTS.md").read_text()
        shutil.copy2(PROFILE / "run_tests.py", template / "_aidd_gate_pilot_tests.py")
        shutil.copy2(PROFILE / "review_contract.py", template / "_aidd_gate_review_contract.py")
        init = execute(template, [python, str(CLI), "--root", str(template), "init", "--profile", "python",
                                  "--source-root", "src", "--source-root", "tests",
                                  "--allow-module", "numpy", "--allow-module", "pytest", "--allow-module", "faiss",
                                  "--test-command", "{python} _aidd_gate_pilot_tests.py",
                                  "--review-command", "{python} _aidd_gate_review_contract.py"], artifact / "init.log")
        if init["exit_code"] != 0 or not (template / "AGENTS.md").read_text().startswith(original_agents.rstrip()):
            raise AssertionError("Generated policy failed or overwrote original guidance")
        summary["original_agent_guidance_preserved"] = True
        summary["upstream"] = execute(template, [python, "_aidd_gate_pilot_tests.py"], artifact / "upstream-tests.log")
        summary["upstream_tests"] = json.loads((template / ".aidd-gate/vecdiff-tests.json").read_text())
        if summary["upstream"]["exit_code"] != 0:
            raise AssertionError("Upstream baseline failed")
        summary["review_contract"] = execute(template, [python, "_aidd_gate_review_contract.py"], artifact / "review-contract.log")
        if summary["review_contract"]["exit_code"] != 0:
            raise AssertionError("Numeric review contract baseline failed")
        expectations = (
            ("baseline", 0, set(), 0),
            ("broken-import", 1, {"imports", "test-command", "review-1"}, 0),
            ("hallucinated-api", 1, {"test-command", "review-1"}, 0),
            ("empty-test", 1, {"test-quality"}, 0),
            ("wrong-exit-code", 1, {"test-command"}, 0),
            ("numeric-regression", 1, {"test-command", "review-1"}, 0),
            ("agents-drift", 1, {"harness.agents.drift"}, 1),
        )
        for name, expected, rules, lint_exit in expectations:
            case = artifact / name
            shutil.copytree(template, case, ignore=shutil.ignore_patterns(".aidd-gate", "__pycache__", "*.pyc", ".pytest_cache"))
            changed = mutate(case, name)
            check = gate(case, "check", artifact / f"{name}-check.log", python)
            lint = gate(case, "lint-config", artifact / f"{name}-lint.log", python)
            found = {f["rule"] for f in check["receipt"]["findings"]}
            checks = {c["id"]: c["status"] for c in check["receipt"]["checks"]}
            passed = check["exit_code"] == expected and found == rules and lint["exit_code"] == lint_exit
            if name == "empty-test":
                passed = passed and checks.get("test-command") == "passed"
            if name == "hallucinated-api":
                passed = passed and checks.get("imports") == "passed"
            result = {"name": name, "status": "passed" if passed else "failed", "changed_file": changed,
                      "expected_exit_code": expected, "expected_rules": sorted(rules), "check": check, "lint": lint}
            tests_path = case / ".aidd-gate/vecdiff-tests.json"
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
    parser.add_argument("--source", type=Path, required=True, help="Local vecdiff checkout at the reviewed commit")
    parser.add_argument("--python", default=sys.executable, help="Existing Python with numpy and pytest; no packages are installed")
    args = parser.parse_args()
    summary, output = run(args.source.resolve(), args.python)
    print(f"Pilot: {summary['status']}; source unchanged: {summary['source_unchanged']}")
    print("Evidence: " + output.relative_to(ROOT).as_posix())
    return 0 if summary["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
