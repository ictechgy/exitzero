# Design references

The brief names weftgate, harness-eval and SWE-Gate as prior art. Their concepts
inform these boundaries: executable verification before merge, fixture-based
evaluation, and independent evidence of patch correctness. No code, licensed
assets or implementation text has been copied from those projects.

Exact project identities and public repository links still need source
verification; names alone are not enough to attribute a specific implementation
accurately. These names describe the requested design influences, not a verified
compatibility or implementation comparison.

Cursor's public hooks documentation is the intended compatibility reference:
<https://cursor.com/docs/agent/hooks>. The local adapter has protocol fixture
tests; those tests do not establish compatibility with every installed Cursor
release. Verify current event names and schemas against official documentation
before publishing a compatibility claim.

Design decisions made independently in this milestone:

- Keep policy parsing, hook dispatch and receipt persistence in core.
- Let plugins register checks, linters, event handlers and CLI commands.
- Keep config lint separate from command execution.
- Treat receipt persistence as part of gate success, including failure runs.
- Make future gateway and richer ledger work explicit stubs.
