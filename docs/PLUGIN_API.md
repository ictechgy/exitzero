# Plugin API, version 1

One executable loads the modules listed in `plugins` in `exitzero.toml`.
Installed third-party plugins may also expose the `exitzero.plugins` entry-point
group; the policy selects entry-point names explicitly. Discovery does not execute
unselected plugins. Plugins are trusted local Python code, not a sandbox.

A plugin exports `API_VERSION = 1` and `register(registry)`:

```python
from exitzero.api import Finding

API_VERSION = 1

def check(context, spec):
    return []  # Or Finding(spec.id, "Actionable error", "src/example.py", 10).

def register(registry):
    registry.add_check("example.check", check)
```

`Context` contains `root`, parsed `policy`, and `policy_path`. `CheckSpec` contains
`id`, `kind`, `paths` and an `options` table. Check and lint handlers return a list
of `Finding`. Exceptions become operational errors (exit 2); they cannot pass a
run. Findings must not include file contents, credentials or command output.

Registration methods:

- `add_check(kind, handler(context, spec))`
- `add_linter(name, handler(context))`
- `add_hook(slot, handler(context, slot))`, for `PreToolUse`, `PostToolUse`,
  `pre-commit`, `CI`.
- `add_command(name, handler(context, argv))`, called with `exitzero plugin NAME`.

Core owns policy parsing, the registry, dispatch, generated AGENTS section, hook
adapters and receipt persistence. Plugins own check semantics. `check` runs
registered configuration linters before verification checks. `lint-config` runs
linters without executing check commands. Hook slots use the same policy and
runner; extra plugin handlers run after its checks.

Shared helpers: plugins may import `exitzero.api` and `exitzero.services`;
every other core module is internal and outside the `API_VERSION` contract.
`exitzero.services.select_files(root, patterns)` returns sorted, deduplicated
repository files; it rejects absolute paths, traversal, symlinks and
secret-like paths. `exitzero.services.render_agents(policy)` returns the
complete managed section, with `<!-- exitzero:begin -->` and
`<!-- exitzero:end -->`; `run_gate(root, policy_name, command)` executes one
gate run (used by the harness eval replayer). A plugin declares the contract
it was written against with its own `API_VERSION = 1` literal — it must not
re-export core's, which could advertise a contract it never verified.
Plugins should validate their own `spec.options` and raise `ValueError` on
invalid settings. Invalid settings must never silently disable a check.

Verification kinds in v1:

- `python.syntax`: parse selected Python files.
- `python.imports`: statically check import resolution and local module symbols;
  options `roots` (default `["."]`), `allow_modules` (default `[]`). Imports
  inside `try` bodies guarded by `except ImportError`/`ModuleNotFoundError`
  (the optional-dependency pattern) are skipped.
- `python.test-quality`: reject missing/empty/obviously vacuous test cases.
- `command`: execute `options.argv` without a shell; `{python}` expands to the
  current Python interpreter. `options.timeout` defaults to 30 seconds.

Harness registration: one `harness.config` linter compares the managed AGENTS
section to `render_agents(policy)`, checks installed hook drift, validates
explicitly listed `harness.config_files`, and reports `harness.rules` entries
that contradict or duplicate each other. It must never read secret-like paths
or execute config values.

`exitzero plugin harness-eval --scenario PATH` replays a bounded scripted
evaluation: PATH holds a `scenario.toml` (`schema_version = 1`, optional
`description`, `max_turns`, `skip`) and `turns/*/turn.toml` files declaring
`expect` (`exit`, `rules`), an optional `delete` list and a `note`. Files
beside `turn.toml` overlay a temporary copy — of the scenario's own `base/`
mini-repository when present, else the invocation root — before each turn's
real `check` run. Turn counts are bounded (default and absolute cap 64),
mismatches fail the scenario, skipped scenarios report separately, and the
report persists under `.exitzero/evals/`.

Listed config files are linted by suffix. `.json` documents recognize Cursor
hook entries (`command`/`prompt` fields, `version = 1` required) and Claude
hook entries (`matcher` plus a nested `hooks` list); empty hook slots and
`./`-relative commands that do not resolve to a file are findings.
`mcpServers` objects are validated too. `.toml` documents must contain an
`mcp_servers` table, which follows the same command-or-url and typed-option
rules.

`exitzero plugin mcp-gateway --config PATH` runs a thin local MCP gateway.
PATH selects a TOML document inside the repository:

```toml
schema_version = 1
description = "optional"

[upstream]
command = "python3"            # required; spawned without a shell
args = ["-m", "example_server"] # optional list of strings
cwd = "subdir"                  # optional repo-relative working directory
[upstream.env]                  # optional string map merged over the environment

[allow]
tools = ["read_*", "get_status"] # required glob patterns
[deny]
tools = ["exec_*"]               # optional; deny always wins
```

The gateway spawns the upstream MCP server as a subprocess and proxies
newline-delimited JSON-RPC between the client's stdio and the server. Only
`tools/call` is gated: a tool matching `[deny]` or matching nothing in
`[allow]` is answered with a JSON-RPC `-32000` error and never forwarded —
deny-by-default. `tools/list` responses drop denied tools before reaching the
client; every other message is forwarded verbatim. Each decision is appended
to `.exitzero/mcp-gateway/audit-<run_id>.jsonl` (`session_start`, `tool_call`,
`tools_filtered`, `session_end` events). The session ends when the client
closes stdin; a clean session exits 0 while configuration, spawn, audit or
premature-upstream failures exit 2. The gateway never inspects tool
arguments, never contacts the network, and is stdio-only.

`exitzero plugin ledger-publish` aggregates `.exitzero/runs/` receipts into
one run record under `.exitzero/ledger/` — a versioned JSON record plus a
Markdown body fit for a PR comment. Flags: `--since ISO8601` filters receipts
by `started_at`; `--base REF` scopes the git history searched for suspect
commits; `--pr N` posts the Markdown via `gh pr comment` and is the only
external write — publishing stays explicit, and rollback hints (implicated
paths plus recent commits touching them) stay read-only suggestions. The
record reports aggregate counts, per-rule findings, corrupt receipts, and run
references; it exits 0 on success and 2 on usage or operational failure —
it is a report, not a gate.
