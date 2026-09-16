<!-- exitzero:begin -->
## exitzero policy

Generated from policy. Edit the TOML, then run `exitzero init --sync`.
Run `exitzero check` before merge; keep the JSON receipt as evidence.
Run `exitzero lint-config` after changing agent configuration.

Required checks:
- `syntax`: `python.syntax` (src/**/*.py, tests/**/*.py)
- `imports`: `python.imports` (src/**/*.py, tests/**/*.py)
- `test-quality`: `python.test-quality` (tests/test_*.py)
- `review-contracts`: `command` (src/**/*.py, tests/**/*.py)
- Rule `review-contract`: Assert exact error text, exception type, return type, ordering and deduplication.

Policy SHA-256: `264539cc549982c4d043caa2e9e8b6d77fbe38c9a7cefc18be18349f20339d30`
<!-- exitzero:end -->
