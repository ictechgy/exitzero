"""Run the pinned riskgate suite with explicit external-integration exclusions.

The pilot copies this file to the riskgate snapshot root. No upstream test files
are edited. This is a test selection adapter, not a process or network sandbox.
"""
import json
from pathlib import Path
import sys
import unittest


def main() -> int:
    root = Path(__file__).resolve().parent
    sys.path.insert(0, str(root))
    suite = unittest.defaultTestLoader.discover(str(root / "tests"), top_level_dir=str(root))
    from tests.test_prototype_parity import PrototypeParityTest
    from tests.test_emit import Agent2PerfettoValidatorTest

    for case, reason in (
        (PrototypeParityTest, "Pilot excludes the personal prototype outside the pinned repository"),
        (Agent2PerfettoValidatorTest, "Pilot excludes an unpinned sibling checkout"),
    ):
        case.__unittest_skip__ = True
        case.__unittest_skip_why__ = reason

    result = unittest.TextTestRunner(verbosity=2).run(suite)
    summary = {"tests_run": result.testsRun, "failures": len(result.failures),
               "errors": len(result.errors), "skipped": [{"test": test.id(), "reason": reason}
                                                        for test, reason in result.skipped],
               "successful": result.wasSuccessful() and result.testsRun > len(result.skipped)}
    artifacts = root / ".exitzero"
    artifacts.mkdir(exist_ok=True)
    (artifacts / "riskgate-tests.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return 0 if summary["successful"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
