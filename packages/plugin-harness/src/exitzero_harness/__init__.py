"""Configuration linting and the v1 harness-evaluation extension point.

The harness plugin deliberately only inspects files.  It does not execute hook,
MCP, or rule values.
"""

from __future__ import annotations

import json
import re
import shlex
import tomllib
from typing import Any

from exitzero.api import API_VERSION, Context, Finding
from exitzero.files import safe_path
from exitzero.hooks import cursor_hook_error, lint_installed
from exitzero.policy import BEGIN, END, render_agents


_HARNESS_FIELDS = frozenset({"config_files", "rules"})
_RULE_FIELDS = frozenset({"id", "value"})
_CLAUDE_ENTRY_FIELDS = frozenset({"matcher", "hooks", "disabled"})
_CLAUDE_ITEM_FIELDS = frozenset({"type", "command", "timeout"})
_CURSOR_EXECUTION_FIELDS = frozenset({"command", "prompt", "type"})
_LITERAL_COMMAND_PATH = re.compile(r"\./[A-Za-z0-9_./-]+\Z")


def _is_claude_entry(entry: Any) -> bool:
    """Detect Claude entries without stealing Cursor entries that carry extra fields.

    A Claude entry owns a nested ``hooks`` list.  A bare ``matcher`` without any
    execution field is also Claude-shaped so a malformed entry still reports its
    missing ``hooks`` list instead of a misleading Cursor complaint.
    """

    return isinstance(entry, dict) and (
        "hooks" in entry
        or ("matcher" in entry and not _CURSOR_EXECUTION_FIELDS & set(entry))
    )


def register(registry: Any) -> None:
    """Register the harness linter and the intentionally unimplemented eval command."""

    registry.add_linter("harness.config", lint_config)
    registry.add_command("harness-eval", harness_eval)


def harness_eval(context: Context, argv: list[str]) -> int:
    """Keep the future multi-turn evaluation command visible but unavailable in v1."""

    del context, argv
    return 2


def lint_config(context: Context) -> list[Finding]:
    """Lint generated policy documentation, hooks, JSON config, and harness rules."""

    findings: list[Finding] = []
    findings.extend(_lint_agents(context))
    findings.extend(_lint_harness_settings(context))
    findings.extend(_lint_installed_hooks(context))
    return findings


def _lint_agents(context: Context) -> list[Finding]:
    agents_path = safe_path(context.root, "AGENTS.md")
    if not agents_path.is_file():
        return [Finding("harness.agents", "AGENTS.md is missing the managed policy section", "AGENTS.md")]

    try:
        text = agents_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ValueError("Cannot read AGENTS.md") from exc

    begin_count = text.count(BEGIN)
    end_count = text.count(END)
    findings: list[Finding] = []
    if begin_count == 0 or end_count == 0:
        missing = []
        if begin_count == 0:
            missing.append("begin")
        if end_count == 0:
            missing.append("end")
        findings.append(
            Finding(
                "harness.agents.markers",
                f"AGENTS.md is missing the managed {', '.join(missing)} marker",
                "AGENTS.md",
            )
        )
    if begin_count > 1 or end_count > 1:
        findings.append(
            Finding(
                "harness.agents.markers",
                "AGENTS.md contains multiple managed section markers",
                "AGENTS.md",
            )
        )

    if begin_count != 1 or end_count != 1:
        return findings

    begin = text.find(BEGIN)
    end = text.find(END)
    if end < begin:
        findings.append(
            Finding(
                "harness.agents.markers",
                "AGENTS.md has broken managed markers: end precedes begin",
                "AGENTS.md",
            )
        )
        return findings

    actual = text[begin : end + len(END)]
    expected = render_agents(context.policy)
    if actual != expected:
        findings.append(
            Finding(
                "harness.agents.drift",
                "AGENTS.md managed section differs from the generated policy section",
                "AGENTS.md",
            )
        )
    return findings


def _lint_harness_settings(context: Context) -> list[Finding]:
    harness = context.policy.get("harness", {})
    if harness is None:
        return [Finding("harness.config", "policy harness must be a table")]
    if not isinstance(harness, dict):
        return [Finding("harness.config", "policy harness must be a table")]

    findings: list[Finding] = []
    unknown = sorted(set(harness) - _HARNESS_FIELDS)
    for field in unknown:
        findings.append(Finding("harness.config", f"unknown field in harness settings: {field}"))

    config_files = harness.get("config_files", [])
    if not isinstance(config_files, list) or any(not isinstance(item, str) for item in config_files):
        findings.append(Finding("harness.config", "harness.config_files must be a list of strings"))
        config_files = []
    for relative in config_files:
        suffix = relative.lower().rsplit(".", 1)[-1]
        if suffix == "json":
            findings.extend(_lint_json_config(context, relative))
        elif suffix == "toml":
            findings.extend(_lint_toml_config(context, relative))
        else:
            findings.append(Finding("harness.config", f"config file must be a .json or .toml filename: {relative}", relative))

    rules = harness.get("rules", [])
    if not isinstance(rules, list):
        findings.append(Finding("harness.config", "harness.rules must be a list of tables"))
    else:
        findings.extend(_lint_rules(rules))
    return findings


