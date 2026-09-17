# Changelog

## Unreleased

Security and reliability hardening from a three-track review of the gateway,
ledger, core and plugins:

- `mcp-gateway`: a non-zero or killed upstream now exits 2 instead of 0;
  the config is hashed and parsed from one byte snapshot; JSON-RPC frames
  are bounded at 4 MiB and outstanding client requests at 1024; duplicate
  and null request ids are handled without evicting in-flight entries;
  workers are joined on shutdown, client writes are serialized, and a
  failed first audit write can no longer leak the upstream process.
- `ledger-publish`: the PR body is piped to `gh` over stdin instead of a
  re-opened repository path; `--base` validation disables partial-clone
  lazy fetch; receipts are trimmed to the fields aggregation needs;
  hostile finding paths (NUL, control characters) are rejected before
  they reach `git` argv or the Markdown body, and commit lookups batch
  under the argv limit.
- Core: `exitzero.toml`, `.exitzero/hooks.json` and ledger receipts must be
  regular files (FIFOs can no longer block a gate); `init --sync`, hook
  installation and manifest writes are atomic renames, which also prevents
  hard-link write-through; pre-commit Git probes have timeouts; deeply
  nested Cursor hook JSON exits 2 with a receipt instead of crashing; hook
  launchers use `python -P -m exitzero` so a repository's own `exitzero/`
  directory cannot shadow the installed package; human output escapes
  control characters in finding paths and messages.
- Performance: `select_files` walks with `os.walk`, pruning excluded and
  credential directories before descent instead of globbing the whole
  tree; input hashing streams in 1 MiB chunks; `python.imports` parses each
  local module once per run; harness rule lint and eval reports no longer
  retain unbounded per-turn data.
- Plugin contract: `exitzero.services` is the documented import surface
  (path safety, managed-section rendering, gate execution); plugins declare
  their own `API_VERSION` literal.
- `scripts/run_fixtures.py` accepts `--timeout SECONDS` (default 120, above
  the 60-second command budget) and kills the whole process group on
  timeout so a hung command check cannot leave children behind.

## 0.2.0 — released 2026-09-16

**Roadmap tail**

- `check`, `lint-config` and `report` accept `--format sarif`: findings are
  emitted as a SARIF 2.1.0 run for code-scanning integrations.
- `python.imports` no longer reports imports guarded by
  `except ImportError`/`ModuleNotFoundError` (the optional-dependency
  pattern), and resolves source modules in O(1) instead of scanning the
  module index per file — the gate is ~20x faster on a 2,000-file tree.
- `scripts/bench.py` measures check latency on a generated synthetic
  repository (default) or any policy root via `--root`; reports persist
  under `.exitzero/bench/`.

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
