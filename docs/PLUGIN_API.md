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

`Context` contains `root`, parsed `policy`, `policy_path`, and `diff` (the
`check --diff` ref or `None`; when set, a narrowed selection may legitimately
select zero files). `CheckSpec` contains
`id`, `kind`, `paths`, an `options` table, `enforcement` (`"block"` by default,
or `"warn"`), and `reuse` (the policy's per-check
`reuse` boolean, default `true`; `false` excludes the check from `--reuse`
result caching). Check and lint handlers return a list
of `Finding`. Exceptions become operational errors (exit 2); they cannot pass a
run. Findings must not include file contents, credentials or command output.

`Finding.category` defaults to `"violation"`. Use `"execution"` with error
severity for an execution failure that must never become advisory (the built-in
command check uses it for spawn errors, timeouts and signal termination).
Invalid categories or warning-severity execution findings are plugin errors.
Core applies `enforcement = "warn"` only to verification violation findings,
changing their effective severity to warning. Check entries retain their observed
`passed`/`failed`/`reused` status and record `enforcement` plus `blocking` separately.
Plugin warnings alone still mean a passed check. Failed advisory checks cannot
be reused. Linter, hook-handler and core errors retain their normal gate behavior.

Registration methods:

- `add_check(kind, handler(context, spec), *, inputs=None)`
- `add_linter(name, handler(context))`
- `add_hook(slot, handler(context, slot))`, for `PreToolUse`, `PostToolUse`,
  `pre-commit`, `pre-push`, `CI`.
- `add_command(name, handler(context, argv))`, called with `exitzero plugin NAME`.

The optional keyword-only `inputs(context, spec)` callable returns a `list` or
`tuple` of repository-relative glob strings; an empty sequence means no additional
inputs. Existing two-argument `add_check` calls are unchanged. Registration rejects
invalid names, duplicate kinds, non-callable checks and non-callable providers
(other than `None`). Providers must validate their options and only discover inputs,
not execute checks or modify files; they also run during `lint-config`.

Core calls each configured check's provider before and after dispatch, adds its
patterns to `spec.paths`, and selects files through core's guarded `select_files`.
The receipt fingerprints the initial selection. Changed hashes and added/deleted
selected files produce `core.inputs-changed` (exit 1). Invalid return shapes,
non-string or invalid path patterns, discovery exceptions and traversal I/O errors
are operational errors (exit 2), with the normal receipt-persistence attempt.
Providers should include all files influencing a check, even outside `spec.paths`,
and return globs covering possible new members rather than only existing names.

Core owns policy parsing, the registry, dispatch, generated AGENTS section, hook
adapters and receipt persistence. Plugins own check semantics. `check` runs
registered configuration linters before verification checks. `lint-config` runs
linters without executing check commands. Hook slots use the same policy and
runner; extra plugin handlers run after its checks.

`doctor` also runs the configured linters without verification checks or hook
handlers. Core adds project hook setup diagnostics and persists them in the
optional receipt `diagnostics` array, with `command: "doctor"`. Each observation
has `target`, `state`, `runtime`, `path`, `detail`, and `next_step`; runtime is
always `unverified`. Doctor does not infer execution from prior receipts and
leaves requirement mappings unverified without failing for missing execution.
Its extra project hook inputs are included in both stability snapshots.
There is no new plugin registration method; `doctor` is a reserved core command.

Shared helpers: plugins may import `exitzero.api` and `exitzero.services`;
every other core module is internal and outside the `API_VERSION` contract.
Policy generators can use `parse_policy(text)` to validate a proposed policy
without writing, and `updated_agents(current_text, policy)` to prepare a managed
section while preserving manual guidance. Both raise on invalid inputs before
the caller publishes changes.
`exitzero.services.select_files(root, patterns)` returns sorted, deduplicated
repository files; it rejects absolute paths, traversal, symlinks and
secret-like paths. `exitzero.services.match_path(relative, patterns)` applies
the same glob semantics to one repository-relative path that may not exist —
for example a deleted file reported by a VCS diff.
`exitzero.services.render_agents(policy)` returns the
complete managed section, with `<!-- exitzero:begin -->` and
`<!-- exitzero:end -->`; `run_gate(root, policy_name, command)` executes one
gate run (used by the harness eval replayer and the mcp-gate server). A plugin declares the contract
it was written against with its own `API_VERSION = 1` literal — it must not
re-export core's, which could advertise a contract it never verified.
Plugins should validate their own `spec.options` and raise `ValueError` on
invalid settings. Invalid settings must never silently disable a check.

Verification kinds in v1:

- `python.syntax`: parse selected Python files.
- `python.imports`: statically check import resolution and local module symbols;
  options `roots` (default `["."]`), `allow_modules` (default `[]`). Imports
  inside `try` bodies guarded by `except ImportError`/`ModuleNotFoundError`
  (the optional-dependency pattern) are skipped. Its input provider conservatively
  fingerprints every selectable Python file under all validated roots, including
  files outside `paths` and unreferenced modules, and detects membership changes.
