# Real-repository pilot: Node and TypeScript

This pilot exercises the Node profile on
[p-limit 7.3.3](https://github.com/sindresorhus/p-limit/tree/a8a6fbec4e0e866d6d779b10889bb4f5567e70eb),
commit `a8a6fbec4e0e866d6d779b10889bb4f5567e70eb`. It uses the project's real
AVA tests, XO linter and tsd declaration tests. It does not add a JavaScript
analyzer to exitzero or change upstream source to obtain a passing baseline.

## Setup and scope

The original `npm test` runs `xo && ava && tsd`: all 30 tests pass, the declaration
tests pass, and XO allows one existing TODO warning. There are no added test
exclusions. The generated default profile passes with a single setup command:

```sh
exitzero init --profile node
exitzero check --format json
```

For the experiment, a fresh copy gets separate checks so the receipt identifies
which tool failed. This does not modify `package.json`:

```sh
exitzero init --profile node \
  --test-command 'node node_modules/ava/entrypoints/cli.mjs' \
  --lint-command 'node node_modules/xo/dist/cli.js' \
  --typecheck-command 'node node_modules/tsd/dist/cli.js'
```

These paths belong to the recorded dependency versions; projects should normally
connect their own stable package scripts. The pilot exports 16 committed files,
shares the prepared dependencies through a `node_modules` symlink, and mutates
separate source copies. The original checkout's HEAD, tracked hashes and Git
status must match before and after the run. Dependency directories are excluded
from gate input hashes; the resolved lockfile and tool versions are retained
separately. This is configuration isolation, not a process or network sandbox.

## Original command-only cases (0.4.0)

| Case | `check` | `lint-config` | Evidence |
| --- | --- | --- | --- |
| Unchanged source | 0 | 0 | 30 tests, lint and declaration tests pass |
| Harmless comment | 0 | 0 | Same 30 tests and checks pass |
| JavaScript syntax error | 1 | 0 | Test and lint commands fail |
| Incorrect `activeCount` getter | 1 | 0 | Tests fail; lint and types pass |
| `activeCount` declared as `string` | 1 | 0 | Declaration tests fail; runtime tests and lint pass |
| Redundant semicolon | 1 | 0 | Lint fails; runtime and declaration tests pass |
| Existing argument-forwarding test deleted | 0 | 0 | Only 29 tests remain: a known blind spot |
| Generated AGENTS policy hash changed | 1 | 1 | `harness.agents.drift` fails |

The runner checks exact finding rules, direct command exits, test counts and
persisted receipt equality. It also invokes the real `hooks run --slot CI` on
the passing baseline and failing getter mutation; those must return 0 and 1.
A pilot exit of 0 means these declared outcomes matched, including the explicitly
recorded test-deletion miss. It does not mean all injected defects were caught.

Two valid cases without findings are limited observations, not an estimated
false-positive rate. The Node profile delegates test quality to the configured
tools: deleting a test can still pass without an additional integrity check.
The 0.5.0 follow-up below enables the new optional JS/TS check.

## Original local run

On 2026-09-20, all eight declared outcomes and both CI-slot verdicts matched.
The source checkout remained unchanged. The 22 persisted receipts matched the
runner exits and JSON output. Local evidence is
`.exitzero/pilots/node-20260920T045812Z-1994febe/summary.json`.

The macOS run used Python 3.14.7, Node 22.20.0, npm 11.6.2, AVA 6.4.1,
XO 1.2.3, tsd 0.33.0, TypeScript 6.0.3 and yocto-queue 1.2.2. Initial dependency
installation took 22.40 seconds. Default policy generation took 0.057 seconds;
splitting the checks required three command flags and no manual TOML
editing. Installation warnings from upstream development dependencies are
retained in the setup log; this experiment is not a dependency security audit.

Across three alternating timing pairs, direct execution of the same three tools
took a median **4.692 seconds** and the gate took **4.873 seconds**. The difference
between those medians was **0.181 seconds (3.8%)** on this machine. Individual
gate-minus-direct differences ranged from 0.144 to 0.336 seconds. The randomized
timers in upstream tests and this small sample limit performance conclusions.

## Original hosted verification

[GitHub Actions run 35490667229](https://github.com/ictechgy/exitzero/actions/runs/35490667229)
passed on Linux at exitzero commit `647e014574ce297dbdb2dc1960cc22caf401a101`.
All eight cases and both CI-slot verdicts matched, including the test-deletion
miss. The downloaded artifact's 22 persisted receipts were independently checked
against the recorded exits and output; the runner/core hashes matched the local
checkout and the source-preservation check passed.

The workflow used Python 3.11 and Node 22.20.0 with its bundled npm 10.9.3. AVA,
XO, tsd, TypeScript and yocto-queue resolved to the same versions as the local
run; the separate Linux lockfile is retained in the artifact. Median direct/gate
times were **6.881/7.079 seconds**, a **0.198-second (2.9%)** difference across
three pairs. These measurements describe separate environments.

The ordinary [main CI run 35490640287](https://github.com/ictechgy/exitzero/actions/runs/35490640287)
also passed Python 3.11/3.14 gates and installed-wheel/Git-hook checks. That commit
changed test tooling and documentation only; its published 0.4.0 runtime was unchanged.

## Follow-up with test integrity (0.5.0)

Pass `--integrity` to the pilot runner to seed an isolated Git baseline from the
pinned export and generate `node.test-integrity` against its immutable commit id.
All Git commits happen in the disposable copies. The initial default Node profile
is still checked separately, without integrity, to preserve the original onboarding
control. The same runtime, lint and declaration commands remain enabled.

This mode changes the deletion expectation to a failed gate and adds a skip case:

| Case | Existing commands | Gate with integrity |
| --- | --- | --- |
| Existing argument-forwarding test deleted | AVA: 29 pass; XO/tsd: pass | Exit 1, `test-integrity` |
| Existing argument-forwarding test changed to `test.skip` | AVA: 29 pass, 1 skip; XO: fail; tsd: pass | Exit 1, `test-integrity` and `lint-command` |

The other seven original cases keep their expected verdicts. XO already catches
the explicit skip in this project; deletion supplies the demonstrated additional
protection. There are no baseline exclusions. The injected skip is intentional and
its upstream output is preserved.

The default lexical inventory recognizes 28 direct declarations here. The other
two AVA tests use the conditional alias `testClearQueueRejects`, which is not in
the default function-name list. Add aliases through the check's `functions` option
when needed. This pilot does not claim to track all 30 runtime tests statically or
prove that their retained assertions are meaningful. See the
[supported forms and remaining limits](NODE_TEST_INTEGRITY.md).

The final local 0.5.0 run on 2026-09-20 matched all nine declared outcomes and
both CI-slot verdicts, preserved the source checkout and validated 24 receipts.
Evidence: `.exitzero/pilots/node-20260920T060028Z-53785801/summary.json`.
Its recorded runner/runtime hashes match the reviewed implementation. Tool
versions match the original macOS run. Three alternating timing pairs gave
direct/gate medians of **4.818/4.980 seconds**; the gate now includes the extra
integrity check. Short runs and randomized upstream timers prevent treating the
difference as a general performance estimate or comparing improvements across runs.

[Linux run 35493351891](https://github.com/ictechgy/exitzero/actions/runs/35493351891)
at `ce30e5b3acb2cf17783f46a9cf4fa0e1a1e97bb1` matched the same nine cases and both
CI-slot verdicts. All 24 downloaded receipts matched the recorded exits and
output; runtime/runner hashes matched the local checkout and the source remained
unchanged. It used the same dependency versions with bundled npm 10.9.3. Direct/gate
medians were **7.916/8.121 seconds** in that separate environment.

[Main CI 35493315255](https://github.com/ictechgy/exitzero/actions/runs/35493315255)
also passed the Python 3.11/3.14 gates and installed-wheel checks. The wheel runner
retained ten receipts, including installed Node baseline/deletion/skip/repair
verdicts of 0/1/1/0 and the existing actual Git-hook checks.

## Reproduce and inspect evidence

Use Node 22.20.0, npm 11.6.2 and Python 3.11+. Prepare the reviewed public checkout
and its development dependencies in a disposable directory. Install with
`npm install --ignore-scripts --no-audit --no-fund --package-lock=true`, using
empty per-process npm user/global configuration and an isolated cache. The
explicit lock option overrides this project's public `package-lock=false`
setting. No exitzero runtime dependency is added.

```sh
python3 scripts/pilot_node.py \
  --source /path/to/pinned/p-limit \
  --dependencies /path/to/prepared/p-limit --integrity
```

The dependency directory must contain `node_modules` and `package-lock.json`.
The runner performs no fetch or installation and sets npm to offline mode.
It writes `.exitzero/pilots/node-<timestamp>-<id>/` with:

- `summary.json`: source/code/lock hashes, versions, expected outcomes, test counts,
  direct command diagnostics, receipt paths and source-preservation result.
- `source-manifest.json` and `package-lock.json`: source and dependency resolution.
- Command logs, separate case copies, generated policies and saved JSON receipts.
- Three alternating direct/gate timing pairs, running the same three commands
  afresh without receipt reuse. These short samples are not a general benchmark.

Upstream uses dependency ranges and does not track a lockfile. Each fresh install
can resolve different versions; the recorded lock supports `npm ci` reproduction
of that run. The runner records versions and checks behavior, but does not attest
the installed dependency tree against the lockfile.

Omit `--integrity` to reproduce the original command-only control. The manual
[Node pilot workflow](../.github/workflows/node-pilot.yml) now enables integrity
on Linux, with the public source commit and Node version pinned. It retains
logs, lockfiles and receipts on failures as well as successes. It runs only on
manual dispatch from main, uses read-only repository permissions, and does not
modify p-limit, install IDE hooks or change branch protection. Its negative cases
are scored inside the runner; an expected failing gate should not fail the whole
pilot job.

## Earlier attempt retained

The initial candidate was `lukeed/clsx` 2.1.1 at
`925494cf31bcd97d3337aacd34e659e80cae7fe2`. Its unmodified `npm test` failed under
Node 22.20.0 with `Cannot use import statement outside a module`, both with the
default loader behavior and `--no-experimental-require-module`. Its build passed.
This happened before any gate integration; it is an upstream/environment failure,
not a gate false positive. The candidate was replaced rather than changing its
tests or claiming it passed. Local logs remain in `.exitzero/node-pilot/`.
