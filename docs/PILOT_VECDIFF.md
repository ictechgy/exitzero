# Real-repository pilot: vecdiff

Run date: 2026-09-16. Source commit:
`fd561f863aabc3937d866bf5d7d1f4090c185284`.

The pilot uses an existing local Python 3.13 environment with NumPy, pytest and
optional FAISS. It exports the pinned source into separate case directories and
uses `init --profile python` to generate the policy, including a test command and
an independent numeric review command. The original source's tracked file hashes,
HEAD and Git status are checked before and after the experiment.

## Results

The upstream baseline had **123 passed, 1 skipped**, with no failures or errors.
The skipped test covers operation without FAISS; FAISS was installed, so the
three FAISS integration cases ran instead. The recorded dependency versions were
NumPy 2.5.2, pytest 9.1.1 and faiss-cpu 1.15.0. No package was downloaded.

| Independent case | `check` | `lint-config` | Evidence |
| --- | --- | --- | --- |
| Baseline | 0 | 0 | Generated policy and all configured checks pass |
| Misspelled NumPy module name | 1 | 0 | Import rule, test command and review command fail |
| Invented `np.as_array` API | 1 | 0 | Both executable commands fail; static import checking passes |
| Existing test body replaced by `pass` | 1 | 0 | Test-quality rule fails while pytest still passes |
| Gate verdict always returns success | 1 | 0 | Existing CLI exit-code assertions fail |
| Normalization returns the unnormalized array | 1 | 0 | Existing tests and independent numeric review both fail |
| Generated AGENTS section changed | 1 | 1 | Policy/document drift is detected |

All seven cases matched the expected outcomes, with fourteen persisted gate
receipts. Original AGENTS guidance was preserved, and the source checkout's
measured state remained unchanged.

The independent review command checks unit normalization, finite zero vectors,
float32 output, descending neighbor order and exclusion of each query itself.
The invented-API case demonstrates why `allow_modules` must be paired with real
execution: permitting an external module does not validate its API surface.
These six controlled faults do not measure general detection accuracy.

## Reproduce

Use a local vecdiff checkout at the pinned revision and an existing Python with
NumPy and pytest. FAISS is optional; the saved result records skips and versions.

```sh
python3 scripts/pilot_vecdiff.py \
  --source /path/to/vecdiff \
  --python /path/to/python-with-numpy-and-pytest
```

The test adapter disables automatic third-party pytest plugin loading and
verifies that it imports vecdiff from the exported snapshot. Source imports from
`src` and `tests` are checked, while external module names are explicitly allowed
in the generated policy. Every case starts from a fresh baseline copy.

The runner exits 0 only after baseline execution, all expected gate outcomes,
saved-receipt equality and source-integrity checks pass. It does not fetch source
or install dependencies. The helpers and review contract are in
[examples/vecdiff](../examples/vecdiff).

The recorded evidence is
`.exitzero/pilots/vecdiff-20260916T043618Z-3ec7c14a/summary.json` in the local
workspace. Each new invocation saves a separate directory containing the source
manifest, gate code hashes, pytest counts, policy initialization log, numeric
review log, case outputs and saved receipts. These local artifacts are ignored
by Git. Hosted CI and live editor integrations are separate checks.