def _lint_rules(rules: list[Any]) -> list[Finding]:
    findings: list[Finding] = []
    seen: dict[str, list[Any]] = {}
    for index, rule in enumerate(rules):
        if not isinstance(rule, dict):
            findings.append(Finding("harness.rules", f"harness.rules[{index}] must be a table"))
            continue
        unknown = sorted(set(rule) - _RULE_FIELDS)
        for field in unknown:
            findings.append(
                Finding("harness.rules", f"unknown field in harness.rules[{index}]: {field}")
            )
        if "id" not in rule or not isinstance(rule["id"], str) or not rule["id"].strip():
            findings.append(Finding("harness.rules", f"harness.rules[{index}].id must be a non-empty string"))
            continue
        if "value" not in rule:
            findings.append(Finding("harness.rules", f"harness.rules[{index}] is missing value"))
            continue
        if not isinstance(rule["value"], str):
            findings.append(Finding("harness.rules", f"harness.rules[{index}].value must be a string"))
            continue
        rule_id = rule["id"]
        if rule_id in seen:
            if rule["value"] not in seen[rule_id]:
                findings.append(
                    Finding("harness.rules", f"harness rule {rule_id!r} has conflicting values")
                )
            else:
                findings.append(Finding("harness.rules", f"harness rule {rule_id!r} is duplicated"))
            seen[rule_id].append(rule["value"])
        else:
            seen[rule_id] = [rule["value"]]
    return findings


def _lint_json_config(context: Context, relative: str) -> list[Finding]:
    try:
        path = safe_path(context.root, relative)
    except (OSError, ValueError, TypeError):
        return [Finding("harness.config", f"config file is not allowed: {relative}", relative)]
    if not path.is_file():
        return [Finding("harness.config", f"config file does not exist: {relative}", relative)]
    try:
        raw = path.read_text(encoding="utf-8")
        document = json.loads(raw)
    except (OSError, UnicodeError) as exc:
        raise ValueError("Cannot read harness config") from exc
    except json.JSONDecodeError as exc:
        return [Finding("harness.config", f"config file has invalid JSON: {relative}", relative, exc.lineno)]
    return _validate_config_shape(context, document, relative)


def _validate_config_shape(context: Context, document: Any, relative: str) -> list[Finding]:
    if not isinstance(document, dict):
        return [Finding("harness.config", f"config file must be an object: {relative}", relative)]
    findings: list[Finding] = []
    recognized = False
    if "hooks" in document:
        recognized = True
        findings.extend(_lint_hooks_document(context, document, relative))
    if "mcpServers" in document:
        recognized = True
        findings.extend(_validate_mcp_servers(document["mcpServers"], relative))
    if not recognized:
        findings.append(Finding("harness.config", f"config file must contain hooks or MCP servers: {relative}", relative))
    return findings


def _lint_hooks_document(context: Context, document: dict, relative: str) -> list[Finding]:
    """Lint Cursor and Claude hook documents without executing any values.

    Cursor hook entries carry ``command``/``prompt`` fields; Claude entries carry
    ``matcher`` plus a nested ``hooks`` list.  The entry shape selects the format,
    so one linter covers ``.cursor/hooks.json`` and ``.claude/settings.json``.
    """

    findings: list[Finding] = []
    hooks = document["hooks"]
    if not isinstance(hooks, dict):
        return [Finding("harness.config", f"hooks must be an object: {relative}", relative)]
    cursor_entries = False
    for slot, entries in hooks.items():
        if not isinstance(slot, str) or not isinstance(entries, list):
            findings.append(Finding("harness.config", f"hook entries must be lists: {relative}", relative))
            continue
        if not entries:
            findings.append(Finding("harness.config", f"hook slot {slot!r} has no entries: {relative}", relative))
            continue
        for entry in entries:
            if _is_claude_entry(entry):
                findings.extend(_lint_claude_hook_entry(context, entry, relative))
            else:
                cursor_entries = True
                error = cursor_hook_error(entry)
                if error:
                    findings.append(Finding("harness.config", error, relative))
                elif isinstance(entry.get("command"), str):
                    findings.extend(_command_path_findings(context, entry["command"], relative))
    version = document.get("version")
    if "version" in document and (type(version) is not int or version != 1):
        findings.append(Finding("harness.config", f"Cursor hooks version must be 1: {relative}", relative))
    elif cursor_entries and "version" not in document:
        findings.append(Finding("harness.config", f"Cursor hook entries require version 1: {relative}", relative))
    return findings


