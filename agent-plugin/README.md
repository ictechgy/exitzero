# exitzero agent plugin

Agent Plugins-style packaging for hosts that cannot install a blocking stop
hook (Windsurf-class IDEs, headless runners, MCP-only hosts).

## Contents

- `plugin.json` — plugin manifest (name, version, skill and MCP references)
- `.mcp.json` — MCP server entry: `exitzero plugin mcp-gate`
- `skills/exitzero-completion-gate/` — skill instructing the agent to call
  `check_completion` before declaring a task done

## Install

Prerequisite: `exitzero` on `PATH` (install the `exitzero` package) and an
`exitzero.toml` policy in the project whose `plugins` list includes
`exitzero_mcp_gate`.

Then register this directory with your host using its plugin mechanism, or
copy the pieces it reads:

- Hosts reading `.mcp.json`: merge the `mcpServers.exitzero` entry into the
  host's MCP configuration (e.g. `.mcp.json`, `mcp_config.json`).
- Hosts loading skills: copy `skills/exitzero-completion-gate/` into the
  host's skill directory.

The MCP server runs over stdio inside the project directory; it needs no
network access and writes receipts under `.exitzero/runs/` in the project.

## Trust boundary

This plugin is advisory, not enforced: the agent chooses whether to call
`check_completion`. Hosts with stop-hook support should prefer
`exitzero hooks install`, which gates the turn automatically. Use this
packaging where no such hook exists.
