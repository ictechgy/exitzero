# Design references

Primary sources reviewed on 2026-09-16. The implementation is independently
written; these sources informed design decisions and protocol compatibility.

| Source | Observed idea | Decision for aidd-gate |
| --- | --- | --- |
| [Weft / weftgate project site](https://weftgate.com/) and its linked [repository](https://github.com/spranab/weft) | Verification evidence is associated with the material being accepted. | Core requires a receipt with policy and selected-input hashes. Local JSON receipts remain unsigned; stronger provenance belongs to future ledger work. |
| [harness-eval (whchoi98)](https://github.com/whchoi98/harness-eval) | Quick, Standard and Full modes combine deterministic checks with progressively deeper configuration evaluation. | Ship deterministic config lint in v1; deepen fixture scoring and optional agent evaluation in v1.1. |
| [SWE-Gate paper, v1](https://arxiv.org/abs/2609.04167v1) and [replication repository](https://github.com/DeepSoftwareAnalytics/SWE-Gate) | Functional correctness and review-derived acceptance constraints have separate tests. | Use named command checks for executable review requirements; test exact error text, types and ordering. v1.1 should report functional and constraint outcomes separately and jointly. |

These are our design interpretations, not claims that aidd-gate reproduces the
referenced systems or their evaluation results. The Weft project site was read;
its repository identity comes from the site's source link. For the brief's
`harness-eval` reference, this document uses the linked Claude Code configuration
evaluation project.

## Cursor protocol

The [official hook reference](https://cursor.com/docs/hooks) documents command and
prompt hooks, native `preToolUse`/`postToolUse` events, and the `stop` follow-up
protocol. It also distinguishes JSON permission responses from process exit
codes. The earlier `/docs/agent/hooks` address redirects here.

The adapter now accepts native tool events, preserves existing prompt hooks, and
installs a one-follow-up stop limit. Suppressing follow-up after an `aborted` stop
is an aidd-gate choice to respect user cancellation. See [hook setup](HOOKS.md)
for the supported mapping and exit behavior.

`tests/test_hooks.py` exercises adapter subprocesses and reads their persisted
receipts. `tests/test_harness.py` covers command/prompt config shapes without
executing config values. This establishes local protocol coverage. A live Cursor
session and hosted GitHub Actions remain separate integration checks.

## Core boundary

Core owns policy parsing, hook dispatch, plugin discovery and receipt persistence.
Plugins register their checks, linters, event handlers and CLI commands. The
gateway and richer ledger packages stay as explicit stubs for the roadmap.