- `python.test-quality`: reject missing/empty/obviously vacuous test cases.
- `python.test-integrity`: flag test weakening relative to a git baseline —
  deleted test files, removed test cases, newly added skip/xfail markers and
  net assertion loss. Options: `base` (default `HEAD`),
  `allow_deletions`/`allow_skip_markers` (default `false`),
  `max_removed_assertions` (default `0`). Requires a git worktree; declare
  `reuse = false` on this check since its baseline is not a hashed file input.
- `command`: execute `options.argv` without a shell; `{python}` expands to the
  current Python interpreter. `options.timeout` defaults to 30 seconds.
- `node.test-integrity`: bounded lexical comparison of literal-named JS/TS test
  declarations and suppression/focus markers against local Git. Options: `base`
  (default `HEAD`), `functions` (default `test`, `it`, `describe`, `suite`). Requires
  `reuse = false`; the input provider validates options without reading Git.
  Missing/changed refs are operational errors, unsupported source is a finding.
  See [supported forms and limits](NODE_TEST_INTEGRITY.md).

### Requirement mappings

An optional `[[requirements]]` array in `exitzero.toml` maps completion
requirements to verification check ids. Each entry requires exactly `id` (a
unique policy name), `description` (1–1000 nonempty characters) and `checks`
(a list of unique existing check ids; `[]` marks a deliberately unverified
requirement). Mapping is documentation: a `checks_passed` status records that
the mapped checks executed and passed in a stable run — it is not semantic
proof that the requirement itself is satisfied.

For example, append this mapping when a verification check with id `regression-suite`
is already declared, then run `exitzero init --sync`:

```toml
[[requirements]]
id = "acceptance"
description = "The declared acceptance suite passes"
checks = ["regression-suite"]
```

When configured, every `check`-family run records a `requirements` list in the receipt, one
entry per requirement: `id`, the mapped `checks`, and a `status` of
`checks_passed` (all mapped checks passed on stable inputs), `failed` (any
mapped check failed) or `unverified` (nothing proved this run). Requirement
statuses reflect actual outcomes from that run, not the mapping itself. A
requirement with no mapped checks, or whose checks did not run, produces a
`core.requirement-unverified` finding (exit 1); a failed mapped check keeps
`failed`. When all failing mapped checks are advisory, the requirement finding
is a warning; any blocking mapped failure makes it an error. A gate exit 0 does
not turn an advisory requirement failure into completion evidence. Operational
errors keep unverified evidence and exit 2. `lint-config`
reports all requirements as `unverified` without failing merely for lacking
execution. `core.inputs-changed` invalidates `checks_passed` evidence for that
run. Policies without `requirements` behave exactly as before.

Harness registration: one `harness.config` linter compares the managed AGENTS
section to `render_agents(policy)`, checks installed hook drift, validates
explicitly listed `harness.config_files`, and reports `harness.rules` entries
that contradict or duplicate each other. It must never read secret-like paths
or execute config values.

`exitzero plugin incident-kit --id NAME --description TEXT [--path GLOB ...]`
adds an unfinished Python unittest regression kit, quality/command checks and a
requirement mapping to the current policy, then synchronizes its AGENTS section.
Inputs default to `**/*.py`; the generated test file is always included. The
command has `reuse = false` and uses `python -B` to avoid stale test bytecode.
Generation does not run checks or install hooks. Duplicate ids, existing kit
directories, unsafe paths and ambiguous AGENTS markers fail before writing.
Writes publish the tests before policy/AGENTS; an I/O failure may leave partial
output to inspect, never a claimed successful kit. The placeholders deliberately
fail and must be replaced by real regression and neighboring-behavior assertions.

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
premature-upstream failures exit 2. The gateway is stdio-only and makes no network
requests; it inspects arguments only where explicit rules are configured.

Optional `[[argument_rules]]` tables constrain known tool schemas before
forwarding. Tool names are exact (not globs); duplicates and unknown rule fields
are errors. `keys` lists the only allowed top-level argument keys. Each key in
`paths`, `origins` or `enums` is required and must be a string:

```toml
[[argument_rules]]
tool = "read_file"
keys = ["path", "mode"]
[argument_rules.paths]
path = ["src", "tests"]
[argument_rules.enums]
mode = ["text"]

[[argument_rules]]
tool = "fetch_document"
keys = ["url"]
[argument_rules.origins]
url = ["https://docs.example.com"]
```

Path roots are relative to `upstream.cwd` (the invocation root by default).
Absolute argument paths must be inside that directory and one allowed root.
Traversal, symlinks, credential-like paths and shell/URL expansion syntax are
rejected; inputs must be literal paths. HTTPS origins match scheme, normalized
ASCII host and port exactly, without userinfo (use explicit punycode for IDNs).
Invalid/missing/extra arguments are
denied before the upstream sees the call; audit events record the decision and
`argument-policy` reason, never the argument values.

These constraints depend on the named tool honoring that literal argument
schema. They are not a shell parser, DNS/redirect firewall, content DLP or sandbox;
filesystem races remain possible. Keep arbitrary execution tools out of the
allowlist. Unlisted tools are already denied and hidden from `tools/list`; use
an explicit list of needed read tools rather than a broad `*` allow pattern.
Tools without argument rules retain the original name-only authorization.

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
