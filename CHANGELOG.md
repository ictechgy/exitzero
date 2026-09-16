# Changelog

## Unreleased — v1.1 in progress

- Each fixture now declares versioned expectations in `fixture.toml`
  (`schema_version = 1`); the runner reads manifests instead of a hardcoded
  case table, and `skip = "reason"` cases are reported separately from
  pass/fail counts.
- Config lint now covers Claude-style hook documents (`matcher` + nested
  `hooks`), TOML `mcp_servers` tables, empty hook slots, and `./`-relative
  hook commands that do not resolve to a file.
- New fixtures: joint test-quality + review-contract failure, declared
  live-Cursor skip, and valid/invalid new-format config cases.

## 0.1.0 — released 2026-09-16

- Policy-driven CLI with `init`, `check`, `lint-config`, hook installation and
  receipt reporting; a small core discovers trusted local plugins.
- Python syntax, local import/API wiring, obvious empty/vacuous test checks,
  and shell-free command checks for executable review requirements.
- Managed AGENTS sections generated from the same TOML policy; config and
  installed-hook drift checks.
- Python setup profile with source roots, external module names, test commands
  and repeatable review commands.
- Cursor adapters, generic CI slots and a Git pre-commit adapter; every gate run
  requires a persisted JSON receipt.
- Ten positive/negative fixture cases, plus pinned local riskgate and vecdiff
  pilots with independent fault injections and preserved evidence.
- Offline wheel verification and actual Git commit acceptance/rejection tests.
- MIT license; gateway and richer PR-ledger features remain roadmap stubs.

Published to PyPI via trusted publishing (GitHub Actions OIDC) from tag
`v0.1.0`, after hosted CI and offline wheel/Git-hook verification in the
release workflow.
