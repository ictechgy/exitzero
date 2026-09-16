# aidd-gate

**AI가 “끝났다”고 말해도, 검사를 통과하기 전에는 끝난 것이 아닙니다.**

aidd-gate는 저장소의 정책을 실행하는 작은 개발 도구입니다. 같은 TOML 정책으로
로컬 CLI, Git 훅, CI를 검사하고 매번 JSON 실행 영수증을 남깁니다. 사용자는 하나의
명령을 쓰고, 내부는 작은 코어와 플러그인으로 나뉩니다.

**An agent saying “done” is not evidence.** Run the policy, check the exit code,
and keep the receipt. This MVP checks Python code and agent configuration.
Explicit command checks can run your existing tools for any language.

## Try it without downloading dependencies

Python 3.11 or newer is required. Runtime and tests use only the standard library.

```sh
export PATH="$PWD/bin:$PATH"
mkdir /tmp/aidd-gate-demo
cd /tmp/aidd-gate-demo
aidd-gate init
aidd-gate check
aidd-gate lint-config
aidd-gate report --format json
```

`init` creates a starter TOML policy, a managed section in `AGENTS.md`, and ignore
entries for local receipts and Python caches. In an empty repository it also
creates a tiny Python sample. It preserves existing policy files. The starter
policy checks syntax only: add project-specific checks before using it as a merge
gate. A syntax pass is not a claim that your application works.

Run the included example, which exercises all four check types:

```sh
./bin/aidd-gate --root examples/sample check
./bin/aidd-gate --root examples/sample lint-config
```

For a conventional installation, use a virtual environment and `python -m pip
install .`. Building uses setuptools; runtime has no third-party dependencies.
If setuptools and wheel are already available, an offline editable install is
`python -m pip install --no-index --no-build-isolation --no-deps -e .`.
The checkout launcher above needs no build tools.

## Put review requirements in the policy

```toml
version = 1
plugins = ["aidd_gate_verify", "aidd_gate_harness"]

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

After editing the policy, run `aidd-gate init --sync`. Only the generated section
of `AGENTS.md` changes. Its fingerprint covers the complete parsed policy, so
changing check options also creates detectable drift. Text outside the section
stays yours. Harness rules are documentation and conflict detection, not semantic
enforcement: express exact messages, types and result ordering in executable
tests. See [the sample](examples/sample).

| Check | What v1 detects |
| --- | --- |
| `python.syntax` | Python that cannot be parsed |
| `python.imports` | Unresolved modules and missing statically declared local module symbols |
| `python.test-quality` | No test cases, empty tests, obvious constant-only assertions |
| `command` | A configured test/lint command fails or exceeds its timeout |
| Harness lint | Generated AGENTS drift, installed-hook drift, explicit JSON config shape errors, repeated/conflicting rule IDs |

Import analysis does not execute imported code. It is intentionally conservative
and does not prove arbitrary dynamic exports, package loading or third-party API
signatures. `allow_modules` explicitly trusts listed external module names.
Test-quality analysis detects obvious problems; run real tests as well. Natural
language contradictions in arbitrary AGENTS prose or Cursor rule files are not
understood by v1.

## Commands and outcomes

```sh
aidd-gate init
aidd-gate init --sync
aidd-gate check --format json
aidd-gate lint-config --format json
aidd-gate hooks install --adapter cursor
aidd-gate hooks install --adapter pre-commit
aidd-gate hooks run --slot CI --format json
aidd-gate report --format json
aidd-gate plugin harness-eval   # Explicitly unavailable in v1: exit 2.
```

Global `--root` and `--policy` options go before the subcommand. `check` runs
configuration linters and verification checks. `lint-config` never executes
verification commands.

| Exit code | Meaning |
| --- | --- |
| 0 | All configured checks passed |
| 1 | A check found a violation |
| 2 | Invalid policy, missing plugin, execution error, or receipt could not be written |

Every `check`, `lint-config`, and hook gate writes a unique JSON receipt under
`.aidd-gate/runs/`, including failures and malformed policies. If storage fails,
the command exits 2 and reports `receipt: null`; it cannot claim success.
`--format json` prints the same machine-readable result. `report` reads the
latest receipt; it does not run a fresh check. See [receipt schema](docs/receipt.schema.json).

Receipts include check IDs, findings, exit code, policy hash, hashes of selected
inputs, plugin names and timing. They do not contain source code, environment
variables, hook input, command arguments or command output. Command output is
discarded; rerun a failing command directly to debug it. Receipts identify the
declared input selection, not every dependency of arbitrary commands. They are
local evidence, not signed or tamper-proof attestations.

## Local hooks and CI

See [hook setup](docs/HOOKS.md). Cursor uses a `stop` hook by default: failures
request one follow-up repair turn. This is feedback, not a merge barrier. Git
pre-commit and CI enforce exit codes. The generic hook command has the same
0/1/2 contract as `check`; Cursor translates results to its JSON protocol.

Run the repository's automated milestone runner:

```sh
python3 scripts/ci.py
```

It runs the repository gate, config lint, sample gate and ten pass/fail fixtures,
and writes `.aidd-gate/ci-results.json` plus logs. The GitHub workflow runs this
same script and uploads `.aidd-gate/` evidence even on failure. Configure the CI
job as a required branch check in your hosting service; this repository does not
change branch protection settings.

The [riskgate pilot](docs/PILOT_RISKGATE.md) applies the same gate to a pinned
real repository. It checks a passing baseline and four independent faults,
including an empty test that the upstream test runner still accepts. Its runner
preserves the original checkout and records all gate receipts.

## Trust and scope

Policies, selected plugins and command checks are trusted executable configuration.
Review them before running an unfamiliar repository. Static checks and config
lint make no network requests. Command checks can run arbitrary local programs;
choose offline commands to keep the whole gate offline. This is not an execution
sandbox or the future MCP allowlist gateway. Config files are read only when
explicitly selected (and the installed Cursor file is fingerprinted); do not
include credential files. Known credential-like paths and symlink targets are
rejected. Path filtering is not a universal secret detector.

There is no cloud service, model hosting, model training, full agent evaluation,
proxy gateway, PR publisher or automatic rollback in v1.

## Structure and contributing

```text
packages/core              policy, CLI, hook slots, plugin loader, receipts
packages/plugin-verify     verification rules and command checks
packages/plugin-harness    configuration lint; eval command stub
packages/plugin-mcp-gateway  v1.2 interface stub
packages/plugin-ledger       v1.3 interface stub
fixtures/                  ten positive and negative repository cases
examples/sample/           runnable error-text/type/order contract example
```

Read [Plugin API](docs/PLUGIN_API.md), [roadmap](ROADMAP.md), and
[design references](docs/REFERENCES.md). No project code was copied from prior art.
Run focused regression tests with `python3 scripts/run_tests.py`; run the complete
local acceptance sequence with `python3 scripts/ci.py`.
License: [MIT](LICENSE).
