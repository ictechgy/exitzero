---
name: exitzero-completion-gate
description: Verify task completion with the project's exitzero gate before declaring work done. Use when finishing a coding task in a repository that configures exitzero (exitzero.toml present) and no stop hook is installed.
---

# exitzero completion gate

When this repository has an `exitzero.toml` policy, the project defines which
checks must pass before work counts as complete. Your claim of completion is
only as good as the evidence behind it.

## Before you declare a task done

1. Call the `check_completion` MCP tool (server name `exitzero`). It runs the
   project's real `check` gate and returns a verdict plus a receipt path.
2. If the verdict is `failed`, read the findings, repair the violations, and
   call `check_completion` again. Do not declare completion on a failed
   verdict.
3. If the verdict is `passed`, cite the receipt path in your summary — it is
   the durable evidence for what was verified.
4. If the tool reports an operational error (isError), the gate could not run;
   say so plainly instead of implying verification happened.

## Notes

- If a stop hook is installed (`.cursor/hooks.json`, `.claude/settings.json`,
  `.codex/hooks.json`), the gate already runs automatically at turn end — the
  MCP tool is for hosts without hook support.
- `check_completion` verifies the configured checks; it does not prove the
  change matches intent. Keep receipts as evidence, not as a substitute for
  review.
