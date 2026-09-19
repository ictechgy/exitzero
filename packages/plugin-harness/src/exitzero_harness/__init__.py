"""Configuration linting and bounded multi-turn scenario evaluation.

The config linter deliberately only inspects files; it never executes hook,
MCP, or rule values.  ``harness-eval`` replays scripted file turns against the
real gate inside a temporary copy — the repository it was invoked on is never
mutated, and trusted command checks run under their normal timeouts.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re
import shlex
import shutil
import tempfile
import tomllib
from typing import Any
import uuid

from exitzero.api import Context, Finding
from exitzero.services import (BEGIN, END, cursor_hook_error, lint_installed,
                               render_agents, run_gate, safe_path)

API_VERSION = 1


_HARNESS_FIELDS = frozenset({"config_files", "rules"})
_RULE_FIELDS = frozenset({"id", "value"})
_CLAUDE_ENTRY_FIELDS = frozenset({"matcher", "hooks", "disabled"})
_CLAUDE_ITEM_FIELDS = frozenset({"type", "command", "timeout"})
_CURSOR_EXECUTION_FIELDS = frozenset({"command", "prompt", "type"})
_LITERAL_COMMAND_PATH = re.compile(r"\./[A-Za-z0-9_./-]+\Z")

_EVAL_SCHEMA_VERSION = 1
_EVAL_MAX_TURNS = 64
_EVAL_SCENARIO_FIELDS = frozenset({"schema_version", "description", "max_turns", "skip"})
_EVAL_TURN_FIELDS = frozenset({"note", "expect", "delete"})
_EVAL_EXPECT_FIELDS = frozenset({"exit", "rules"})
_EVAL_IGNORED = frozenset({".git", ".exitzero", ".serena", "__pycache__", ".venv", "venv",
                          "node_modules", "build", "dist"})


class _EvalOperationalError(Exception):
    """An operational eval failure that must exit 2 rather than score a mismatch."""


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
    """Register the harness linter and the bounded scenario evaluator."""

    registry.add_linter("harness.config", lint_config)
    registry.add_command("harness-eval", harness_eval)


def harness_eval(context: Context, argv: list[str]) -> int:
    """Replay scripted scenario turns against the real gate in a temporary copy.

    ``exitzero plugin harness-eval --scenario PATH`` is strictly opt-in: PATH is
    a scenario directory (or a parent containing scenario directories), each
    with a ``scenario.toml`` manifest and ``turns/*/`` directories whose files
    overlay the repository copy before that turn's ``check`` run.  A scenario
    may ship its own ``base/`` mini-repository; otherwise the invocation root
    is copied minus generated directories.  Every turn expects ``exit`` and
    ``rules``; mismatches fail the scenario while ``skip`` reports it without
    running.  Results persist under ``.exitzero/evals/``.
    """

    try:
        container = _eval_container(context, argv)
    except ValueError as error:
        print(f"harness-eval: {error}")
        return 2
    scenario_dirs = _discover_scenarios(container)
    if not scenario_dirs:
        print(f"harness-eval: no scenario directories under {container}")
        return 2
    started_at = datetime.now(timezone.utc)
    results = [_run_scenario(context, container, directory) for directory in scenario_dirs]
    for result in results:
        line = f"{result['name']}: {result['status']}"
        if result["status"] == "SKIP":
            line += f" ({result['reason']})"
        print(line)
        for error in result.get("errors", []):
            print(f"  {error}")
    report = {
        "schema_version": _EVAL_SCHEMA_VERSION,
        "tool": "exitzero harness-eval",
        "run_id": uuid.uuid4().hex,
        "started_at": started_at.isoformat(),
        "container": str(container),
        "passed": sum(result["status"] == "PASS" for result in results),
        "failed": sum(result["status"] == "FAIL" for result in results),
        "skipped": sum(result["status"] == "SKIP" for result in results),
        "errors": sum(result["status"] == "ERROR" for result in results),
        "skipped_scenarios": [
            {"name": result["name"], "reason": result["reason"]}
            for result in results
            if result["status"] == "SKIP"
        ],
        "scenarios": results,
    }
    report["status"] = "error" if report["errors"] else "failed" if report["failed"] else "passed"
    try:
        destination = safe_path(context.root, f".exitzero/evals/harness-eval-{report['run_id']}.json")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                               encoding="utf-8")
    except (OSError, ValueError):
        print("harness-eval: cannot persist the eval report")
        return 2
    print(f"Report: {destination.relative_to(context.root)}")
    if report["errors"]:
        return 2
    return 1 if report["failed"] else 0


def _eval_container(context: Context, argv: list[str]) -> Path:
    scenario: str | None = None
    index = 0
    while index < len(argv):
        argument = argv[index]
        if argument == "--scenario":
            if index + 1 >= len(argv):
                raise ValueError("--scenario requires a path")
            scenario = argv[index + 1]
            index += 2
        elif argument.startswith("--scenario="):
            scenario = argument.split("=", 1)[1]
            index += 1
        else:
            raise ValueError(f"unknown argument: {argument}")
    if scenario is None or not scenario.strip():
        raise ValueError("usage: harness-eval --scenario PATH")
    candidate = Path(scenario)
    if not candidate.is_absolute():
        candidate = context.root / candidate
    container = candidate.resolve()
    if not container.is_dir():
        raise ValueError(f"scenario path is not a directory: {scenario}")
    return container


def _discover_scenarios(container: Path) -> list[Path]:
    """List scenario candidates; a missing manifest must fail, never vanish."""

    if (container / "scenario.toml").is_file():
        return [container]
    return sorted(
        child for child in container.iterdir()
        if child.is_dir() and not child.name.startswith(".")
    )


def _load_scenario(directory: Path) -> dict[str, Any]:
    manifest_path = directory / "scenario.toml"
    if not manifest_path.is_file():
        raise ValueError(f"{directory.name} is missing scenario.toml")
    try:
        manifest = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as error:
        raise ValueError(f"{directory.name} scenario.toml cannot be parsed: {error}") from error
    if not isinstance(manifest, dict) or set(manifest) - _EVAL_SCENARIO_FIELDS:
        raise ValueError("scenario.toml has unknown fields")
    version = manifest.get("schema_version")
    if type(version) is not int or version != _EVAL_SCHEMA_VERSION:
        raise ValueError(f"scenario.toml schema_version must be {_EVAL_SCHEMA_VERSION}")
    for key in ("description", "skip"):
        if key in manifest and not isinstance(manifest[key], str):
            raise ValueError(f"scenario.toml {key} must be a string")
    if "skip" in manifest and not manifest["skip"].strip():
        raise ValueError("scenario.toml skip must explain the reason")
    max_turns = manifest.get("max_turns")
    if max_turns is not None and (type(max_turns) is not int or max_turns < 1):
        raise ValueError("scenario.toml max_turns must be a positive integer")
    return manifest


def _load_turn(directory: Path) -> dict[str, Any]:
    manifest_path = directory / "turn.toml"
    if not manifest_path.is_file():
        raise ValueError(f"{directory.name} is missing turn.toml")
    try:
        manifest = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as error:
        raise ValueError(f"{directory.name} turn.toml cannot be parsed: {error}") from error
    if not isinstance(manifest, dict) or set(manifest) - _EVAL_TURN_FIELDS:
        raise ValueError(f"{directory.name} turn.toml has unknown fields")
    if "note" in manifest and not isinstance(manifest["note"], str):
        raise ValueError(f"{directory.name} turn.toml note must be a string")
    delete = manifest.get("delete", [])
    if not isinstance(delete, list) or any(not isinstance(item, str) or not item for item in delete):
        raise ValueError(f"{directory.name} turn.toml delete must be a list of paths")
    expect = manifest.get("expect")
    if not isinstance(expect, dict) or set(expect) - _EVAL_EXPECT_FIELDS:
        raise ValueError(f"{directory.name} turn.toml expect must be a table of exit/rules")
    exit_code = expect.get("exit")
    rules = expect.get("rules")
    if type(exit_code) is not int or exit_code not in (0, 1, 2):
        raise ValueError(f"{directory.name} expect.exit must be 0, 1 or 2")
    if not isinstance(rules, list) or any(not isinstance(rule, str) for rule in rules):
        raise ValueError(f"{directory.name} expect.rules must be a list of strings")
    return {"name": directory.name, "exit": exit_code, "rules": list(rules),
            "delete": list(delete), "note": manifest.get("note")}


def _copy_tree(source: Path, destination: Path, extra_ignored: frozenset[Path]) -> None:
    def ignore(directory: str, names: list[str]) -> set[str]:
        return {name for name in names
                if name in _EVAL_IGNORED or Path(directory, name) in extra_ignored}

    # symlinks=True keeps links as links so the gate sees the same filesystem
    # shape it would see in a direct check instead of dereferenced copies.
    shutil.copytree(source, destination, dirs_exist_ok=True, ignore=ignore, symlinks=True)


def _apply_turn(turn_directory: Path, target_root: Path) -> None:
    root_resolved = target_root.resolve()
    for source in sorted(turn_directory.rglob("*")):
        relative = source.relative_to(turn_directory)
        if relative.parts == ("turn.toml",):
            continue
        if source.is_symlink():
            raise ValueError(f"turn overlay may not contain symlinks: {relative.as_posix()}")
        destination = target_root / relative
        # Every write must resolve inside the temp copy; a symlink planted by an
        # earlier turn (for example by a command check) must never redirect an
        # overlay to a file outside it.
        if destination.is_symlink() or not destination.resolve().is_relative_to(root_resolved):
            raise ValueError(f"turn overlay escapes the eval copy: {relative.as_posix()}")
        if source.is_dir():
            destination.mkdir(exist_ok=True)
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)


def _apply_deletes(turn: dict[str, Any], target_root: Path) -> None:
    root_resolved = target_root.resolve()
    for relative in turn["delete"]:
        try:
            target = safe_path(target_root, relative)
        except (OSError, ValueError, TypeError) as error:
            raise ValueError(f"turn {turn['name']} delete path is not allowed: {relative}") from error
        if not target.resolve().is_relative_to(root_resolved):
            raise ValueError(f"turn {turn['name']} delete path escapes the eval copy: {relative}")
        if not target.is_file():
            raise ValueError(f"turn {turn['name']} delete path does not exist: {relative}")
        target.unlink()


def _check_statuses(receipt: dict[str, Any]) -> dict[str, str]:
    """Extract check id -> status from one gate receipt for transition scoring."""
    return {entry["id"]: entry["status"]
            for entry in receipt.get("checks", [])
            if isinstance(entry, dict) and isinstance(entry.get("id"), str)
            and isinstance(entry.get("status"), str)}


def _transitions(previous: dict[str, str], current: dict[str, str]) -> dict[str, list[str]]:
    """Classify check outcomes across two turns as SWE-bench-style evidence.

    fail_to_pass marks checks a turn repaired, pass_to_pass marks checks kept
    green, and the regression direction is recorded too so a turn that breaks
    previously green checks is visible rather than silently absent.
    """
    def passed(status: str | None) -> bool | None:
        if status is None:
            return None
        return status in ("passed", "reused")

    result: dict[str, list[str]] = {"fail_to_pass": [], "pass_to_pass": [],
                                   "pass_to_fail": [], "fail_to_fail": []}
    for check_id in sorted(current):
        before, after = passed(previous.get(check_id)), passed(current[check_id])
        if before is None:
            continue
        key = f"{'pass' if before else 'fail'}_to_{'pass' if after else 'fail'}"
        result[key].append(check_id)
    return {key: ids for key, ids in result.items() if ids}


def _run_scenario(context: Context, container: Path, directory: Path) -> dict[str, Any]:
    """Score one scenario; operational failures are ERROR, never silent passes."""

    try:
        return _execute_scenario(context, container, directory)
    except _EvalOperationalError as error:
        return {"name": directory.name, "status": "ERROR", "errors": [str(error)]}
    except (OSError, ValueError) as error:
        return {"name": directory.name, "status": "ERROR",
                "errors": [f"{type(error).__name__}: {error}"]}


def _execute_scenario(context: Context, container: Path, directory: Path) -> dict[str, Any]:
    result: dict[str, Any] = {"name": directory.name}
    try:
        manifest = _load_scenario(directory)
    except ValueError as error:
        return {**result, "status": "FAIL", "errors": [str(error)]}
    if "description" in manifest:
        result["description"] = manifest["description"]
    if "skip" in manifest:
        return {**result, "status": "SKIP", "reason": manifest["skip"]}
    turns_dir = directory / "turns"
    turn_dirs = sorted(child for child in turns_dir.iterdir() if child.is_dir()) if turns_dir.is_dir() else []
    if not turn_dirs:
        return {**result, "status": "FAIL", "errors": ["scenario has no turn directories"]}
    bound = min(manifest.get("max_turns") or _EVAL_MAX_TURNS, _EVAL_MAX_TURNS)
    if len(turn_dirs) > bound:
        return {**result, "status": "FAIL",
                "errors": [f"{len(turn_dirs)} turns exceed the bound of {bound}"]}
    turns: list[dict[str, Any]] = []
    try:
        for turn_dir in turn_dirs:
            turns.append(_load_turn(turn_dir))
    except ValueError as error:
        return {**result, "status": "FAIL", "errors": [str(error)]}
    base = directory / "base"
    if base.is_dir():
        source, policy_name = base, "exitzero.toml"
        if not (base / "exitzero.toml").is_file():
            return {**result, "status": "FAIL", "errors": ["base/ requires its own exitzero.toml"]}
        ignored = frozenset()
    else:
        source = context.root
        policy_name = context.policy_path.relative_to(context.root).as_posix()
        ignored = frozenset({container} if container.is_relative_to(context.root) else set())
    turn_results: list[dict[str, Any]] = []
    previous_statuses: dict[str, str] | None = None
    with tempfile.TemporaryDirectory(prefix=f"exitzero-eval-{directory.name}-") as temporary:
        temporary_root = Path(temporary)
        try:
            _copy_tree(source, temporary_root, ignored)
            for turn in turns:
                _apply_deletes(turn, temporary_root)
                _apply_turn(Path(turns_dir) / turn["name"], temporary_root)
                receipt = run_gate(temporary_root, policy_name, "check")
                if receipt.get("receipt") is None:
                    raise _EvalOperationalError(
                        f"turn {turn['name']} could not persist its gate receipt")
                rules = sorted(
                    finding.get("rule") for finding in receipt.get("findings", [])
                    if isinstance(finding, dict)
                )
                expected = sorted(turn["rules"])
                matched = receipt["exit_code"] == turn["exit"] and rules == expected
                entry: dict[str, Any] = {
                    "name": turn["name"],
                    "matched": matched,
                    "exit_code": receipt["exit_code"],
                    "expected_exit": turn["exit"],
                    "rules": rules,
                    "expected_rules": expected,
                    # Drop the per-file input hash map — it dominates memory on
                    # large trees; findings, ids and timing remain as evidence.
                    "receipt": {key: value for key, value in receipt.items() if key != "inputs"},
                }
                statuses = _check_statuses(receipt)
                if previous_statuses is not None:
                    entry["transitions"] = _transitions(previous_statuses, statuses)
                previous_statuses = statuses
                if "note" in turn:
                    entry["note"] = turn["note"]
                turn_results.append(entry)
        except (OSError, ValueError) as error:
            return {**result, "status": "FAIL",
                    "errors": [f"{type(error).__name__}: {error}"], "turns": turn_results}
    result["turns"] = turn_results
    mismatches = [turn["name"] for turn in turn_results if not turn["matched"]]
    if mismatches:
        result["status"] = "FAIL"
        result["errors"] = [f"turn {name} did not match its expectation" for name in mismatches]
    else:
        result["status"] = "PASS"
    return result


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
    seen: dict[str, set[Any]] = {}
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
        values = seen.setdefault(rule_id, set())
        if values:
            if rule["value"] in values:
                findings.append(Finding("harness.rules", f"harness rule {rule_id!r} is duplicated"))
            else:
                findings.append(
                    Finding("harness.rules", f"harness rule {rule_id!r} has conflicting values")
                )
        values.add(rule["value"])
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
