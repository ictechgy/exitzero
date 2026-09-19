# CI receipt attestations

The [optional workflow recipe](../examples/ci/attested-gate.yml) runs a fresh full
gate, signs the resulting JSON artifact through `actions/attest@v4`, and uploads
the artifact even when the gate fails. Signing runs only after success. This
uses GitHub's OIDC identity rather than a signing key stored in the repository.
It is a recipe, not an enabled workflow or evidence of a hosted signing run.

Use it only for trusted same-repository changes and a reviewed workflow/policy.
The example explicitly fails for fork PRs instead of skipping a required job.
Give such changes a separate approved validation path; never switch to
`pull_request_target` and execute untrusted patch code with write/OIDC permissions.
Protect workflow, policy and test changes through review. Pin the gate release
or a reviewed wheel that supports your policy; 0.3.0 does not understand the new
`enforcement` field. Install the project's test tools before the gate step.

After adapting the example, make `exitzero-attested` a required check for the
target branch and test rejection on a deliberately failing PR. Review bypass
permissions and merge-queue behavior. A signed artifact by itself does not
establish a required merge check, nor that its policy covers every requirement.
Advisory failures remain failed checks inside an otherwise successful gate.

Download the receipt artifact from the intended CI run, then verify its digest
and provenance:

```sh
gh attestation verify ci-receipt.json --repo OWNER/REPO --format json
```

Inspect the verified provenance's repository, workflow identity and source commit
against the intended merge candidate. Also inspect the receipt's command,
exit code, policy hash, selected-input hashes and requirement/check outcomes.
Require a full `check` (no `diff` scope) for this acceptance path. Do not accept
an unrelated older signed run just because its signature verifies. Do not restore
an agent-controlled `.exitzero/` cache or use `--reuse` in the attesting job.

For local unsigned in-toto exports, select a receipt explicitly:

```sh
exitzero report --run-id RUN_ID --format intoto
```

`RUN_ID` is the 32-character id returned by that gate run. This avoids selecting
a later doctor/lint run. The subject SHA-256 hashes the actual persisted receipt
file bytes, and the predicate contains the decoded receipt. Export is not signing
or rerunning the gate. Local receipts can still be rewritten by someone with
filesystem write access; their hash alone is not trusted provenance.

Official references, checked 2026-09-19:
[GitHub artifact attestations](https://docs.github.com/en/actions/how-tos/secure-your-work/use-artifact-attestations/use-artifact-attestations)
and [the attest action](https://github.com/actions/attest).
