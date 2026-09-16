#!/usr/bin/env python3
"""Run the ten repository fixtures against the checkout's CLI.

The fixtures are copied into temporary repositories so a run never changes a
fixture.  Receipts are copied out before cleanup and the summary is written in
the checkout's own ledger area for CI and human inspection.
"""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import Any
import uuid


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures"
CLI = ROOT / "bin" / "aidd-gate"


CASES: tuple[dict[str, Any], ...] = (
    {"name": "01-valid", "check": 0, "check_rules": [], "lint": 0, "lint_rules": []},
    {"name": "02-syntax-error", "check": 1, "check_rules": ["syntax"], "lint": 0, "lint_rules": []},
    {"name": "03-missing-import", "check": 1, "check_rules": ["imports"], "lint": 0, "lint_rules": []},
    {"name": "04-hallucinated-api", "check": 1, "check_rules": ["imports"], "lint": 0, "lint_rules": []},
    {"name": "05-empty-test", "check": 1, "check_rules": ["test-quality"], "lint": 0, "lint_rules": []},
    {"name": "06-vacuous-test", "check": 1, "check_rules": ["test-quality"], "lint": 0, "lint_rules": []},
    {"name": "07-review-contract-pass", "check": 0, "check_rules": [], "lint": 0, "lint_rules": []},
    {"name": "08-review-contract-fail", "check": 1, "check_rules": ["review-contract"], "lint": 0, "lint_rules": []},
    {"name": "09-agents-drift", "check": 1, "check_rules": ["harness.agents.drift"], "lint": 1, "lint_rules": ["harness.agents.drift"]},
    {"name": "10-invalid-policy", "check": 2, "check_rules": ["core.error"], "lint": 2, "lint_rules": ["core.error"]},
)


