"""Run the pinned vecdiff tests with a structured pytest result record."""
import importlib.metadata
import json
import os
from pathlib import Path
import sys


class Results:
    def __init__(self):
        self.counts = {"passed": 0, "failed": 0, "errors": 0, "skipped": 0}
        self.skips = []

    def pytest_runtest_logreport(self, report):
        if report.skipped:
            self.counts["skipped"] += 1
            self.skips.append(report.nodeid)
        elif report.failed:
            self.counts["failed" if report.when == "call" else "errors"] += 1
        elif report.when == "call":
            self.counts["passed"] += 1

    def pytest_collectreport(self, report):
        if report.failed:
            self.counts["errors"] += 1
        elif report.skipped:
            self.counts["skipped"] += 1
            self.skips.append(report.nodeid)


def main() -> int:
    root = Path(__file__).resolve().parent
    sys.path.insert(0, str(root / "src"))
    os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    import pytest
    import vecdiff

    if not Path(vecdiff.__file__).resolve().is_relative_to(root / "src"):
        raise RuntimeError("Tests must load the snapshot, not an installed vecdiff checkout")
    recorder = Results()
    code = int(pytest.main([str(root / "tests"), "-q", "-p", "no:cacheprovider"], plugins=[recorder]))
    versions = {}
    for name in ("numpy", "pytest", "faiss-cpu"):
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    summary = {"exit_code": code, **recorder.counts, "skipped_tests": recorder.skips, "dependencies": versions}
    artifacts = root / ".aidd-gate"
    artifacts.mkdir(exist_ok=True)
    (artifacts / "vecdiff-tests.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
