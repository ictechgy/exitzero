# Optional AgentWarden integration

ExitZero can run an already installed, operator-pinned AgentWarden 0.3.2 and
record its result in the same gate receipt as project tests. Its scanner rules
and skill lock format stay owned by AgentWarden. ExitZero adds bounded local
input discovery, version/JSON checks and setup diagnostics; it does not install
AgentWarden, execute skills, start MCP servers or refresh locks.

Enable the bundled verify plugin, then add explicit checks:

```toml
[[checks]]
id = "skill-integrity"
kind = "agentwarden.audit"
reuse = false
[checks.options]
argv = ["agentwarden"]
config = ".agentwarden/policy.json"
version = "0.3.2"
timeout = 60

[[checks]]
id = "mcp-config"
kind = "agentwarden.scan"
paths = [".mcp.json"]
reuse = false
[checks.options]
argv = ["agentwarden"]
config = ".agentwarden/policy.json"
version = "0.3.2"
timeout = 60
```

`audit` requires a nonempty root `skills.lock` and root-relative installed skill
sources. Missing/empty locks are not passing evidence, even when the upstream
CLI would report an empty successful audit. `scan` requires a nonempty explicit
file selection. Both require an explicit JSON configuration file; configuration
inheritance through `extends` is unsupported. A configured finding baseline must
also be a guarded local file and is fingerprinted. Protect these inputs with
[permission zones](PERMISSION_ZONES.md) when the candidate must not weaken them.

```sh
exitzero init --sync
exitzero doctor --format json
exitzero check --format json
```

Doctor inspects setup without invoking the external program. It reports
`check.skill-integrity` / `check.mcp-config` as configured, missing or
misconfigured; runtime remains unverified. A normal check first verifies the
program's version, then runs the local audit/scan with a timeout and capped
stdout/stderr. Upstream findings and source content are not copied into ExitZero
receipts. A normal risk is a verification violation; missing tools, unknown
versions, invalid JSON and execution failures are operational errors.

## Trust and compatibility

The executable is trusted local code, like any configured `command` check.
Version text is a compatibility check, not proof of executable authenticity.
Pin the package/artifact and its dependencies in your own environment or CI;
ExitZero does not certify that installation. An alternative argv form is
`["node", "tools/agentwarden/src/cli.ts"]` for an operator-reviewed local source
copy. Include its full runtime tree in `checks.options.tool_paths`, for example
`tool_paths = ["tools/agentwarden/**/*"]`, and protect it from candidate edits.
These are fingerprint inputs, separate from `scan` targets. The entry script
itself is also fingerprinted. Package managers,
download commands and shells are not accepted as implicit launcher prefixes.

Input paths must stay inside the project and avoid symlinks, credential-like
files and generated directories excluded by ExitZero. Skill packages are checked
against their local manifests, including extra-file membership. Unsupported
layouts fail rather than silently narrowing the inspected input set. `reuse`
must be false because the external executable and environment can change.
Execution currently requires POSIX pipe controls (macOS/Linux); other platforms
report the integration as unavailable instead of using unbounded pipe capture.

This integration does not give the scanner an OS sandbox by itself. Run it in
your existing restricted CI or process environment. AgentWarden is a static
security/integrity gate; neither a clean scan nor a lock hash proves safe runtime
behavior. See the [upstream repository](https://github.com/juangh123/AgentWarden)
and [its stated limits](https://dev.to/agentwarden/agent-skills-and-mcp-configs-need-a-security-gate-cdf).

Compatibility was checked against the source of upstream tag `v0.3.2`, commit
`9ece59584adb0f3cdb30f805659535475f1da1a0`, using synthetic inputs in an isolated
home with network destinations disabled. That is source-CLI evidence, not a
claim about an installed npm package or the user's existing skill inventory.
