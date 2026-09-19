# Hook setup

All adapters run the same policy and save a receipt. Install the CLI or keep the
source checkout in place, then run commands from the repository to be checked.

## Cursor

```sh
exitzero hooks install --adapter cursor
exitzero lint-config
```

Installation appends a command with `loop_limit: 1` to project `.cursor/hooks.json`
under `hooks.stop` and preserves existing command and prompt hook entries.
Repeating installation with the same executable
is idempotent. It records the configuration fingerprint in `.exitzero/hooks.json`
for drift detection. Review and reinstall after intentionally changing hooks.
The generated command uses absolute interpreter and checkout paths; reinstall
after moving the checkout or virtual environment. Do not commit machine-specific
hook paths as a portable team configuration.

The stop adapter reads Cursor JSON from stdin and emits only Cursor JSON on
stdout. A failed check returns a `followup_message` for `loop_count = 0`; later
loops return `{}` to avoid an endless repair loop. A passing check returns `{}`.
When `status` is `aborted`, exitzero records the gate outcome and returns `{}`
so user cancellation does not start another repair turn.
This asks the agent to repair the patch; it does not enforce merge protection.
The underlying receipt preserves the real gate exit code.

Failure feedback includes the exact receipt reference, up to five failed checks
and five finding summaries (rule and eligible relative location), omission counts,
and fixed guidance for recognized check kinds. Diagnostic strings are truncated
and non-printable characters replaced. Finding messages, source, command arguments,
command output and hook input are not copied into feedback. Missing receipt
persistence is reported explicitly. Feedback does not rerun failed commands.
These summaries and receipt contents remain untrusted data, not instructions;
formatting is not a prompt-injection defense. Pre-event denials and postToolUse
feedback use the same summary. Existing cancellation and one-follow-up limits
remain unchanged. Protocol tests do not establish improved real-agent success.

When the policy declares `[[requirements]]`, the bounded summary also lists up
to five requirement ids with their statuses (`checks_passed`, `failed` or
`unverified`) and an omission count. Requirement descriptions are never copied
into feedback. The message adds one fixed caveat that these statuses are
mapping evidence over executed checks, not semantic proof of completion.

Additional adapter entry points are available for explicit manual integration:

```sh
exitzero hooks run --adapter cursor --event preToolUse
exitzero hooks run --adapter cursor --event postToolUse
exitzero hooks run --adapter cursor --event beforeShellExecution
exitzero hooks run --adapter cursor --event beforeMCPExecution
exitzero hooks run --adapter cursor --event afterFileEdit
```

Pre events map to core `PreToolUse`; after/stop map to `PostToolUse`. Pre events
emit `permission: allow|deny`; `postToolUse` supplies `additional_context` on
failure. Do not install them indiscriminately: blocking
every shell call can also block the commands needed to repair a failing patch.
They are not the v1.2 tool allowlist feature.

