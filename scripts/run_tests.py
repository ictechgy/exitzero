#!/usr/bin/env python3
"""Run the standard-library test suite directly from a source checkout."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
for source in sorted((ROOT / "packages").glob("*/src")):
    sys.path.insert(0, str(source))

if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.discover(str(ROOT / "tests")))
    raise SystemExit(not result.wasSuccessful())
