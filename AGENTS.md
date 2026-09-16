# Agent Instructions

## Global Rules

- Read the local `HANDOFF.md` when present for current evidence, decisions and
  pending authorization. Keep changing session state there; this file holds
  durable project rules.
- Keep one user-facing tool with a small core and separate plugins. Core owns
  policy, discovery, hook dispatch and receipts; plugins own check behavior.
  Follow [the plugin contract](docs/PLUGIN_API.md).
- v1 ships verification and configuration lint. Keep full agent evaluation,
  MCP gateway and PR publishing/rollback features at the roadmap stages in
  [ROADMAP.md](ROADMAP.md). Cursor is one adapter; the CLI and CI stay IDE-agnostic.
- Use Python 3.11+ and no runtime dependencies. Ask before introducing a large
  dependency or expanding scope. Preserve existing user changes and make small,
  cohesive commits. Keep comments technical and docs plain; short Korean/English
  sections are welcome.
- Treat runner exits and persisted receipts as completion evidence. A passing
  test command alone does not prove review requirements or test quality.
- Keep credentials, personal machine paths, local pilot copies and build output
  out of commits. Network access, credential reads and external writes follow
  the user's approved scope; public-document lookup is not publication approval.
- Preserve the generated section below. Change `exitzero.toml` and run
  `./bin/exitzero init --sync`; keep manual guidance outside its paired markers.

## Scoped Guidance Index

These links are a discoverability index. Child files govern their directories
and descendants; links do not extend their scope.

- [fixtures/AGENTS.md](fixtures/AGENTS.md) — intentional failures and fixture evidence.
- [scripts/AGENTS.md](scripts/AGENTS.md) — isolated pilots and release verification.
- [examples/sample/AGENTS.md](examples/sample/AGENTS.md) — generated sample policy.

## Verification

- Use `python3 scripts/run_tests.py` for focused implementation work and
  `python3 scripts/ci.py` for the local acceptance sequence. A fixture with an
  expected nonzero exit is successful only when the runner confirms that result.
- For guidance changes, verify links and marker preservation, then run
  `./bin/exitzero lint-config --format json`. Preserve its receipt.
- For package changes, follow [the release recipe](docs/RELEASE.md). Distinguish
  source tests, installed-wheel/Git-hook evidence, hosted CI and live IDE testing.
- Reuse valid evidence for unchanged inputs. Record skipped tests and reasons;
  never report them as passed or treat local CI as hosted CI.

<!-- exitzero:begin -->
## exitzero policy

Generated from policy. Edit the TOML, then run `exitzero init --sync`.
Run `exitzero check` before merge; keep the JSON receipt as evidence.
Run `exitzero lint-config` after changing agent configuration.

Required checks:
- `syntax`: `python.syntax` (packages/**/*.py, scripts/*.py, tests/test_*.py)
- `imports`: `python.imports` (packages/**/*.py)
- `test-quality`: `python.test-quality` (tests/test_*.py)
- `regression-suite`: `command` (packages/**/*.py, scripts/run_tests.py, tests/test_*.py)
- Rule `completion-evidence`: Require runner exit codes and persisted receipts before claiming completion.

Policy SHA-256: `fe49f34f0ee0d5df2de36e3916084977f2bc5f31d1e397ef351459dde9e3733a`
<!-- exitzero:end -->
