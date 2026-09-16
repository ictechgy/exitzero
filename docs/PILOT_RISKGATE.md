# Real-repository pilot: riskgate

Run date: 2026-09-16. Source commit:
`62fc59bd5899c25e422d070915f2d2cd8d55ec51`.

The pilot exports the 56 committed files from a local riskgate checkout. All
policy setup, generated documentation and fault injections happen in separate
copies under `.exitzero/pilots/`. The source checkout's tracked file hashes,
HEAD and Git status are compared before and after execution.

## Results

The upstream baseline collected 214 tests: **211 passed, 3 skipped**, with no
failures or errors. The bundled policy lint passed and its **15/15** declared
cases passed. The gate's baseline reported no findings for the configured scope.

| Independent case | `check` | `lint-config` | Expected evidence |
| --- | --- | --- | --- |
| Unchanged source with generated policy section | 0 | 0 | All seven registered checks/linters pass |
| Misspelled policy module import in `riskgate/cli.py` | 1 | 0 | `imports`, regression suite and both policy commands fail |
| Existing CLI exit-code test replaced by `pass` | 1 | 0 | `test-quality` fails while the upstream suite still succeeds |
| CLI always returns success for policy test failures | 1 | 0 | The upstream exit-code regression test fails |
| Generated AGENTS section changed | 1 | 1 | `harness.agents.drift` fails |

All five outcomes matched their expectations. Each fault uses a fresh copy of
the same baseline. Existing AGENTS instructions remained intact during section
generation. The source checkout's measured state remained unchanged.

The empty-test case demonstrates additional value beyond checking a test
runner's exit code. These four injected defects are a bounded experiment; they
do not establish general defect detection accuracy or semantic test quality.

## Explicit exclusions

The test adapter marks these upstream classes as skipped at runtime, without
editing upstream test source:

- `PrototypeParityTest` (2 tests) requires a personal prototype outside the pinned
  repository.
- `Agent2PerfettoValidatorTest` (1 test) requires an unpinned sibling checkout.

These exclusions apply equally to the baseline and every mutation. Other
platform-dependent tests keep their upstream skip conditions. On the recorded
macOS run there were no additional skips. The adapter is test selection, not a
network or filesystem sandbox. Hooks are not installed into real agent settings.

## Reproduce

Use a local riskgate checkout whose HEAD matches the commit above. From the
exitzero checkout, run:

```sh
python3 scripts/pilot_riskgate.py --source /path/to/riskgate
```

No fetch or package installation is performed. The runner exits 0 only when the
upstream baseline, all expected gate outcomes and source-integrity checks pass.
Exit 1 means the pilot did not satisfy its expectations; inspect the saved logs.

The reusable policy and explicit test-selection adapter live in
[examples/riskgate](../examples/riskgate). This profile is specific to the reviewed
source revision; review the integration when upgrading riskgate.

Each run creates a unique evidence directory containing:

- `summary.json`: expected versus observed exits, findings, source commit, gate
  code hashes, per-case test counts and receipt paths.
- `source-manifest.json`: hashes of the exported committed files.
- `upstream-*.log`: direct baseline test and policy command output.
- Separate case copies, check/lint output logs, and ten saved gate receipts.

The first recorded run is
`.exitzero/pilots/riskgate-20260916T041646Z-a0e16ab6/summary.json` in the local
workspace. Evidence directories are ignored by Git; rerunning the command
produces a new record. This pilot used the local CLI and did not run hosted CI
or a live IDE integration.
