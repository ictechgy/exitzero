# exitzero

**An agent saying “done” is not evidence.** Run the policy, check the exit
code, and keep the receipt.

English | [한국어](README.ko.md)

exitzero is a small developer tool that runs a repository's policy. The same
TOML policy drives the local CLI, Git hooks, and CI, and every run leaves a
JSON receipt. You use one command; inside, a small core and plugins do the
work. Built-in checks cover Python code and agent configuration. The Node
profile connects existing test, lint and type-check commands; explicit command
checks can run your tools for other languages too.

**Cursor stop requests a repair turn. Merge protection comes from required CI.**
Start with the failure demo below, then follow [required CI setup](docs/HOOKS.md#required-ci-setup).

## Install

Python 3.11 or newer is required. Runtime and tests use only the standard
library.

```sh
pip install exitzero
```

This is the Python package. The unrelated npm package named `exitzero` forces
commands to exit zero; it also installs an `exitzero` executable. Use a Python
virtual environment if both are installed.

## 30-second failure demo

After installation, run this in a **new directory**, separate from your project.
It needs no test framework download. The policy explicitly runs the test suite:

```sh
mkdir exitzero-demo
cd exitzero-demo
mkdir tests
cat > tests/test_double.py <<'PY'
from unittest import TestCase

def double(value):
    return value + 1

class DoubleTests(TestCase):
    def test_double(self):
        self.assertEqual(double(3), 6)
PY
exitzero init --profile python \
  --test-command '{python} -B -m unittest discover -s tests'
exitzero check --format json  # expected exit 1; test-command fails
exitzero report --format json # the saved failure, including its receipt path
python3 - <<'PY'
from pathlib import Path
path = Path('tests/test_double.py')
path.write_text(path.read_text().replace('value + 1', 'value * 2'))
PY
exitzero check --format json  # exit 0; a new receipt records the repair
```

Both outcomes persist under `.exitzero/runs/`. A basic `init` checks syntax only;
the `--test-command` above is what catches this behavior bug.

For an existing repository without a policy, start there instead:

```sh
exitzero init
exitzero check
exitzero lint-config
exitzero report --format json
```

Without installing, prefix every command with `uvx` (or `pipx run`):

```sh
uvx exitzero init
uvx exitzero check
```

`init` creates a starter TOML policy, a managed section in `AGENTS.md`, and
ignore entries for local receipts and Python caches. In an empty repository
it also creates a tiny Python sample. It preserves existing policy files.
The starter policy checks syntax only: add project-specific checks before
using it as a merge gate. A syntax pass is not a claim that your application
works.

To run it from a source checkout instead — no package installation or build
tools needed:

```sh
git clone https://github.com/ictechgy/exitzero.git
cd exitzero
export PATH="$PWD/bin:$PATH"
```

For a conventional source install, use a virtual environment and `python -m
pip install .`. Building uses setuptools. If setuptools and wheel are already
available, an offline editable install is `python -m pip install --no-index
--no-build-isolation --no-deps -e .`. The checkout launcher above needs no
build tools.

## Generate checks for a Python repository

`init` can write the common static checks and your existing test/review
commands in one step. Run this *instead of* the bare `init` above: `init`
never overwrites an existing policy and exits 2 if one is present — in that
case edit the policy directly, then run `exitzero init --sync`. Each command
is parsed into argv and later runs with `shell=False`; `{python}` means the
Python interpreter running `exitzero`.

```sh
exitzero init --profile python \
  --source-root src --source-root tests \
  --allow-module numpy --allow-module pytest \
  --test-command '{python} -m pytest' \
  --review-command '{python} scripts/review_contract.py'
```

`--source-root`, `--allow-module`, and `--review-command` can be repeated.
Quote literal arguments that contain punctuation; shell pipelines, redirects,
and other control operators are rejected. `init` only records commands, so it
does not execute them. Keep credentials out of command arguments because the
policy stores the resulting argv. Existing policies are never overwritten,
and generation options cannot be combined with `init --sync`. The default
profile remains the syntax-only compatibility starter.
Generated command checks fingerprint Python files for the receipt. If a test
or review command depends on JSON, YAML, Markdown, or another non-Python
input, edit that check's `paths` in the policy to include those files.

From a checkout, the included example exercises all four check types:

```sh
./bin/exitzero --root examples/sample check
./bin/exitzero --root examples/sample lint-config
```

## Generate checks for a Node repository

Run inside an existing Node project with a local
`package.json` and a test script:

```sh
exitzero init --profile node
# Or, instead of the command above, explicitly connect your existing scripts:
exitzero init --profile node --source-root src \
  --test-command 'npm test' \
  --lint-command 'npm run lint' \
  --typecheck-command 'npm run typecheck' \
  --input-path 'fixtures/**/*.json'
```

These are alternative initializations; an existing policy is never overwritten.
The default test command is `npm test`; choose `pnpm test`, `yarn test`,
`node --test`, or a non-watch runner command through `--test-command`.
Lint/type-check scripts are added only when explicitly supplied. Generation
does not run scripts, install packages, or create a Python sample. Missing tools
and failing scripts fail the gate; no `--if-present` or error-swallowing flags
are inserted. A custom test command can initialize a project without package.json.

The checks fingerprint JS/JSX/TS/TSX and module variants, test directories, root
tool configuration, package manifests, common lockfiles, TS/JS configs and ESLint
configuration. Repeat `--source-root` for monorepo code; the default is the whole
repository. Add other fixtures/configs with `--input-path`. Dependency/build
directories already excluded by core are not inputs; external tools, installed
dependencies and environment are not fingerprinted. Generated Node commands use
`reuse = false` and run afresh. Existing project tools own JS/TS analysis; there
is no bundled JavaScript parser or package manager.

## Put review requirements in the policy

Turn a reported regression into a policy-linked kit:

```sh
exitzero plugin incident-kit --id negative-total \
  --description 'A negative input silently produces the wrong total' \
  --path 'src/**/*.py' --path 'tests/**/*.py' --path 'data/cases.json'
```

Use input paths that apply to your project; omit `--path` for `**/*.py`.
The generator adds two deliberately failing unittest placeholders under
`tests/incidents/negative-total/`, a test-quality check, a regression command,
and a requirement mapping. Existing hooks/CI use the updated policy; no hooks
are installed automatically. Fill in the reported case and a neighboring valid
case, record a failing gate against the bug, repair it, and keep the passing
receipt. Missing tests also fail. Existing kits are never overwritten, commands
are not executed during generation, and the description is stored only in the
kit's `incident.json`. Review descriptions before committing them.

```toml
version = 1
plugins = ["exitzero_verify", "exitzero_harness"]

[[checks]]
id = "syntax"
kind = "python.syntax"
paths = ["src/**/*.py", "tests/**/*.py"]

[[checks]]
id = "imports"
kind = "python.imports"
paths = ["src/**/*.py", "tests/**/*.py"]
[checks.options]
roots = ["src", "."]
allow_modules = []

[[checks]]
id = "test-quality"
kind = "python.test-quality"
paths = ["tests/test_*.py"]

[[checks]]
id = "review-contracts"
kind = "command"
paths = ["src/**/*.py", "tests/**/*.py"]
[checks.options]
argv = ["{python}", "-m", "unittest", "discover", "-s", "tests", "-v"]
timeout = 30

[harness]
config_files = []
rules = [{id = "error-text", value = "Tests assert the exact public error text."}]
```

After editing the policy, run `exitzero init --sync`. Only the generated
section of `AGENTS.md` changes. Its fingerprint covers the complete parsed
policy, so changing check options also creates detectable drift. Text outside
the section stays yours. Harness rules are documentation and conflict
detection, not semantic enforcement: express exact messages, types and result
ordering in executable tests. See [the sample](examples/sample).

| Check | What it detects |
| --- | --- |
| `python.syntax` | Python that cannot be parsed |
| `python.imports` | Unresolved modules and missing statically declared local module symbols |
| `python.test-quality` | No test cases, empty tests, obvious constant-only assertions |
| `python.test-integrity` | Test files deleted since a git base ref, removed test cases, new skip/xfail markers, net assertion loss |
| `command` | A configured test/lint command fails or exceeds its timeout |
| Harness lint | Generated AGENTS drift, installed-hook drift, JSON/TOML config shape errors (Cursor and Claude hook documents, MCP server tables), repeated/conflicting rule IDs |

Import analysis does not execute imported code. It is intentionally
conservative and does not prove arbitrary dynamic exports, package loading or
third-party API signatures. `allow_modules` explicitly trusts listed external
module names. Test-quality analysis detects obvious problems; run real tests
as well. Natural language contradictions in arbitrary AGENTS prose or Cursor
rule files are not understood by v1.

`python.test-integrity` compares worktree test files against a git baseline
(`base` option, default `HEAD`; two-dot `git diff <base>` semantics covering
staged and unstaged edits). It needs a git worktree and a resolvable base
commit; a missing repo or ref is an operational error. Options
`allow_deletions`, `allow_skip_markers` and `max_removed_assertions` relax
individual categories. Set `reuse = false` on this check — the baseline lives
outside hashed file inputs, so unchanged worktree files could otherwise reuse
a stale pass after the base ref moves.

## Commands and outcomes

Each check can declare `enforcement = "warn"` for
advisory rollout (`"block"` is the default):

```toml
[[checks]]
id = "style"
kind = "command"
enforcement = "warn"
paths = ["src/**/*.py"]
[checks.options]
argv = ["ruff", "check", "src"]
```

A reported violation stays `status: "failed"` in the check entry, with
`enforcement: "warn"` and `blocking: false`; its findings become warnings.
The gate can exit 0 while retaining those advisory failures. Mapped requirements
remain `failed`, never `checks_passed`; they block only when a failing mapped
check blocks. Unmapped/unverified requirements still block. Missing commands,
timeouts and signal termination remain blocking execution findings, and invalid
policy, plugin errors or missing receipts still exit 2. Config lint and hook
handlers are not downgraded. `--reuse` never reuses an advisory failure.

```sh
exitzero init
exitzero init --sync
exitzero check --format json
exitzero check --reuse --format json
exitzero check --diff origin/main...HEAD --format json
exitzero lint-config --format json
exitzero doctor --format json
exitzero doctor --adapter cursor  # require this local adapter to be configured
exitzero hooks install --adapter cursor
exitzero hooks install --adapter claude
exitzero hooks install --adapter codex
exitzero hooks install --adapter gemini
exitzero hooks install --adapter agy
exitzero hooks install --adapter pre-commit
exitzero hooks install --adapter pre-push
exitzero hooks install --adapter copilot
exitzero hooks run --slot CI --format json
exitzero report --format json
exitzero report --format intoto   # unsigned in-toto Statement wrapping the latest receipt
exitzero report --run-id RUN_ID --format intoto # select the gate run explicitly
exitzero plugin harness-eval --scenario examples/eval-repair   # opt-in bounded eval
exitzero plugin mcp-gateway --config gateway.toml              # stdio MCP proxy
exitzero plugin mcp-gate                                       # stdio completion-gate MCP server
exitzero plugin ledger-publish                                 # aggregate run record
```

Global `--root` and `--policy` options go before the subcommand. `check` runs
configuration linters and verification checks. `lint-config` never executes
verification commands. `plugin harness-eval` replays a scripted multi-turn
scenario against the gate inside a temporary copy; each turn's expectations
are scored, skipped scenarios are reported separately, and the report lands
under `.exitzero/evals/`. From the second turn onward each turn records
`transitions` — SWE-bench-style `fail_to_pass`, `pass_to_pass` and the
regression directions over check ids — so "what got fixed" and "what stayed
green" are separate evidence. See [the eval example](examples/eval-repair).
`plugin mcp-gateway` spawns one upstream MCP server and proxies stdio
JSON-RPC; `tools/call` is authorized against TOML allow/deny patterns
(deny-by-default) and every decision lands in `.exitzero/mcp-gateway/`
audit logs. See [the plugin contract](docs/PLUGIN_API.md) for the config
schema. `plugin mcp-gate` serves the completion gate itself over stdio
JSON-RPC: hosts without a blocking stop hook register it so the agent calls
the `check_completion` tool before declaring done — advisory, not enforced.
The [agent-plugin directory](agent-plugin/) packages it with a skill and
`.mcp.json` for Agent Plugins-style hosts. `plugin ledger-publish` rolls
receipts into a run record with rollback hints under `.exitzero/ledger/`;
`--pr N` posts it via `gh` — explicitly, and only then.

`check --reuse` shortens iterative loops: a check is recorded as `reused`
instead of re-executed only when a prior receipt passed that check against
the same policy, tool version, and byte-identical selected inputs (the
check's `paths` plus plugin-declared inputs). Added, deleted or modified
inputs re-run the check, and hook gates always execute fully. Reuse is
evidence, not a new result — the receipt names the source run under
`reused_from`.

Reuse is deliberately narrow. The source entry must have passed with zero
findings (warning-carrying passes re-run so findings stay fresh), checks
with an empty input selection always re-run, and receipts flagging input
mutation or operational errors are skipped. The reuse key covers core's
tool version and the policy hash — plugin code changes do not invalidate
reuse, so re-verify after upgrading plugins. Treat `.exitzero/runs` as a
trust boundary: receipts are unsigned local evidence, so use `--reuse`
only where the runs directory is not attacker-writable or restored from
an untrusted cache. A check can opt out with `reuse = false` in its
`[[checks]]` table — required for checks whose correctness depends on
state outside hashed file inputs, such as `python.test-integrity`'s git
baseline.

`check --diff REF` limits verification to files changed against a git ref
or range — the PR-scoped pattern documented in [the CI section](docs/HOOKS.md).
It narrows evidence to the changed set; keep a full `check` on release paths.

Gate commands — `check`, `lint-config`, and `hooks run` — report outcomes
through exit codes:

| Exit code | Meaning |
| --- | --- |
| 0 | No blocking violations (advisory failures remain in the receipt) |
| 1 | A check found a violation, including a command that fails, times out, or cannot start |
| 2 | Invalid policy, missing plugin, internal execution error, or a receipt that could not be written |

`init` and `report` are not gates. `init` exits 2 on invalid options or an
existing policy; `report` prints the latest stored receipt and exits 2 only
when that receipt itself recorded an execution error.

Every `check`, `lint-config`, and hook gate writes a unique JSON receipt
under `.exitzero/runs/`, including failures and malformed policies. If
storage fails, the command exits 2 and reports `receipt: null`; it cannot
claim success. `--format json` prints the same machine-readable result, and
`--format sarif` emits findings as a SARIF 2.1.0 run for code-scanning
integrations. `report` reads the latest receipt; it does not run a fresh
check. See [receipt schema](docs/receipt.schema.json).

Receipts include check IDs, findings, exit code, policy hash, hashes of
selected inputs, plugin names and timing. They do not contain source code,
environment variables, hook input, command arguments or command output.
Command output is discarded; rerun a failing command directly to debug it.
Receipts identify the declared input selection, not every dependency of
arbitrary commands. They are local evidence, not signed or tamper-proof
attestations.

## Local hooks and CI

exitzero also supports Copilot CLI `agentStop` and Git `pre-push`.
Pre-push checks a clean committed tree and rejects pushed commits different from
the checked-out HEAD. Copilot CLI 1.0.86 was live-verified with explicit repository
hook loading in prompt mode; host timeouts can still fail open. See
[hook setup](docs/HOOKS.md) for both contracts. The
[optional attestation recipe](docs/ATTESTATIONS.md) signs a fresh trusted CI
receipt artifact and explains how to verify its provenance and require the job.

These features require exitzero 0.4.0 or newer.
Run `exitzero doctor` to diagnose AGENTS drift and project hook setup without
executing verification commands. It reports `configured`, `missing`, `unmanaged`,
`misconfigured` or `unknown`, with a next step for each adapter. It catches a
disabled Cursor `failClosed` even after reinstalling the hook. Use
`--adapter NAME` to make an unconfigured adapter an error; optional missing
adapters do not fail a CI-only project. Doctor saves the normal JSON receipt:
0 means no diagnosed setup errors, 1 means a setup violation, and 2 means the
diagnosis or receipt could not be completed. Every runtime state remains
`unverified`: no client session, user/managed settings or remote branch rules
are inspected. A configured hook is not proof that it fires.

See [hook setup](docs/HOOKS.md) and [required CI setup](docs/HOOKS.md#required-ci-setup).
Cursor uses a `stop` hook by default:
failures request one follow-up repair turn. This is feedback, not a merge
barrier. Git pre-commit and CI enforce exit codes. The generic hook command
has the same 0/1/2 contract as `check`; Cursor translates results to its JSON
protocol.

From the exitzero checkout root, run the repository's automated milestone
runner:

```sh
python3 scripts/ci.py
```

It runs the repository gate, config lint, sample gate and the manifest-scored
fixtures, and writes `.exitzero/ci-results.json` plus logs. The GitHub
workflow runs this same script and uploads `.exitzero/` evidence even on
failure. Configure the CI job as a required branch check in your hosting
service; this repository does not change branch protection settings.

The [riskgate pilot](docs/PILOT_RISKGATE.md) applies the same gate to a
pinned real repository. It checks a passing baseline and four independent
faults, including an empty test that the upstream test runner still accepts.
Its runner preserves the original checkout and records all gate receipts.
The [vecdiff pilot](docs/PILOT_VECDIFF.md) adds external NumPy dependencies
and independent numeric review contracts. The [Node pilot](docs/PILOT_NODE.md)
connects p-limit's real tests, lint and TypeScript declaration checks, including
an explicit test-deletion blind spot. All pilots use isolated source copies.

## Trust and scope

Policies, selected plugins and command checks are trusted executable
configuration. Review them before running an unfamiliar repository. Static
checks and config lint make no network requests. Command checks can run
arbitrary local programs; choose offline commands to keep the whole gate
offline. This is not an execution sandbox; the MCP gateway authorizes tool names
and optional explicit argument rules for known tool schemas. Config files listed under `harness.config_files` are read only when
explicitly selected; an installed Cursor hooks file is fingerprinted
automatically so drift is detectable. Do not include credential files.
Known credential-like paths and symlink targets are rejected. Path filtering
is not a universal secret detector.

Receipts and gateway audit logs are local evidence only: anyone with write
access to `.exitzero/` can rewrite them; they are not signed or
tamper-proof. Files are validated before use, but a narrow
check-then-act window remains — policy reads, `init --sync` and hook
installation write through atomic renames and reject symlinks and
non-regular files, yet an adversary racing the filesystem inside the
checkout can still defeat per-path checks. Run the gate on a checkout you
control. The hook launcher runs `python -P -m exitzero` so a repository's
own `exitzero/` directory cannot shadow the installed package. The MCP
gateway bounds each JSON-RPC frame to 4 MiB and outstanding client
requests to 1024; a hostile or crashed upstream ends the session with
exit 2 rather than silently dropping traffic.

There is no cloud service, model hosting, model training, full agent
evaluation or automatic rollback. The MCP gateway stays a local stdio proxy;
`ledger-publish` writes to GitHub only through an explicit `--pr` flag.

## Structure and contributing

```text
packages/core              policy, CLI, hook slots, plugin loader, receipts
packages/plugin-verify     verification rules and command checks
packages/plugin-harness    configuration lint; bounded eval command
packages/plugin-mcp-gateway  stdio MCP proxy with a TOML tool allowlist
packages/plugin-mcp-gate     stdio completion-gate MCP server (check_completion)
packages/plugin-ledger       run-record aggregation and rollback hints
agent-plugin/                Agent Plugins packaging: manifest, .mcp.json, skill
fixtures/                  manifest-scored cases (one declared live-only skip)
examples/sample/           runnable error-text/type/order contract example
examples/eval-repair/      scripted multi-turn eval scenario
```

Read [Plugin API](docs/PLUGIN_API.md), [roadmap](ROADMAP.md), and
[design references](docs/REFERENCES.md). No project code was copied from
prior art. Run focused regression tests with `python3 scripts/run_tests.py`;
run the complete local acceptance sequence with `python3 scripts/ci.py`;
measure gate latency with `python3 scripts/bench.py` (synthetic tree by
default, `--root PATH` for a real repository). Verify a built package and
real Git-hook behavior with the [offline release
runner](docs/RELEASE.md). See [release notes](CHANGELOG.md).
License: [MIT](LICENSE).
