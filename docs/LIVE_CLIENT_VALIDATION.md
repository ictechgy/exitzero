# Claude and Gemini validation attempts

Run date: 2026-09-20. The full model-driven repair loops remain **unverified**
because both configured accounts rejected their live requests. The Gemini
component test below found and verified a separate adapter fix.

## Actual CLI attempts

Both clients used isolated synthetic projects and the published exitzero 0.5.0
wheel. Each project contained one Python syntax error, a generated policy and
the installed native stop adapter. Setup lint passed. The prompt requested an
initial `READY` response, followed by repair of only the seeded file if the native
hook blocked completion. Neither client was asked to simulate a hook or run the
gate itself. Credentials were handled by the installed clients; credential files
were not inspected or copied by the validation driver.

| Client | Attempt | Observed result | Native stop receipts |
| --- | --- | --- | --- |
| Claude Code 2.1.278 | Print mode, Sonnet alias, restricted Read/Edit tools, explicit generated settings, six-turn / USD 1 caps | Exit 1, HTTP 429 weekly limit; no model response | 0 |
| Gemini CLI 0.44.1 | Headless mode, eight-turn cap, project read/edit tools, extensions/MCP excluded for this test | Exit 1, `IneligibleTierError`, reason `UNSUPPORTED_CLIENT` | 0 |

Claude's stream marked `is_error: true` and `terminal_reason: api_error`; its
`subtype: success` field alone would have been misleading. The exit and error
fields were used to score the attempt. Gemini rejected the account's Code Assist
for individuals tier and directed it to Antigravity. These are observations for
the validation accounts, not claims that all accounts lack access.

Policy, generated instructions and hook settings remained unchanged by both
attempts; the seeded files remained broken. Setup receipts are configuration
evidence only. Completing the live checks requires usable account quota/access
and another actual fail→block→agent repair→pass session.

## Gemini timeout reproduction and fix

Gemini defines hook timeouts in milliseconds. exitzero through 0.5.0 reused the
seconds value `120`, leaving only 120 ms for its process. Version 0.5.1 generates
`120000` and migrates the legacy value when `hooks install --adapter gemini` is
rerun. Other custom budgets and foreign entries remain unchanged. Doctor detects
the legacy value even when the installation manifest is internally consistent.

The local test imported `HookRunner` and `AfterAgentHookOutput` from the installed
Gemini CLI 0.44.1 bundle and invoked the real command executor in a trusted,
synthetic working directory. The driver supplied a synthetic AfterAgent input and
minimal configuration interfaces. This establishes native command-execution and
output-parsing behavior. Model inference, authentication and workspace trust UI
were not exercised by this component test.

The configured check waited 350 ms and parsed the seeded Python file. It used
the actual command emitted by the installed exitzero console script.

| Control | Native executor | Gate evidence |
| --- | --- | --- |
| Published 0.5.0 hook, 120 ms | Timed out after 125 ms; no parsed output/block decision | Host feedback was lost |
| Corrected 0.5.1 hook, broken file | Completed in 451 ms; `block` recognized as blocking | Exit 1 receipt `b7f53a3b1ddb421eacf95098aa725931` |
| Same hook after driver repairs the file | Completed in 445 ms; empty output allowed completion | Exit 0 receipt `aad845d74ed24086ae069051ae089a8a` |

The repair was made by the test driver, not an agent. Both new receipts identify
exitzero 0.5.1 and the `PostToolUse` gate slot; the failed hook's reason references
the exact saved receipt. The installed doctor rejected the old budget, then passed
after reinstalling. Wheel/runtime hashes matched the tested source.

The native executor accepts both `block` and `deny`; no wire-decision change was
needed. The corrected budget is still finite: require CI for merge protection.
Native behavior was checked against the installed implementation and the official
[Gemini hook reference](https://geminicli.com/docs/hooks/reference/).

## Retained evidence

Local evidence is under `.exitzero/live-validation/claude-gemini/`:

- Client versions, scrubbed output streams and `claude-attempt-1.json` /
  `gemini-attempt-1.json`; neither attempt has native stop receipts.
- Setup lint receipts and installed legacy/migrated doctor receipts.
- `gemini-timeout-before.json`, `gemini-native-failure.json`,
  `gemini-native-repair.json` and copied gate receipts.

Temporary project, driver and installed-package paths are recorded locally for
reproduction and cleanup; personal paths and client output are not committed.