def _lint_claude_hook_entry(context: Context, entry: dict, relative: str) -> list[Finding]:
    findings: list[Finding] = []
    for field in sorted(set(entry) - _CLAUDE_ENTRY_FIELDS):
        findings.append(Finding("harness.config", f"unknown field in Claude hook entry: {field}", relative))
    if "matcher" in entry and not isinstance(entry["matcher"], str):
        findings.append(Finding("harness.config", f"Claude hook matcher must be a string: {relative}", relative))
    items = entry.get("hooks")
    if not isinstance(items, list) or not items:
        findings.append(Finding("harness.config", f"Claude hook entries require a non-empty hooks list: {relative}", relative))
        return findings
    for item in items:
        if not isinstance(item, dict):
            findings.append(Finding("harness.config", f"Claude hook items must be objects: {relative}", relative))
            continue
        for field in sorted(set(item) - _CLAUDE_ITEM_FIELDS):
            findings.append(Finding("harness.config", f"unknown field in Claude hook item: {field}", relative))
        if item.get("type", "command") != "command":
            findings.append(Finding("harness.config", f"Claude hook item type must be command: {relative}", relative))
        if not isinstance(item.get("command"), str) or not item["command"].strip():
            findings.append(Finding("harness.config", f"Claude hook items require a non-empty command: {relative}", relative))
        else:
            findings.extend(_command_path_findings(context, item["command"], relative))
    return findings


def _command_path_findings(context: Context, command: str, relative: str) -> list[Finding]:
    """Check that an explicitly repo-relative hook command points at a real file."""

    try:
        tokens = shlex.split(command)
    except ValueError:
        return []
    # Only a plainly literal "./path" token is checked; shell operators,
    # expansions or punctuation make the token something else entirely.
    if not tokens or not _LITERAL_COMMAND_PATH.fullmatch(tokens[0]):
        return []
    try:
        target = safe_path(context.root, tokens[0][2:])
    except (OSError, ValueError, TypeError):
        return [Finding("harness.config", f"hook command path is not allowed: {tokens[0]}", relative)]
    if not target.is_file():
        return [Finding("harness.config", f"hook command path does not exist: {tokens[0]}", relative)]
    return []


def _validate_mcp_servers(servers: Any, relative: str) -> list[Finding]:
    if not isinstance(servers, dict):
        return [Finding("harness.config", f"MCP servers must be an object: {relative}", relative)]
    findings: list[Finding] = []
    for name, server in servers.items():
        if not isinstance(name, str) or not name.strip() or not isinstance(server, dict):
            findings.append(Finding("harness.config", f"MCP server entries must be objects: {relative}", relative))
            continue
        has_command = isinstance(server.get("command"), str) and bool(server["command"].strip())
        has_url = isinstance(server.get("url"), str) and bool(server["url"].strip())
        if has_command == has_url:
            findings.append(Finding("harness.config", f"MCP server requires exactly one non-empty command or url: {relative}", relative))
        for key in ("command", "url"):
            if key in server and (not isinstance(server[key], str) or not server[key].strip()):
                findings.append(Finding("harness.config", f"MCP {key} must be a non-empty string: {relative}", relative))
        if "args" in server and (
            not isinstance(server["args"], list)
            or any(not isinstance(arg, str) for arg in server["args"])
        ):
            findings.append(Finding("harness.config", f"MCP args must be a list of strings: {relative}", relative))
        if "env" in server and (not isinstance(server["env"], dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in server["env"].items()
        )):
            findings.append(Finding("harness.config", f"MCP env must be a string map: {relative}", relative))
    return findings


def _lint_toml_config(context: Context, relative: str) -> list[Finding]:
    try:
        path = safe_path(context.root, relative)
    except (OSError, ValueError, TypeError):
        return [Finding("harness.config", f"config file is not allowed: {relative}", relative)]
    if not path.is_file():
        return [Finding("harness.config", f"config file does not exist: {relative}", relative)]
    try:
        document = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError) as exc:
        raise ValueError("Cannot read harness config") from exc
    except tomllib.TOMLDecodeError:
        return [Finding("harness.config", f"config file has invalid TOML: {relative}", relative)]
    findings: list[Finding] = []
    if "mcp_servers" in document:
        findings.extend(_validate_mcp_servers(document["mcp_servers"], relative))
    else:
        findings.append(Finding("harness.config", f"TOML config file must contain mcp_servers: {relative}", relative))
    return findings


def _lint_installed_hooks(context: Context) -> list[Finding]:
    return list(lint_installed(context) or [])


__all__ = ["API_VERSION", "harness_eval", "lint_config", "register"]