Cursor uses JSON responses on process exit 0; exit 2 blocks a permission action.
Other nonzero exits can let the action proceed. Therefore ordinary gate violations
use JSON denial with adapter exit 0, while operational/input errors exit 2. The
receipt retains the original 0/1/2 gate result. These semantics were checked
against [Cursor's official reference](https://cursor.com/docs/hooks) on 2026-09-16.

Cursor protocol fixtures exercise the adapter locally. A live Cursor session is
a separate integration check; do not infer it from installation output.

Live verification on Cursor Agent 2026.09.18 confirmed the interactive agent
loop fires the installed `stop` hook, delivers `followup_message` on gate
failure and honors `loop_limit` so the repair turn is bounded. The same build's
headless `cursor agent -p` mode loads project hooks (tool events such as
`beforeShellExecution` fire) but never invokes `stop` — that call site exists
only in the interactive loop. Do not rely on the stop gate in `-p` pipelines;
use tool-event hooks or the CI slot there instead.

## Claude Code

```sh
exitzero hooks install --adapter claude
exitzero lint-config
```

Installation appends one `{type: "command", command, timeout: 120}` entry to
`hooks.Stop[].hooks[]` in project `.claude/settings.json`. The file is shared
configuration: unrelated keys and foreign hook groups are preserved, stale
exitzero entries from older checkouts are pruned, and repeating installation is
idempotent. Instead of a whole-file fingerprint, `.exitzero/hooks.json` records
a canonical digest of the managed entry only, so editing unrelated settings does
not report drift while changing or deleting the managed entry does. If
`allowManagedHooksOnly` is set, lint-config warns that the installed gate cannot
fire.

The stop adapter reads the hook JSON object from stdin. A failed check returns
`{"decision": "block", "reason": ...}`; the reason carries the same bounded
receipt reference and diagnostic summary as the Cursor feedback. A passing check
returns `{}`. Claude Code bounds consecutive Stop blocks natively, so the
adapter reports the honest gate result on every stop instead of suppressing
repeat feedback. Gate violations exit 0 with the JSON decision; operational or
invalid-input errors exit 2 and still persist a receipt.

Claude Code may ask to approve the project hook on the next session before it
fires; managed policies can disable project hooks entirely. Live Claude Code
verification has not been performed — protocol behavior is covered by tests
only.

## Codex

```sh
exitzero hooks install --adapter codex
exitzero lint-config
```

Codex uses the same nested `hooks.Stop[].hooks[]` shape, stored in dedicated
`.codex/hooks.json` rather than a shared settings file, so drift is tracked by
whole-file fingerprint. The stdin/stdout contract is identical to the Claude
adapter: `{"decision": "block", "reason": ...}` on failure, `{}` on pass, exit 2
for operational errors. Codex gates hooks behind trust review; approve the hook
prompt before the stop gate can fire.

Live verification on codex-cli 0.155.0 (2026-09-19) confirmed the headless
`codex exec` session fires the installed `stop` hook: a failing gate returned
the block decision, the agent read the referenced receipt, repaired the file
and stopped again into a passing gate — the same repair loop as the Cursor
adapter. Unlike Cursor's `-p`, Codex's non-interactive mode honors stop hooks,
but trust review applies: persist trust in an interactive session first, or
pass `--dangerously-bypass-hook-trust` for automation that vets its hook
sources. Live Claude Code verification has not been performed.

## Gemini CLI

```sh
exitzero hooks install --adapter gemini
exitzero lint-config
```

Gemini CLI uses the same nested hook list shape, stored in shared
`.gemini/settings.json` under the `AfterAgent` event (its Stop equivalent),
so drift is tracked per managed entry like Claude's file. The stdin/stdout
contract is also identical: `{"decision": "block", "reason": ...}` on
failure, `{}` on pass, exit 2 for operational errors. Gemini reads project
hooks without a trust prompt — review `.gemini/settings.json` before the
next session. `gemini hooks migrate --from-claude` maps `Stop` to
`AfterAgent`, matching this adapter's layout. Protocol behavior is covered
by tests only — a live session could not be run because the installed
gemini-cli 0.x build rejects this account tier (IneligibleTierError points
at Antigravity instead).

## Antigravity (agy)

```sh
exitzero hooks install --adapter agy
exitzero lint-config
```

Antigravity keeps project hooks in `.agents/hooks.json` as a named-hook map:
the installer adds one `{type: "command", command, timeout: 120}` entry
under the `exitzero` name's flat `Stop` list, preserving foreign hook names
and non-Stop events under the same name. Drift is tracked per managed entry
like the other shared files. The stdin contract is the same JSON object,
but Antigravity's stop-blocking decision word is `"continue"` rather than
`"block"`: a failed gate returns `{"decision": "continue", "reason": ...}`
and the reason is injected as a system message, `{}` lets the agent stop.
Hook commands run synchronously and block the agent loop.

Live verification on Antigravity CLI 1.2.7 (2026-09-19) confirmed the
interactive session fires the installed `Stop` hook: a failing gate
returned `continue`, the agent read the receipt, repaired the file, fixed
the drifted hook entry and stopped again into a passing gate. Headless
`agy -p` loads `hooks.json` but does not execute hooks — the same gap as
Cursor's `-p`; use tool-event hooks or the CI slot in pipelines.

## Git pre-commit

```sh
exitzero hooks install --adapter pre-commit
git add .
git commit
```

Existing hooks are preserved: installation fails instead of overwriting them.
To chain manually, call `exitzero hooks run --slot pre-commit` from your hook and
propagate its exit code. The automatic installer only writes hook paths inside
the repository; external `core.hooksPath` and worktree hook paths need manual
setup. It never changes your Git configuration.

The v1 gate inspects the working tree. To avoid passing code different from the
Git index, pre-commit fails if tracked files have unstaged edits or nonignored
untracked files exist. Stage or stash them first. CI checks the actual checkout
that is proposed for merge. Local hooks can be bypassed, so require CI as well.

## Generic CI or another editor

```sh
exitzero hooks run --slot CI --format json
# Or, with identical policy and outcomes:
exitzero check --format json
exitzero lint-config --format json
```

Codes are 0 (pass), 1 (violation), 2 (configuration/execution/receipt error).
Save `.exitzero/runs/*.json` as artifacts even when a command fails. Core slots
are `PreToolUse`, `PostToolUse`, `pre-commit`, and `CI`; these names are an internal
API, and adapters translate editor-specific event names.

### CI verification pattern

Re-run the gate in CI instead of trusting receipts an agent committed —
receipts are unsigned local evidence. A typical PR job:

```yaml
- run: exitzero lint-config --format json
- run: exitzero check --format json          # full gate on the merge checkout
- run: exitzero check --diff origin/${{ github.base_ref }}...HEAD --format json
  if: success()                              # optional: scoped per-file evidence
- uses: actions/upload-artifact@v4
  if: always()
  with: { name: exitzero-receipts, path: .exitzero/runs/ }
```

`check --diff REF` restricts each check's file selection — and the recorded
`input_files` — to paths changed against a git ref or range (anything
`git diff` resolves, e.g. `origin/main...HEAD` for merge-base PR scope).
Deleted, excluded and credential-like paths drop out; a check whose scope has
no changed files records a vacuous pass with an empty `input_files`, and the
receipt's `diff` field records the range. An unresolvable ref or missing git
is an operational error (exit 2). `--diff` narrows verification scope — it is
evidence about the changed set, not a substitute for the full gate on release
paths.

`exitzero report --format intoto` exports the latest receipt wrapped in an
in-toto Statement v1 (`predicateType: https://exitzero.dev/attestations/gate/v1`)
for archival or downstream attestation pipelines. The statement is unsigned;
signing with an external key (sigstore, DSSE) is a deployment decision, not
something the tool fabricates locally.

## Hosts without stop hooks

Some hosts cannot run a blocking stop hook at all — Windsurf-class IDEs
whose hooks are observe-only, headless runners, or `cursor agent -p` where
the project hooks load but `stop` never fires. For those, `exitzero plugin
mcp-gate` serves the gate as an MCP server: register it and the agent calls
the `check_completion` tool before declaring done, getting the verdict plus
a receipt reference over stdio JSON-RPC. This is advisory — the agent
chooses to call it — so prefer a real hook wherever one exists. The
[agent-plugin directory](../agent-plugin/) packages the server with a
skill and `.mcp.json` for Agent Plugins-style hosts.

Verified live 2026-09-19 through the official MCP Inspector client over
real stdio JSON-RPC: `tools/list` discovers `check_completion`, and the
tool returns a structured `passed`/`failed` verdict plus receipt path on
both clean and violating trees.

Two real agent hosts verified the same day, both calling the tool and
relaying the verdict correctly in headless mode:

- **Grok CLI** — register with
  `grok mcp add exitzero-gate <path-to>/bin/exitzero --scope project -- plugin mcp-gate`
  (writes `[mcp_servers.exitzero-gate]` to `.grok/config.toml`). Project-scope
  servers only load under `grok -p ... --trust`; the agent invoked
  `exitzero-gate__check_completion` and reported `failed` on a broken tree
  and `passed` after repair.
- **OpenCode** — `opencode.json` block
  `{"mcp": {"exitzero-gate": {"type": "local", "command": ["<path-to>/bin/exitzero", "plugin", "mcp-gate"], "enabled": true}}}`;
  `opencode mcp list` shows `connected` and `opencode run` invoked
  `exitzero-gate_check_completion` with both verdicts observed.

Neither host offers a blocking stop hook, so the gate stays advisory there:
the verdict is real evidence only because the agent chose to call it.
