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
