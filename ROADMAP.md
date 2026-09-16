# Roadmap

- **v1 — core + verification + light harness:** TOML policy, generated AGENTS
  section, shared CLI/hook/CI runner, required JSON receipts, Python static checks,
  command-based review contracts, config lint, ten fixtures. Gateway and ledger
  enhancement packages contain explicit unavailable stubs.
- **v1.1 — deepen harness:** versioned fixture scoring for joint test and review
  constraints, additional rules/config formats, opt-in bounded multi-turn eval.
  Report failures and skipped scenarios separately from pass rates.
- **v1.2 — local MCP/tool gateway:** a thin local proxy, YAML tool allowlist,
  explicit authorization decisions and audit events via core hook interfaces.
  No cloud control plane or Kubernetes requirement.
- **v1.3 — reviewable ledger:** attach one run record to a PR, aggregate related
  receipts and propose rollback hints. Publishing and rollback actions remain
  explicit; core receipts continue to work without this plugin.

Before broad adoption: test real Cursor releases, improve Python import analysis,
add richer machine-output integrations, and measure check latency on larger repos.
No claim of replacing full static analysis, human review or commercial review tools.