def invoke(root: Path, command: str) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
    process = subprocess.run(
        [sys.executable, str(CLI), "--root", str(root), command, "--format", "json"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    try:
        payload = json.loads(process.stdout)
    except json.JSONDecodeError as error:
        return process, {"_json_error": f"{type(error).__name__}: {error}"}
    if not isinstance(payload, dict):
        return process, {"_json_error": f"{command} JSON output is not an object"}
    return process, payload


def safe_output_path(relative: str) -> Path:
    """Resolve runner output without following symlinks out of the checkout."""

    candidate = Path(relative)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError("runner output path must remain inside the checkout")
    current = ROOT
    for part in candidate.parts:
        current /= part
        if current.is_symlink():
            raise ValueError(f"runner output path contains a symlink: {relative}")
    resolved = current.resolve(strict=False)
    if resolved != ROOT and ROOT not in resolved.parents:
        raise ValueError("runner output path escapes the checkout")
    return current


def prepare_outputs() -> None:
    safe_output_path(".aidd-gate").mkdir(parents=True, exist_ok=True)
    safe_output_path(".aidd-gate/fixture-receipts").mkdir(parents=True, exist_ok=True)


def verify_run(
    process: subprocess.CompletedProcess[str],
    payload: dict[str, Any],
    expected_exit: int,
    expected_rules: list[str],
    command: str,
    temporary_root: Path,
    case_name: str,
) -> dict[str, Any]:
    errors: list[str] = []
    evidence: dict[str, Any] = {
        "exit_code": process.returncode,
        "stdout": process.stdout,
        "stderr": process.stderr,
    }
    receipt: dict[str, Any] | None = None
    receipt_name = payload.get("receipt")
    receipt_path = temporary_root / receipt_name if isinstance(receipt_name, str) else None
    if receipt_path is None or not receipt_path.is_file():
        errors.append(f"{command} did not return an existing receipt path")
    else:
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            errors.append(f"{command} receipt could not be read: {type(error).__name__}")
        else:
            evidence["receipt"] = receipt
            run_id = receipt.get("run_id")
            if not isinstance(run_id, str) or not run_id:
                errors.append(f"{command} receipt has no run_id")
                run_id = f"missing-{uuid.uuid4().hex}"
            destination = safe_output_path(f".aidd-gate/fixture-receipts/{case_name}-{command}-{run_id}.json")
            shutil.copy2(receipt_path, destination)
            evidence["receipt_path"] = destination.relative_to(ROOT).as_posix()

            # Receipt copying deliberately precedes expectation checks, so a
            # mismatched expected result still leaves durable evidence behind.
            if receipt != payload:
                errors.append(f"{command} output does not match its saved receipt")
            if receipt.get("command") != command or receipt.get("exit_code") != process.returncode:
                errors.append(f"{command} receipt identity is inconsistent")
            if receipt.get("run_id") not in receipt_path.name:
                errors.append(f"{command} receipt run_id does not match its path")
            findings = receipt.get("findings")
            if not isinstance(findings, list):
                errors.append(f"{command} receipt findings are not a list")
            else:
                rules = [finding.get("rule") for finding in findings if isinstance(finding, dict)]
                if sorted(rules) != sorted(expected_rules):
                    errors.append(f"{command} rules {rules!r}, expected {expected_rules!r}")
            expected_status = {0: "passed", 1: "failed", 2: "error"}.get(expected_exit)
            if process.returncode != expected_exit:
                errors.append(f"{command} exit {process.returncode}, expected {expected_exit}")
            if expected_status is None or receipt.get("status") != {0: "passed", 1: "failed", 2: "error"}.get(process.returncode):
                errors.append(f"{command} status is inconsistent with its exit code")
    if "_json_error" in payload:
        errors.append(f"{command} did not produce JSON: {payload['_json_error']}")
    if receipt is not None:
        evidence["receipt"] = receipt
    if errors:
        evidence["errors"] = errors
    return evidence


def run_case(case: dict[str, Any]) -> dict[str, Any]:
    name = str(case["name"])
    source = FIXTURES / name
    if not source.is_dir():
        raise AssertionError(f"fixture directory is missing: {source}")
    with tempfile.TemporaryDirectory(prefix=f"aidd-gate-{name}-") as temporary:
        temporary_root = Path(temporary)
        shutil.copytree(source, temporary_root, dirs_exist_ok=True)
        check_process, check_payload = invoke(temporary_root, "check")
        check_evidence = verify_run(
            check_process,
            check_payload,
            int(case["check"]),
            list(case["check_rules"]),
            "check",
            temporary_root,
            name,
        )
        lint_process, lint_payload = invoke(temporary_root, "lint-config")
        lint_evidence = verify_run(
            lint_process,
            lint_payload,
            int(case["lint"]),
            list(case["lint_rules"]),
            "lint-config",
            temporary_root,
            name,
        )
        errors = check_evidence.get("errors", []) + lint_evidence.get("errors", [])
        return {
            "name": name,
            "status": "FAIL" if errors else "PASS",
            "check": check_evidence,
            "lint_config": lint_evidence,
            **({"errors": errors} if errors else {}),
        }


def main() -> int:
    prepare_outputs()
    results: list[dict[str, Any]] = []
    for case in CASES:
        name = str(case["name"])
        try:
            result = run_case(case)
        except (AssertionError, OSError, subprocess.SubprocessError, ValueError, json.JSONDecodeError) as error:
            result = {"name": name, "status": "FAIL", "error": f"{type(error).__name__}: {error}"}
        results.append(result)
        print(f"{name}: {result['status']}")
    summary = {
        "schema_version": 1,
        "tool": "aidd-gate fixture runner",
        "fixture_count": len(CASES),
        "passed": sum(result["status"] == "PASS" for result in results),
        "failed": sum(result["status"] == "FAIL" for result in results),
        "results": results,
    }
    summary_path = safe_output_path(".aidd-gate/fixture-results.json")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(f"Summary: {summary_path}")
    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
