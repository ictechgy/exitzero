# Live client validation

A fresh [0.6.0 riskgate validation](PILOT_POLICY_PACK.md#fresh-native-antigravity-evidence)
on 2026-09-21 passed Antigravity's native Stop repair loop with a trusted policy
baseline. It verifies 12 real CLI contracts in a confined gate subprocess;
the full upstream suite is separate offline evidence. The records below retain
their original package versions and dates.

Run date: 2026-09-20. Claude Code and Gemini CLI model-driven repair loops remain
**unverified** because the configured accounts rejected their live requests.
The Gemini component test below verified a separate adapter fix. Subsequent
Antigravity validation passed in interactive and headless modes. Devin CLI passed
in an interactive sandbox with one approved fixture edit. Protected OpenCode and
Zcode backend sessions also completed advisory MCP repair loops with Qwen and
GLM respectively. Kimi subsequently completed its real-model MCP loop after an
isolated native login. These are separate client results.

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

## Antigravity follow-up

The user offered Antigravity as an available alternative and flagged security
concerns about other clients. Only Antigravity was used for this follow-up;
OpenCode, Kimi, Zcode and Devin were not run during that stage. Validation used
synthetic temporary projects and the published exitzero 0.5.1 wheel, not the
implementation repository.

Antigravity CLI 1.2.7 with `gemini-3.8-flash-low` completed the real model-driven
loop in both modes:

| Mode | Failed Stop receipt | Agent action | Passing Stop receipt |
| --- | --- | --- | --- |
| Interactive TTY | `a509b91207d04d7481aa521916ec56f1` (exit 1) | Read the receipt/source and repair `src/broken.py` | `402f5529ad4c4a1ea8b898b2cdc3f1f2` (exit 0) |
| Headless `-p`, explicit `--new-project` | `53c0eaf436cd49368866ea76673c7c98` (exit 1) | Read the receipt/source and repair `src/broken.py` | `87122e31f95a4850a292c4f752876d5c` (exit 0) |

Each run produced a failed and passing native gate receipt. The interactive
observer recorded two Stop events in one conversation; the separate headless
observer recorded the final Stop only. The sole
failure finding was the seeded syntax error; the repaired file defines
`answer()` returning 42. Policy, AGENTS and hook fingerprints remained unchanged.
The controller never repaired the source or invoked a hook itself during these
sessions. Both clients exited 0; the interactive controller closed the idle
session after collecting the successful receipt.

Runs used `--sandbox`, an explicit new project and test-only PreToolUse guards.
The guards used matcher/`hooks` groups and allowed file reads for the receipt and
fixture, with edits restricted to the fixture. Native guard logs recorded the
actual file operations; no shell, MCP or network tool operation was observed.
This is evidence for these bounded sessions, not a general client security audit.

An exploratory first interactive run had no record of its flat PreToolUse guard
executing and its PTY teardown stalled. The recorded result above uses a fresh
project, the documented grouped guard shape and a corrected bounded PTY driver.
That exploratory run is retained separately and is not used as scope-enforcement
evidence.

The successful headless run supersedes the earlier claim that this CLI version
never executes hooks in `-p` mode. Project selection was explicit in both new
runs; this does not establish why every earlier attempt failed. It also does not
resolve the separate Claude Code and Gemini CLI account blockers.

## Devin CLI follow-up

The user subsequently authorized Devin. Devin CLI 3000.10.31 (`b98cc431`) with
`gemini-3-8-flash-low` ran locally against a synthetic temporary project and the
published exitzero 0.5.1 wheel. No cloud task or repository handoff was created.
Project `.devin/config.json` contained a native Stop hook calling
`exitzero hooks run --adapter claude --event stop`. This reuses the compatible
JSON protocol; it is not a dedicated Devin adapter or Claude Code live evidence.

The interactive `--sandbox` session completed the real repair loop:

| Stage | Observed evidence |
| --- | --- |
| Initial stop | Receipt `4dcd044d5e3045ec8ab090ada9447dfd`, exit 1; seeded syntax error only |
| Agent repair | Read the exact receipt and `src/broken.py`, then propose `def answer(:` → `def answer():` |
| Edit approval | Controller reviewed the displayed one-file diff and chose **Approve once** |
| Final stop | Receipt `23a56de755f94912b1f4da446eb1f13b`, exit 0; `answer()` returns 42 |

Two Stop observations and three scoped tool callbacks belonged to the same
session. Policy, AGENTS, project hooks, scope guard and initialized test config
hashes were unchanged. Receipt input hashes matched the broken and repaired
source. The controller never edited the source or invoked the gate during the
session; it closed the completed interactive client after collecting both
receipts, with client exit 0. Runtime hashes matched source, wheel and installed
package for all 21 Python files.

Two earlier `-p --sandbox` attempts did fire Stop and deliver the failed receipt,
but stopped at the direct file edit's permission prompt. Each exited 0 with one
failed gate receipt and unchanged broken source; neither is scored as a pass.
The first attempt also initialized the disposable user config. Retrying with an
absolute write scope did not resolve the prompt. The successful run retained
the sandbox and approved only the displayed edit interactively; unattended
sandbox repair was not established. This matches the documented
[direct-edit approval requirement](https://docs.devin.ai/cli/reference/permissions).

An explicit disposable user config disabled cross-client imports, subagents and
updates; the native MCP listing reported no configured servers. Test-only
PreToolUse guards restricted reads to the fixture/policy/receipts and writes to
the fixture. Only receipt read, source read and source edit were observed. Shell,
web and MCP calls were disallowed. Existing native login was used without
inspecting or copying credential files. This bounded session is not a general
Devin security assessment. Required CI remains the merge barrier.

## Protected OpenCode follow-up

OpenCode 1.18.31 with Qwen `qwen3.8-max` through the approved Alibaba Token Plan
endpoint completed a real model-driven MCP repair loop using published exitzero
0.5.1. The project was a synthetic Git repository with one seeded syntax error.
The run used the operator's installed `safecode` protection engine (agentbelt),
with a stricter test profile rather than the normal launcher's defaults.

| Stage | Observed evidence |
| --- | --- |
| Initial MCP call | `exitzero-gate_check_completion` failed; receipt `f32b8dc47d96476fbbeaa777d2ce3693`, exit 1 |
| Model repair | Read the source and failing receipt, then edit only `src/broken.py` |
| Final MCP call | Same tool passed; receipt `10d9686b4f8f47fdb8491bf4b0d2f30c`, exit 0 |
| Completion | Model read the passing receipt; native client exited 0 |

Seven native tool operations completed in one session: two gate calls, four file
reads and one edit. The repaired function returns 42. Policy and AGENTS hashes
matched both receipts; the launcher also checked its configuration and Git
controls for changes. The controller did not repair source or call the gate
during the live session. All 21 runtime Python files matched source, published
wheel and installed package.

The macOS process sandbox limited filesystem access to the synthetic workspace,
ephemeral client state and required runtime assets. The test omitted host Git
identity, GitHub tokens, screenshot/status relays and development ports. Native
tool permissions allowed only fixture/receipt reads, fixture edits and the one
MCP gate. The package, policy and instructions were protected against writes.

A local broker held the selected provider's API key outside the sandbox. The
client received a temporary broker token, not the provider key. The broker pinned
the model and API endpoint, rejected redirects and unauthenticated requests, and
replaced workspace/home paths and the hostname in outbound JSON. All seven API
requests passed checks for absence of the raw home path, hostname and provider
key in their bodies. These are controls of this local test setup, not protection
features shipped by exitzero or a privacy guarantee for arbitrary repositories.

Before the live run, 18 synthetic boundary checks passed, including outside-file
and symlink denials, child-process confinement, environment filtering, blocked
Keychain/clipboard service lookup, and network restrictions. An actual OpenCode
session against a local fake model also failed to read an outside canary; its
contents never appeared in captured model requests. No real personal files were
used as test data.

Earlier attempts are retained separately. One could not reach the broker because
direct loopback was blocked; clearing the proxy-bypass variables routed it through
the permitted proxy. Another produced only a failed receipt before timing out
on file-tool permissions. A local scripted-model reproduction passed after adding
a synthetic Git root, with the same file allow rules. Only the final real-model
run above is scored as successful.

This integration uses advisory MCP, not a Stop hook or merge barrier. Subsequent
Zcode and Kimi work is recorded separately below.

## Protected Zcode backend follow-up

The verified Zcode CLI 0.16.5 bundled with desktop 3.12.3 completed a real MCP
repair loop with `glm-5.3-flash` through the approved Z.ai Coding Plan endpoint.
The test used published exitzero 0.5.1, a synthetic Git workspace and the installed
Zcode Safe protection engine under a stricter task profile. It launched the native
`app-server --stdio` backend with a terminal presentation surface; no GUI ran.

| Stage | Observed evidence |
| --- | --- |
| Initial gate | Receipt `74c9945e04334743b39e8a01c142ea90`, exit 1; seeded syntax error |
| Model action | Read that receipt and `src/broken.py`, then edit only the fixture |
| Final gate | Receipt `5f73f2f5e68a4855abb08d85fa7d7663`, exit 0; `answer()` returns 42 |

Five native tool operations completed in one session, each observed by both
PreToolUse and PostToolUse hooks. The protocol controller could answer scoped
permission requests, but none required its approval in the successful run:
the configured native guards allowed the five authorized operations. The backend
and controller exited 0. Five real model API calls completed in 29.279 seconds.
The controller never repaired source or invoked the gate during the model session.

The selected API key stayed in the authenticated, endpoint-pinned local broker
outside the sandbox. Request bodies were checked after workspace/home/hostname
redaction. Native tools were limited to Read, Edit, Write and the one MCP gate;
file guards restricted access to the fixture and receipts. Protected policy and
instruction hashes matched both receipts, and all 21 runtime files matched source,
wheel and installed package. Private runtime sockets were allowed only in owned
temporary directories; a live unrelated host-socket negative control remained
blocked. This is bounded backend evidence, not GUI privacy certification.

Setup controls found client-specific constraints. In the restricted one-shot
setup, the CLI advertised flags its parser rejected, walked upward for dotenv
files, and denied the gate with `No permission client configured`. The final
app-server route avoids that dotenv startup path
and supplies the protocol client; it does not use the exploratory dotenv shim
or disable permission checks. The guard's earlier doctor failure came from checking
only `out/host/index.js`: routing moved to its imported `chunk-OIOBEZTZ.js`.

A subsequent local agentbelt fix follows bounded static imports within the packed
ASAR modules. Required markers must occur together in a linked module; unrelated
chunks, unsafe paths, missing entries and traversal-budget overruns are rejected.
The 26 focused tests passed, including rejection of changed hashes without rewriting
the baseline. After targeted installation, `agentbelt doctor` exited 0 and reported
the existing OpenCode, Zcode and Kimi hashes as verified. Recorded compatibility
and Zcode-profile bytes remained unchanged. This corrects compatibility discovery;
it does not certify the GUI's privacy or change its launcher permissions.

## Kimi offline preparation

Kimi Code 0.43.1 ran in a separate, credential-free safekimi engine profile against
a local scripted model. Native MCP calls produced exit 1 receipt
`6fc0972a8c2c46fab15fd9b1383bc616`, then exit 0 receipt
`cd86fbd104614aabadf9208c7d950163` after the scripted response requested a fixture
write. All five native calls returned results, and source/policy hashes matched
the saved receipts. This is component evidence, not a real-model repair result.

Only Read, Write and the completion gate were exposed. Four PreToolUse records
covered the three file operations and final gate; the initial gate had no hook
observation. Complete hook coverage is therefore not established. Native permission
rules and OS confinement remain separate controls. The hook mechanism itself is
[documented as fail-open on errors or timeouts](https://moonshotai.github.io/kimi-code/en/customization/hooks).

The local control allowed one newly created, authenticated broker port, with no
public API domains allowed. A positive/negative socket control confirmed that
the broker was reachable and an unrelated live host service remained blocked.
The original Kimi credentials were not copied. The subsequent real-model test used
a separate isolated login profile, as recorded below.

## Protected Kimi live follow-up

After the operator completed native login in the isolated profile, Kimi Code
0.43.1 completed the real MCP repair loop with its configured default model,
`kimi-for-coding`, through `api.kimi.ai/coding/v1`. The test used the installed
safekimi protection engine, a separate ephemeral client profile and published
exitzero 0.5.1 in the synthetic Git workspace.

| Stage | Observed evidence |
| --- | --- |
| Initial gate | Receipt `dc54ef029b4b47abafb60e13e830b04f`, exit 1; seeded syntax error |
| Model action | Read that receipt and the source, then Write only `src/broken.py` |
| Final gate | Receipt `ef2a348363c447089cdcf3c3295cbc41`, exit 0; `answer()` returns 42 |

The client exited 0 after five real API calls in a 28.947-second run. Five native
tool calls had matching results and PreToolUse observations in the same session:
two gate calls, two reads and one write. Every observed operation was within the
fixture/receipt scope. The controller neither repaired source nor called the gate
during this session. Source and policy hashes matched the saved receipts; all 21
runtime files matched source, published wheel and installed package.

The trusted local broker used the new isolated login's OAuth access token
and device identifier for authentication. Neither was provided to the ephemeral
agent process; the original host Kimi profile remained untouched. The broker pinned
the endpoint and model, rejected unauthenticated local requests and redirects,
and checked outbound bodies after home/workspace/hostname redaction. The client
could reach its dedicated broker port; the socket control also confirmed denial
of an unrelated host service.
The native login profile is retained separately from disposable validation output.

All five live calls had hook observations, including the first gate. This differs
from the earlier scripted control's four observations; it establishes coverage
for this session without explaining the earlier gap or proving universal hook
reliability. The integration remains advisory MCP, with required CI providing
merge protection.

## Retained evidence

Local evidence is under `.exitzero/live-validation/claude-gemini/`:

- Client versions, scrubbed output streams and `claude-attempt-1.json` /
  `gemini-attempt-1.json`; neither attempt has native stop receipts.
- Setup lint receipts and installed legacy/migrated doctor receipts.
- `gemini-timeout-before.json`, `gemini-native-failure.json`,
  `gemini-native-repair.json` and copied gate receipts.

Temporary project, driver and installed-package paths are recorded locally for
reproduction and cleanup; personal paths and client output are not committed.

Antigravity evidence is under `.exitzero/live-validation/agy-0.5.1/`: `summary.json`,
`verified.json`, `headless-control.json`, four final receipts, native Stop/tool
records, the bounded interactive driver and the fixture-only scope guard.

Devin evidence is under `.exitzero/live-validation/devin-0.5.1/`: `summary.json`,
`verified.json`, two final receipts, Stop/tool records, the reviewed edit approval,
bounded drivers and the two incomplete headless attempts. Initial lint rejection
of standalone `hooks.v1.json` is retained separately; the documented nested
configuration passed lint.

Protected OpenCode evidence is under `.exitzero/live-validation/safe-clients/`:
`live-summary.json`, `live-verified.json`, two final `live-receipts/`, scrubbed
native tool events, broker request hashes/statuses, boundary checks and separate
offline/failed attempts. Broker and client state were temporary; provider keys
and raw personal paths are not committed.

Zcode and Kimi follow-up evidence is under `.exitzero/live-validation/safe-tail/`:
Zcode `live-summary.json`, `live-verified.json`, two live receipts, native hooks
and broker records; Kimi `kimi-offline/verified.json`, native streams, two component
receipts and its broker-port control. Failed setup attempts are separate from the
final results. The completed Kimi real-model evidence is in `safe-tail/kimi-live/`:
`live-summary.json`, `live-verified.json`, two receipts, five native tool/guard
records and broker request metadata. Login state is retained without committing
credentials or raw personal paths.
