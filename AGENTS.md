<!-- aidd-gate:begin -->
## aidd-gate policy

Generated from policy. Edit the TOML, then run `aidd-gate init --sync`.
Run `aidd-gate check` before merge; keep the JSON receipt as evidence.
Run `aidd-gate lint-config` after changing agent configuration.

Required checks:
- `syntax`: `python.syntax` (packages/**/*.py, scripts/*.py, tests/test_*.py)
- `imports`: `python.imports` (packages/**/*.py)
- `test-quality`: `python.test-quality` (tests/test_*.py)
- `regression-suite`: `command` (packages/**/*.py, scripts/run_tests.py, tests/test_*.py)
- Rule `completion-evidence`: Require runner exit codes and persisted receipts before claiming completion.

Policy SHA-256: `66c938e841a262a70e07786180885beed5be8b51a7e423f2811d8e85873c3ccf`
<!-- aidd-gate:end -->
