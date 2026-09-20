# Changelog

## 0.5.0 — unreleased

- Opt-in `node.test-integrity` detects removed literal-named JS/TS tests and new
  skip/focus markers against a trusted local Git commit. It preserves in-scope
  moves, checks duplicate title counts, and requires `reuse = false`. Unsupported
  declarations fail visibly; JSX, assertion quality and dynamic registration
  semantics remain outside its bounded lexical scope. No runtime dependency added.
- `init --profile node --test-integrity-base REF` generates the check and test
  paths without resolving/fetching Git refs or executing project commands.
- Extend the real p-limit pilot with a Git baseline and deletion/skip controls;
  extend installed-wheel acceptance with static Node fail/repair receipts.

## 0.4.0 — released 2026-09-20

- Review fixes: doctor rejects disabled project hooks; diff-scoped checks that
  did not execute cannot satisfy mapped requirements.
- Copilot CLI executable `agentStop`/manual `preToolUse` adapter and Git pre-push
  installation with clean-HEAD/ref validation, shared gate receipts and doctor.
- Opt-in MCP argument constraints for literal paths, exact HTTPS origins and
  enumerations; invalid arguments are denied before forwarding without logging
  values. Name-only behavior remains for tools without explicit rules.
- `report --run-id` selects an exact receipt. in-toto subject digests now cover
  the persisted file bytes (previously a canonical reserialization). Added an
  optional trusted-CI GitHub attestation recipe; no signing occurs locally.
- `init --profile node` connects existing test/lint/type-check commands with
  JS/TS, manifests, lockfiles and configuration inputs. Generation is offline,
  preserves existing policy, and never installs packages or runs commands.
- Per-check `enforcement = "warn" | "block"` separates observed failure from
  gate disposition. Receipts retain failed checks/requirements while marking
  advisory findings; execution, policy and receipt errors stay blocking.
- `plugin incident-kit` scaffolds deliberately failing regression/neighboring
  tests and connects quality/command checks plus requirement mappings to the
  existing policy, preserving manual AGENTS guidance and existing files.
- `doctor [--adapter NAME] [--format json]` diagnoses project policy/AGENTS
  drift and installed client/Git hook setup without running verification
  commands. It catches disabled Cursor `failClosed` after reinstall, invalid
  repair limits/timeouts and managed-only project hooks; receipts distinguish
  configuration observations from unverified runtime enforcement.
- English/Korean failure demos configure a real test command and retain failing
  and repaired receipts. Required-CI guidance covers artifacts, branch rules,
  trusted test-integrity baselines and the limit of local stop hooks.

## 0.3.0 — released 2026-09-19

**Multi-adapter hooks and completion coverage**

- `hooks install --adapter claude` writes a nested `hooks.Stop[].hooks[]`
  entry into `.claude/settings.json`; `--adapter codex` writes
  `.codex/hooks.json`. Both adapters answer `{}` on pass and
  `{"decision": "block", "reason": ...}` on any nonzero gate. Generated
  entries carry `failClosed: true` and `timeout: 120`; reinstalls prune
  stale exitzero entries without touching foreign hooks; lint tracks
  managed entries per digest and warns when `allowManagedHooksOnly`
  would disable the project gate.
- `python.test-integrity` compares worktree test files against a git
  `base` (default `HEAD`): deleted or moved-out test files, removed test
  cases, net assertion loss and new skip/xfail suppression markers are
  findings. Options: `allow_deletions`, `allow_skip_markers`,
  `max_removed_assertions`. Declares `reuse = false` — its correctness
  depends on moving git state outside hashed inputs.
- Per-check `reuse = false` in `[[checks]]` excludes a check from
  `--reuse` result caching while still recording its selected inputs.
- `check --diff REF` narrows every check's selection and the stability
  snapshot to paths changed against a git ref or range (including deleted
  paths, so scoped integrity checks still see removals); a scope with no
  changed files records a vacuous pass and the receipt carries `diff`.
- `report --format intoto` exports the latest receipt wrapped in an
  unsigned in-toto Statement v1 (`predicateType:
  https://exitzero.dev/attestations/gate/v1`).
- `harness-eval` turns record `transitions` from the second turn onward —
  `fail_to_pass`, `pass_to_pass`, `pass_to_fail`, `fail_to_fail` over
  check ids.
- `exitzero plugin mcp-gate` serves the completion gate over stdio
  JSON-RPC: the `check_completion` tool runs the real `check` and returns
  the verdict plus the receipt reference (gate failure is a verdict;
  operational exit 2 is a tool error). `agent-plugin/` packages it with a
  skill and `.mcp.json` for hosts without stop-hook support — advisory
  by design.

**Review-hardening follow-ups**

- `check --diff` keeps deleted paths in the changed set (scoped
  `python.test-integrity` can no longer be bypassed by deleting tests)
  and escapes glob metacharacters in narrowed literal paths; `Context`
  gains a `diff` field so plugins can treat empty selections as vacuous
  under scoped runs.
- Hook entry pruning requires the exitzero launcher and only removes
  groups emptied by pruning — foreign hook-shaped entries and pre-empty
  groups are preserved; `match_path` rejects candidates under excluded
  segments for `select_files` parity.

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
