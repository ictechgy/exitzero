# Changelog

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
