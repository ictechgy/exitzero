<!-- aidd-gate:begin -->
## aidd-gate policy

Generated from policy. Edit the TOML, then run `aidd-gate init --sync`.
Run `aidd-gate check` before merge; keep the JSON receipt as evidence.
Run `aidd-gate lint-config` after changing agent configuration.

Required checks:
- `syntax`: `python.syntax` (src/**/*.py, tests/**/*.py)
- `imports`: `python.imports` (src/**/*.py, tests/**/*.py)
- `test-quality`: `python.test-quality` (tests/**/*.py)

Policy SHA-256: `b9bdf4fc394d81a1e9fefce8f7c428a9781f09ef76bbecfbe721c02ddf5af9c1`
<!-- aidd-gate:end -->
