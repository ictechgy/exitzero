# exitzero

**An agent saying “done” is not evidence.** Run the policy, check the exit
code, and keep the receipt.

English | [한국어](README.ko.md)

exitzero is a small developer tool that runs a repository's policy. The same
TOML policy drives the local CLI, Git hooks, and CI, and every run leaves a
JSON receipt. You use one command; inside, a small core and plugins do the
work. This MVP checks Python code and agent configuration. Explicit command
checks can run your existing tools for any language.

## Install

Python 3.11 or newer is required. Runtime and tests use only the standard
library.

```sh
pip install exitzero
```

Then, inside any repository:

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
and generation options cannot be combined with `init --sync`. Without
`--profile python`, `init` remains the syntax-only compatibility starter.
Generated command checks fingerprint Python files for the receipt. If a test
or review command depends on JSON, YAML, Markdown, or another non-Python
input, edit that check's `paths` in the policy to include those files.

From a checkout, the included example exercises all four check types:

```sh
./bin/exitzero --root examples/sample check
./bin/exitzero --root examples/sample lint-config
```

## Put review requirements in the policy

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

| Check | What v1 detects |
| --- | --- |
| `python.syntax` | Python that cannot be parsed |
| `python.imports` | Unresolved modules and missing statically declared local module symbols |
| `python.test-quality` | No test cases, empty tests, obvious constant-only assertions |
| `command` | A configured test/lint command fails or exceeds its timeout |
| Harness lint | Generated AGENTS drift, installed-hook drift, JSON/TOML config shape errors (Cursor and Claude hook documents, MCP server tables), repeated/conflicting rule IDs |

Import analysis does not execute imported code. It is intentionally
conservative and does not prove arbitrary dynamic exports, package loading or
third-party API signatures. `allow_modules` explicitly trusts listed external
module names. Test-quality analysis detects obvious problems; run real tests
as well. Natural language contradictions in arbitrary AGENTS prose or Cursor
rule files are not understood by v1.

## Commands and outcomes

```sh
exitzero init
exitzero init --sync
exitzero check --format json
exitzero lint-config --format json
exitzero hooks install --adapter cursor
exitzero hooks install --adapter pre-commit
exitzero hooks run --slot CI --format json
exitzero report --format json
exitzero plugin harness-eval --scenario examples/eval-repair   # opt-in bounded eval
```

Global `--root` and `--policy` options go before the subcommand. `check` runs
configuration linters and verification checks. `lint-config` never executes
verification commands. `plugin harness-eval` replays a scripted multi-turn
scenario against the gate inside a temporary copy; each turn's expectations
are scored, skipped scenarios are reported separately, and the report lands
under `.exitzero/evals/`. See [the eval example](examples/eval-repair).

Gate commands — `check`, `lint-config`, and `hooks run` — report outcomes
through exit codes:

| Exit code | Meaning |
| --- | --- |
| 0 | All configured checks passed |
| 1 | A check found a violation, including a command that fails, times out, or cannot start |
| 2 | Invalid policy, missing plugin, internal execution error, or a receipt that could not be written |

`init` and `report` are not gates. `init` exits 2 on invalid options or an
existing policy; `report` prints the latest stored receipt and exits 2 only
when that receipt itself recorded an execution error.

Every `check`, `lint-config`, and hook gate writes a unique JSON receipt
under `.exitzero/runs/`, including failures and malformed policies. If
storage fails, the command exits 2 and reports `receipt: null`; it cannot
claim success. `--format json` prints the same machine-readable result.
`report` reads the latest receipt; it does not run a fresh check. See
[receipt schema](docs/receipt.schema.json).

Receipts include check IDs, findings, exit code, policy hash, hashes of
selected inputs, plugin names and timing. They do not contain source code,
environment variables, hook input, command arguments or command output.
Command output is discarded; rerun a failing command directly to debug it.
Receipts identify the declared input selection, not every dependency of
arbitrary commands. They are local evidence, not signed or tamper-proof
attestations.

## Local hooks and CI

See [hook setup](docs/HOOKS.md). Cursor uses a `stop` hook by default:
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
and independent numeric review contracts. Both pilots use isolated source
copies.

## Trust and scope

Policies, selected plugins and command checks are trusted executable
configuration. Review them before running an unfamiliar repository. Static
checks and config lint make no network requests. Command checks can run
arbitrary local programs; choose offline commands to keep the whole gate
offline. This is not an execution sandbox or the future MCP allowlist
gateway. Config files listed under `harness.config_files` are read only when
explicitly selected; an installed Cursor hooks file is fingerprinted
automatically so drift is detectable. Do not include credential files.
Known credential-like paths and symlink targets are rejected. Path filtering
is not a universal secret detector.

There is no cloud service, model hosting, model training, full agent
evaluation, proxy gateway, PR publisher or automatic rollback in v1.

## Structure and contributing

```text
packages/core              policy, CLI, hook slots, plugin loader, receipts
packages/plugin-verify     verification rules and command checks
packages/plugin-harness    configuration lint; eval command stub
packages/plugin-mcp-gateway  v1.2 interface stub
packages/plugin-ledger       v1.3 interface stub
fixtures/                  manifest-scored cases (one declared live-only skip)
examples/sample/           runnable error-text/type/order contract example
```

Read [Plugin API](docs/PLUGIN_API.md), [roadmap](ROADMAP.md), and
[design references](docs/REFERENCES.md). No project code was copied from
prior art. Run focused regression tests with `python3 scripts/run_tests.py`;
run the complete local acceptance sequence with `python3 scripts/ci.py`.
Verify a built package and real Git-hook behavior with the [offline release
runner](docs/RELEASE.md). See [release notes](CHANGELOG.md).
License: [MIT](LICENSE).
