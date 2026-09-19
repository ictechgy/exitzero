# Roadmap

- **v1 — core + verification + light harness:** TOML policy, generated AGENTS
  section, shared CLI/hook/CI runner, required JSON receipts, Python static checks,
  command-based review contracts, config lint, ten fixtures. Gateway and ledger
  enhancement packages contain explicit unavailable stubs.
- **v1.1 — deepen harness:** versioned fixture scoring for joint test and review
  constraints, additional rules/config formats, opt-in bounded multi-turn eval.
  Report failures and skipped scenarios separately from pass rates.
  Shipped: `fixture.toml` manifests (schema_version 1) score joint
  check/lint expectations per fixture, skipped cases are reported separately
  from pass rates, config lint covers Claude hook documents, TOML
  `mcp_servers`, empty hook slots and repo-relative hook command paths, and
  `harness-eval` replays bounded scripted multi-turn scenarios (see
  `examples/eval-repair`).
- **v1.2 — local MCP/tool gateway:** a thin local proxy, TOML tool allowlist,
  explicit authorization decisions and audit events under `.exitzero/`.
  No cloud control plane or Kubernetes requirement.
  Shipped: `exitzero plugin mcp-gateway --config PATH` spawns one upstream
  MCP server as a subprocess and proxies newline-delimited JSON-RPC on stdio;
  `tools/call` is authorized against allow/deny glob patterns
  (deny-by-default, deny wins), `tools/list` responses drop denied tools, and
  every decision is appended to `.exitzero/mcp-gateway/audit-*.jsonl`.
- **v1.3 — reviewable ledger:** attach one run record to a PR, aggregate related
  receipts and propose rollback hints. Publishing and rollback actions remain
  explicit; core receipts continue to work without this plugin.
  Shipped: `exitzero plugin ledger-publish` aggregates `.exitzero/runs/`
  receipts into `.exitzero/ledger/` JSON+Markdown run records, ranks
  implicated paths from non-passing runs, lists recent commits touching them
  (`--base` scopes the range), and posts the body via `gh pr comment` only
  when `--pr N` is passed explicitly.

Before broad adoption: test real Cursor releases, improve Python import analysis,
add richer machine-output integrations, and measure check latency on larger repos.
No claim of replacing full static analysis, human review or commercial review tools.

## Post-0.3.0 source additions

Implemented: project doctor, incident regression kits, check-level warn/block
policy, Node command profiles, Copilot CLI and Git pre-push adapters, constrained
MCP argument rules, exact receipt exports and optional CI attestation guidance.
These remain source features until the next package release. Copilot has protocol
tests but no live-session evidence; the attestation workflow is an example, not
an enabled hosted signing deployment. Cloud control planes, generic shell-command
inspection and automatic rollback remain outside this implementation.
