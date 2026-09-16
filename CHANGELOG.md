# Changelog

## Unreleased

**v1.3**

- `exitzero plugin ledger-publish` aggregates `.exitzero/runs/` receipts
  into a versioned run record (`.exitzero/ledger/record-*.json` + Markdown
  body), ranks paths implicated by non-passing runs, and lists recent
  commits touching them (`--since`, `--base`). `--pr N` posts the body via
  `gh pr comment` — the only external write, always explicit.

**v1.2**

- `exitzero plugin mcp-gateway --config PATH` is a thin local MCP gateway:
  it spawns one upstream MCP server and proxies stdio JSON-RPC, authorizes
  `tools/call` against TOML allow/deny glob patterns (deny-by-default), drops
  denied tools from `tools/list`, and appends every decision to
  `.exitzero/mcp-gateway/audit-*.jsonl`.

**v1.1**

- Each fixture now declares versioned expectations in `fixture.toml`
  (`schema_version = 1`); the runner reads manifests instead of a hardcoded
  case table, and `skip = "reason"` cases are reported separately from
  pass/fail counts.
- Config lint now covers Claude-style hook documents (`matcher` + nested
  `hooks`), TOML `mcp_servers` tables, empty hook slots, and `./`-relative
  hook commands that do not resolve to a file.
- New fixtures: joint test-quality + review-contract failure, declared
  live-Cursor skip, and valid/invalid new-format config cases.
- `exitzero plugin harness-eval --scenario PATH` replays bounded scripted
  multi-turn scenarios against the real gate in a temporary copy; per-turn
  exit/rule expectations are scored, skipped scenarios report separately,
  and reports persist under `.exitzero/evals/`.

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
