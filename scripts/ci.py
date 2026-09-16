#!/usr/bin/env python3
"""The same offline milestone runner is used locally and in GitHub Actions."""
import json
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
CLI = [sys.executable, str(ROOT / "bin/exitzero")]
sys.path.insert(0, str(ROOT / "packages/core/src"))
from exitzero.files import safe_path


def main() -> int:
    artifacts = safe_path(ROOT, ".exitzero")
    artifacts.mkdir(exist_ok=True)
    jobs = [
        ("repository-check", [*CLI, "check", "--format", "json"]),
        ("repository-lint", [*CLI, "lint-config", "--format", "json"]),
        ("sample-check", [*CLI, "--root", str(ROOT / "examples/sample"), "check", "--format", "json"]),
        ("sample-lint", [*CLI, "--root", str(ROOT / "examples/sample"), "lint-config", "--format", "json"]),
        ("fixtures", [sys.executable, str(ROOT / "scripts/run_fixtures.py")]),
    ]
    results = []
    for name, command in jobs:
        started = time.monotonic()
        try:
            process = subprocess.run(command, cwd=ROOT, text=True, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, timeout=180)
            code, output = process.returncode, process.stdout
        except subprocess.TimeoutExpired:
            code, output = 2, "CI job exceeded 180 seconds\n"
        log = safe_path(ROOT, f".exitzero/{name}.log")
        log.write_text(output, encoding="utf-8")
        print(f"{name}: {'PASS' if code == 0 else 'FAIL'} (exit {code})", flush=True)
        if name == "fixtures" or code != 0:
            print(output, end="", flush=True)
        results.append({"job": name, "exit_code": code, "duration_ms": round((time.monotonic() - started) * 1000, 3),
                        "log": log.relative_to(ROOT).as_posix()})
    success = all(result["exit_code"] == 0 for result in results)
    summary = {"schema_version": 1, "status": "passed" if success else "failed", "jobs": results}
    safe_path(ROOT, ".exitzero/ci-results.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print("Evidence: .exitzero/ci-results.json")
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
