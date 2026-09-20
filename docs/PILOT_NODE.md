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

## Independent cases

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
tools: deleting a test can still pass. Python's `python.test-integrity` does not
cover JavaScript. A useful follow-up is a separately reviewed JS/TS test-integrity
check, starting with deleted tests and newly skipped tests.

## Recorded local run

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
  --dependencies /path/to/prepared/p-limit
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

The manual [Node pilot workflow](../.github/workflows/node-pilot.yml) uses the same
runner on Linux, with the public source commit and Node version pinned. It retains
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
