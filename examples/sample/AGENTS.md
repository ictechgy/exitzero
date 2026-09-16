<!-- aidd-gate:begin -->
## aidd-gate policy

Generated from policy. Edit the TOML, then run `aidd-gate init --sync`.
Run `aidd-gate check` before merge; keep the JSON receipt as evidence.
Run `aidd-gate lint-config` after changing agent configuration.

Required checks:
- `syntax`: `python.syntax` (src/**/*.py, tests/**/*.py)
- `imports`: `python.imports` (src/**/*.py, tests/**/*.py)
- `test-quality`: `python.test-quality` (tests/test_*.py)
- `review-contracts`: `command` (src/**/*.py, tests/**/*.py)
- Rule `review-contract`: Assert exact error text, exception type, return type, ordering and deduplication.

Policy SHA-256: `c81af5772be8cb270bd470225d2b73cb5e837b99c2d25e2bca90af8cf00f083a`
<!-- aidd-gate:end -->
