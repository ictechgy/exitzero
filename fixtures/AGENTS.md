# Fixture Guidance

- These directories are test data: syntax errors, absent APIs, empty tests,
  invalid policy and document drift are intentional failure cases.
- Keep case expectations in `scripts/run_fixtures.py` aligned with each fixture.
  Preserve both positive controls and negative cases.
- Preserve existing generated AGENTS marker blocks. Do not bulk-run policy sync
  across fixtures: `09-agents-drift` must drift and `10-invalid-policy` must fail
  policy parsing. Deeper fixture AGENTS files contain their policy examples.
- Use `python3 scripts/run_fixtures.py` from the project root. It runs temporary
  copies and verifies process exits, findings and saved-receipt equality.
- Keep receipts and stdout/stderr when a case fails expectations. Use synthetic
  inputs; do not copy credentials or real agent transcripts into fixtures.
