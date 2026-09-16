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

Shared helpers: `exitzero.files.select_files(root, patterns)` returns sorted,
deduplicated repository files; it rejects absolute paths, traversal, symlinks and
secret-like paths. `exitzero.policy.render_agents(policy)` returns the complete
managed section, with `<!-- exitzero:begin -->` and `<!-- exitzero:end -->`.
Plugins should validate their own `spec.options` and raise `ValueError` on invalid
settings. Invalid settings must never silently disable a check.

Verification kinds in v1:

- `python.syntax`: parse selected Python files.
- `python.imports`: statically check import resolution and local module symbols;
  options `roots` (default `["."]`), `allow_modules` (default `[]`).
- `python.test-quality`: reject missing/empty/obviously vacuous test cases.
- `command`: execute `options.argv` without a shell; `{python}` expands to the
  current Python interpreter. `options.timeout` defaults to 30 seconds.

Harness registration: one `harness.config` linter compares the managed AGENTS
section to `render_agents(policy)`, checks installed hook drift, validates
explicitly listed `harness.config_files`, and reports `harness.rules` entries
that contradict or duplicate each other. It must never read secret-like paths
or execute config values. `harness-eval` is a CLI stub that exits 2.

Listed config files are linted by suffix. `.json` documents recognize Cursor
hook entries (`command`/`prompt` fields, `version = 1` required) and Claude
hook entries (`matcher` plus a nested `hooks` list); empty hook slots and
`./`-relative commands that do not resolve to a file are findings.
`mcpServers` objects are validated too. `.toml` documents must contain an
`mcp_servers` table, which follows the same command-or-url and typed-option
rules.

The gateway and ledger packages only expose extension boundaries in v1. They
provide no proxy, network access, PR publishing, or automatic rollback. Core emits
the versioned receipt schema now so these plugins can be added later.
