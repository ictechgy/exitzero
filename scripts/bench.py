#!/usr/bin/env python3
"""Measure check latency on a synthetic repository (or an existing one)."""
import argparse
import json
import statistics
import sys
import tempfile
import time
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
CLI = [sys.executable, str(ROOT / "bin/exitzero")]

POLICY = """version = 1
plugins = ["exitzero_verify"]

[[checks]]
id = "syntax"
kind = "python.syntax"
paths = ["src/**/*.py", "tests/**/*.py"]

[[checks]]
id = "imports"
kind = "python.imports"
paths = ["src/**/*.py"]

[checks.options]
roots = ["src"]

[[checks]]
id = "test-quality"
kind = "python.test-quality"
paths = ["tests/**/*.py"]
"""


def generate(root: Path, files: int, tests: int) -> int:
    """Create a package tree of cross-importing modules plus real tests."""

    (root / "exitzero.toml").write_text(POLICY, encoding="utf-8")
    packages = max(1, files // 25)
    base, remainder = divmod(files, packages)
    written = 0
    for package in range(packages):
        directory = root / "src" / f"pkg_{package}"
        directory.mkdir(parents=True)
        (directory / "__init__.py").write_text("", encoding="utf-8")
        written += 1
        for module in range(base + (1 if package < remainder else 0)):
            body = [f'"""Module {package}.{module}."""']
            if package:
                body.append(f"from pkg_{package - 1} import mod_0")
                body.append(f"PREVIOUS = mod_0.value_{package - 1}")
            body.append(f"def value_{package}(left: int) -> int:")
            body.append(f"    return left + {package}")
            body.append(f"CONSTANT_{module} = {module}")
            (directory / f"mod_{module}.py").write_text("\n".join(body) + "\n", encoding="utf-8")
            written += 1
    test_root = root / "tests"
    test_root.mkdir()
    for case in range(tests):
        (test_root / f"test_case_{case}.py").write_text(
            f"from pkg_0.mod_0 import value_0\n\n\n"
            f"def test_value_{case}():\n"
            f"    result = value_0({case})\n"
            f"    assert result == {case}\n",
            encoding="utf-8",
        )
    return written + tests


def time_check(root: Path, timeout: float) -> tuple[float, int]:
    started = time.perf_counter()
    try:
        result = subprocess.run([*CLI, "--root", str(root), "check", "--format", "json"],
                                capture_output=True, text=True, timeout=timeout)
        code = result.returncode
    except subprocess.TimeoutExpired:
        code = -1
    return time.perf_counter() - started, code


def run_benchmark(root: Path, repeats: int, timeout: float, synthetic: bool) -> tuple[list[dict], bool]:
    """Time each run; a nonzero exit is invalid for synthetic trees and fatal on 2."""

    runs: list[dict] = []
    operational_failure = False
    for run_index in range(repeats):
        elapsed, code = time_check(root, timeout)
        runs.append({"seconds": round(elapsed, 4), "exit_code": code})
        print(f"  run {run_index + 1}: {elapsed:.3f}s (exit {code})")
        if code == 2 or code < 0 or (synthetic and code != 0):
            operational_failure = True
    return runs, operational_failure


def write_report(report: dict) -> Path:
    directory = ROOT / ".exitzero" / "bench"
    directory.mkdir(parents=True, exist_ok=True)
    stamp = int(time.time())
    for attempt in range(100):
        suffix = "" if attempt == 0 else f"-{attempt}"
        destination = directory / f"bench-{stamp}{suffix}.json"
        try:
            with destination.open("x", encoding="utf-8") as handle:
                handle.write(json.dumps(report, indent=2, sort_keys=True) + "\n")
            return destination
        except FileExistsError:
            continue
    raise RuntimeError("could not allocate a unique bench report filename")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--files", type=int, default=400, help="Synthetic source files (default 400)")
    parser.add_argument("--tests", type=int, default=40, help="Synthetic test files (default 40)")
    parser.add_argument("--repeats", type=int, default=3, help="Timed check runs (default 3)")
    parser.add_argument("--timeout", type=float, default=600.0,
                        help="Per-run check timeout in seconds (default 600)")
    parser.add_argument("--root", type=Path, help="Existing repository to benchmark instead of a synthetic one")
    args = parser.parse_args()
    if args.files < 1 or args.tests < 1 or args.repeats < 1 or args.timeout <= 0:
        parser.error("--files, --tests and --repeats must be positive; --timeout must be > 0")

    if args.root is not None:
        root = args.root.resolve()
        if not (root / "exitzero.toml").is_file():
            parser.error("--root must contain an exitzero.toml policy")
        total = sum(1 for _ in root.rglob("*.py"))
        print(f"Benchmarking existing repository: {root} ({total} Python files)")
        runs, failed = run_benchmark(root, args.repeats, args.timeout, synthetic=False)
        report = {"mode": "existing", "root": str(root), "python_files": total,
                  "repeats": args.repeats, "runs": runs}
    else:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            generated = generate(root, args.files, args.tests)
            print(f"Benchmarking synthetic repository: {generated} files "
                  f"({args.files} sources, {args.tests} tests)")
            runs, failed = run_benchmark(root, args.repeats, args.timeout, synthetic=True)
            report = {"mode": "synthetic", "files": generated, "repeats": args.repeats,
                      "runs": runs}

    timings = [entry["seconds"] for entry in runs]
    report.update({"min": min(timings), "median": statistics.median(timings), "max": max(timings)})
    try:
        destination = write_report(report)
    except OSError as error:
        print(f"Could not persist the bench report: {error}")
        return 2
    print(f"min {report['min']:.3f}s  median {report['median']:.3f}s  max {report['max']:.3f}s")
    print(f"Report: {destination.relative_to(ROOT)}")
    if failed:
        print("One or more check runs failed operationally; timings are not trustworthy.")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
