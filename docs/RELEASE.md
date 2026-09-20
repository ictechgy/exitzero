# Offline release check

Build and verify a wheel without downloading packages or changing global
configuration. The build command uses the repository's existing virtual
environment; `--no-build-isolation` keeps the build offline.

```sh
cd /path/to/exitzero
.venv/bin/python -m pip wheel --no-index --no-build-isolation --no-deps . --wheel-dir dist
.venv/bin/python scripts/release_check.py --wheel dist/exitzero-*.whl
```

`release_check.py` creates a fresh temporary virtual environment, runs its
embedded `ensurepip`, installs only the selected wheel with `--no-index
--no-deps`, and invokes the installed `exitzero` command. It checks `init`,
`check`, `lint-config`, `doctor`, `report`, both `verify` and `harness` plugin entry-point
aliases, and equality between JSON output and saved receipts.

It also generates a Node policy with a trusted Git baseline and verifies the
installed static test-integrity check's baseline, deletion, skip and repair
receipts. This packaging check uses a no-op command and requires no Node
installation; the separate [Node pilot](PILOT_NODE.md) supplies real tool evidence.

The runner also creates an isolated temporary Git repository and installs the
wheel's pre-commit hook. It records an accepted commit, rejects syntactically
invalid staged Python while keeping `HEAD` unchanged, and accepts the repaired
staged source. Git author and committer identity are explicit test values,
commit signing is disabled for those commands, and global/system Git settings
are ignored.

The temporary repository and virtual environment are removed after the run.
The machine-readable schema-1 summary, bounded command output, and every gate
receipt are retained under `.exitzero/releases/<run-id>/`. Command evidence
applies only a small credential-like `token`/`password`/`secret`/`api-key`
pattern redaction; it is not a general secret scanner. A missing receipt,
receipt mismatch, dependency in wheel metadata, missing runtime package, failed
hook assertion, or unexpected exit code makes the runner exit 1. The runner
does not publish, tag, push, contact an IDE, or call a model.

For a compact result, inspect `summary.json`; detailed command output is in
`commands.json`, and copied run receipts are in `receipts/`.
