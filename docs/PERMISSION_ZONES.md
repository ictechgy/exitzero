# Permission zones

Permission zones check whether a candidate changed files it was authorized to
edit. They also catch an agent changing the policy to approve its own work.
The gate loads the zones from an independently supplied local Git commit,
before loading any candidate plugins or running commands.

Add this to a reviewed policy and establish that commit as the trusted base:

```toml
[permissions]
editable = ["src/**", "docs/**"]
protected = ["tests/**", "AGENTS.md"]
immutable = [".github/**", ".cursor/**", ".claude/**", "scripts/verify.py"]
```

Then invoke an installed, trusted copy of ExitZero from the repository root:

```sh
exitzero check --trust-base TRUSTED_COMMIT --format json
exitzero doctor --trust-base TRUSTED_COMMIT --format json
exitzero hooks run --slot CI --trust-base TRUSTED_COMMIT --format json
```

`TRUSTED_COMMIT` is a placeholder for a reviewed commit hash or a protected ref
already available locally. In CI, supply it from the trusted workflow or base
branch event. Do not accept it from a candidate-controlled file or command.
The check never fetches a missing Git object. The selected policy must already
exist in the trusted commit and declare all three zone arrays.

| Zone | Gate decision |
| --- | --- |
| `editable` | Changes are allowed; ordinary checks still need to pass. |
| `protected` | Changes fail the gate with `review_required`. |
| `immutable` | Changes fail the gate with `denied`. |
| No matching zone | Changes fail the gate with `denied`. |

The strongest matching zone wins: immutable, protected, then editable. The
selected policy file is always immutable, even if a broad editable pattern
matches it. No worktree annotation, model statement, or local approval file
waives a protected change. A maintainer must review it through a separately
controlled change process and establish a new trusted baseline. This version
does not verify human approvals or provide a per-change waiver command.

The receipt's `permissions` object contains the resolved base commit, trusted
policy digest, changed paths, zone decisions and before/after digests.
`ledger-publish` carries these decisions into its JSON and Markdown records.
They remain local unsigned observations unless an external CI attestation
workflow authenticates the receipt. A policy with zones requires `--trust-base`
for `check` and hook runs, or an operator pin installed through the
[policy pack](POLICY_PACKS.md). `lint-config` validates the declaration; `doctor`
without a base reports that authority has not been inspected. Hook installation
accepts an explicit `--trust-base`; it does not choose one on the user's behalf.

## Scope and limits

The comparison includes baseline files, currently tracked files, and untracked
files not ignored by Git. It reads raw regular-file bytes and executable modes;
Git clean filters and assume-unchanged/skip-worktree flags cannot hide edits.
Deleted and renamed files are checked as deletions/additions. `--diff` and
`--reuse` never narrow or reuse permission decisions. A second snapshot rejects
changes to the baseline ref, file contents, modes or membership during the run.

Ignored untracked files and transient edits restored before the final snapshot
are outside this evidence. This is a merge-time gate, not an OS sandbox, a live
tool-call monitor, or proof that a test is semantically correct. Protect the CI
invocation and installed runner too, and keep executable plugins/check scripts
in protected or immutable zones. Required CI is the merge barrier; Cursor stop
continues to provide repair feedback.

Run from the Git worktree root. Symlinks, submodules, credential-like tracked
paths, unsupported path names and unavailable Git objects are errors rather
than silently omitted evidence. Inspection is limited to 20,000 paths, 32 MiB
per file and 256 MiB of file content. A missing baseline or incomplete inspection
exits 2; a diagnosed zone violation exits 1. No credentials or file contents are
included in receipts.
