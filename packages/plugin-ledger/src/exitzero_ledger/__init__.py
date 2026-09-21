"""Reviewable run ledger: aggregate receipts, propose rollback hints.

``exitzero plugin ledger-publish`` rolls the ``.exitzero/runs/`` receipts into
one run record under ``.exitzero/ledger/`` (JSON plus a Markdown body fit for
a PR comment), ranks the paths implicated by non-passing runs, and — when a
``--base`` ref is given — lists recent commits touching those paths.  The
``--pr N`` flag is the only write beyond the repository: it shells out to
``gh pr comment`` verbatim, so publishing stays an explicit caller decision.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Any
import uuid

from exitzero.api import Context
from exitzero.services import safe_path, validate_relative, write_atomic


API_VERSION = 1

_LEDGER_SCHEMA_VERSION = 1
_HINT_COMMITS = 20
_HINT_PATH_BATCH = 256
_HINT_ARGV_BUDGET = 128 * 1024
# Receipt-derived text flows into Markdown and process argv; control
# characters and NULs are never valid in a repository path or commit subject.
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
# A ref name never starts with "-" and never contains spaces or shell-ish
# characters; anything else would land in argv as an option.
_REF_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._~^/:-]*\Z")


def register(registry: Any) -> None:
    """Register the run-record ledger command."""

    registry.add_command("ledger-publish", ledger_publish)


def ledger_publish(context: Context, argv: list[str]) -> int:
    """Aggregate run receipts into a publishable record with rollback hints.

    ``--since ISO8601`` filters receipts by ``started_at``; ``--base REF``
    scopes the git history searched for suspect commits; ``--pr N`` posts the
    Markdown body via ``gh pr comment`` and is the only external write.
    """

    try:
        options = _parse_args(argv)
        _validate_base(context.root, options["base"])
    except ValueError as error:
        print(f"ledger-publish: {error}", file=sys.stderr)
        return 2
    receipts, corrupt = _load_receipts(context.root, options["since"])
    record = _build_record(context, receipts, corrupt, options)
    try:
        json_path, markdown_path = _persist_record(context.root, record)
    except (OSError, ValueError) as error:
        print(f"ledger-publish: cannot persist the record: {error}", file=sys.stderr)
        return 2
    print(f"Record: {json_path.relative_to(context.root)}")
    print(f"Body:   {markdown_path.relative_to(context.root)}")
    if options["pr"] is not None:
        error = _publish_to_pr(options["pr"], _render_markdown(record), context.root)
        if error is not None:
            print(f"ledger-publish: {error}", file=sys.stderr)
            return 2
        print(f"Published run record to PR #{options['pr']}.")
    return 0


def _parse_args(argv: list[str]) -> dict[str, Any]:
    options: dict[str, Any] = {"since": None, "base": None, "pr": None}
    index = 0
    while index < len(argv):
        argument = argv[index]
        for flag in ("--since", "--base", "--pr"):
            if argument == flag or argument.startswith(flag + "="):
                if argument == flag:
                    if index + 1 >= len(argv):
                        raise ValueError(f"{flag} requires a value")
                    value = argv[index + 1]
                    index += 2
                else:
                    value = argument.split("=", 1)[1]
                    index += 1
                if not value.strip():
                    raise ValueError(f"{flag} requires a non-empty value")
                if flag == "--since":
                    options["since"] = _parse_since(value)
                elif flag == "--pr":
                    if not value.isdigit() or int(value) < 1:
                        raise ValueError("--pr requires a positive PR number")
                    options["pr"] = int(value)
                else:
                    options["base"] = value
                break
        else:
            raise ValueError(f"unknown argument: {argument}")
    return options


def _parse_since(value: str) -> datetime:
    try:
        moment = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError("--since must be an ISO-8601 timestamp") from error
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


def _validate_base(root: Path, base: str | None) -> None:
    """An explicit --base must be a resolvable commit-ish, never an option."""

    if base is None:
        return
    if not _REF_NAME.fullmatch(base):
        raise ValueError(f"--base is not a valid git ref: {base}")
    if shutil.which("git") is None:
        raise ValueError("--base requires git on PATH")
    env = {**os.environ, "GIT_NO_LAZY_FETCH": "1"}
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}"],
        cwd=root, capture_output=True, text=True, timeout=15, env=env)
    if result.returncode != 0 or not result.stdout.strip():
        raise ValueError(f"--base does not resolve to a commit: {base}")


def _load_receipts(root: Path, since: datetime | None) -> tuple[list[dict[str, Any]], list[str]]:
    """Read run receipts without ever following a symlink outside the repo."""

    receipts: list[dict[str, Any]] = []
    corrupt: list[str] = []
    try:
        directory = safe_path(root, ".exitzero/runs")
    except (OSError, ValueError):
        return receipts, [".exitzero/runs is not a readable directory"]
    if not directory.is_dir():
        return receipts, corrupt
    for path in sorted(directory.glob("*.json")):
        try:
            checked = safe_path(root, f".exitzero/runs/{path.name}")
            if not checked.is_file():
                raise ValueError("not a regular file")
            receipt = json.loads(checked.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            corrupt.append(path.name)
            continue
        if not _is_receipt(receipt):
            corrupt.append(path.name)
            continue
        started = receipt.get("started_at")
        if since is not None:
            try:
                moment = datetime.fromisoformat(str(started))
                if moment.tzinfo is None:
                    moment = moment.replace(tzinfo=timezone.utc)
            except ValueError:
                corrupt.append(path.name)
                continue
            if moment < since:
                continue
        # Keep only the fields aggregation needs; the per-file ``inputs`` hash
        # map dominates receipt size and must not accumulate in memory.
        receipts.append({key: receipt.get(key)
                         for key in ("command", "status", "receipt", "findings", "permissions")})
    return receipts, corrupt


def _is_receipt(document: Any) -> bool:
    """Minimal structural check: corrupt files are reported, never fatal."""

    return (
        isinstance(document, dict)
        and isinstance(document.get("run_id"), str)
        and isinstance(document.get("command"), str)
        and isinstance(document.get("status"), str)
        and isinstance(document.get("findings", []), list)
    )


def _build_record(context: Context, receipts: list[dict[str, Any]],
                  corrupt: list[str], options: dict[str, Any]) -> dict[str, Any]:
    by_command: dict[str, int] = {}
    by_status: dict[str, int] = {}
    rules: dict[str, int] = {}
    implicated: dict[str, dict[str, Any]] = {}
    permissions = []
    for receipt in receipts:
        permission = receipt.get("permissions")
        if isinstance(permission, dict) and isinstance(permission.get("changes"), list):
            changes = []
            for change in permission["changes"]:
                if (isinstance(change, dict) and _implicated_path(change.get("path"))
                        and isinstance(change.get("zone"), str) and isinstance(change.get("decision"), str)
                        and change.get("zone") in {"editable", "protected", "immutable", "unclassified"}
                        and change.get("decision") in {"allowed", "review_required", "denied"}):
                    changes.append({key: change[key] for key in ("path", "zone", "decision")})
            commit = permission.get("base_commit")
            if isinstance(commit, str) and re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", commit):
                permissions.append({"receipt": receipt.get("receipt"), "base_commit": commit,
                                    "status": permission.get("status") if isinstance(permission.get("status"), str) and permission.get("status") in
                                    {"passed", "failed", "unverified"} else "unverified", "changes": changes})
        command = str(receipt.get("command", "?"))
        status = str(receipt.get("status", "?"))
        by_command[command] = by_command.get(command, 0) + 1
        by_status[status] = by_status.get(status, 0) + 1
        for finding in receipt.get("findings", []):
            if not isinstance(finding, dict):
                continue
            rule = str(finding.get("rule", "?"))
            rules[rule] = rules.get(rule, 0) + 1
            if status == "passed":
                continue
            path = finding.get("path")
            if _implicated_path(path):
                entry = implicated.setdefault(path, {"count": 0, "rules": set()})
                entry["count"] += 1
                entry["rules"].add(rule)
    hints = {
        "implicated_paths": [
            {"path": path, "finding_count": entry["count"],
             "rules": sorted(entry["rules"])}
            for path, entry in sorted(implicated.items(),
                                      key=lambda item: (-item[1]["count"], item[0]))
        ],
    }
    suspect_commits = _suspect_commits(context.root, options["base"],
                                     [entry["path"] for entry in hints["implicated_paths"]])
    if suspect_commits is not None:
        hints["suspect_commits"] = suspect_commits
    else:
        hints["suspect_commits_note"] = "git history unavailable"
    return {
        "schema_version": _LEDGER_SCHEMA_VERSION,
        "tool": "exitzero ledger-publish",
        "run_id": uuid.uuid4().hex,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "filters": {"since": options["since"].isoformat() if options["since"] else None,
                    "base": options["base"]},
        "receipts": len(receipts),
        "corrupt_receipts": corrupt,
        "by_command": by_command,
        "by_status": by_status,
        "findings_by_rule": rules,
        "permission_runs": permissions,
        "rollback_hints": hints,
        "run_refs": [receipt.get("receipt") for receipt in receipts
                     if isinstance(receipt.get("receipt"), str)],
    }


def _implicated_path(value: Any) -> bool:
    """A finding path must be a clean relative path before it reaches argv."""
    if not isinstance(value, str) or not value or _CONTROL.search(value):
        return False
    try:
        validate_relative(value)
    except ValueError:
        return False
    return True


def _suspect_commits(root: Path, base: str | None, paths: list[str]) -> list[dict[str, Any]] | None:
    """List recent commits touching implicated paths; None when git cannot help."""

    if not paths:
        return []
    if shutil.which("git") is None or not (root / ".git").exists():
        return None
    rev_range = f"{base}..HEAD" if base else "HEAD"
    # GIT_NO_LAZY_FETCH keeps history inspection local: a partial clone must
    # never reach the network for a hint. ":(literal)" pins finding paths so
    # glob characters like [] in a path cannot widen the match.  Paths are
    # batched so a huge finding set cannot exceed the argv limit.
    env = {**os.environ, "GIT_NO_LAZY_FETCH": "1"}
    commits: list[dict[str, Any]] = []
    seen_shas: set[str] = set()
    for batch in _path_batches(paths):
        try:
            result = subprocess.run(
                ["git", "log", f"--max-count={_HINT_COMMITS}", "--format=%H%x00%cI%x00%s",
                 rev_range, "--", *batch],
                cwd=root, capture_output=True, text=True, timeout=15, env=env)
        except (OSError, subprocess.TimeoutExpired):
            return None
        if result.returncode != 0:
            return None
        for line in result.stdout.splitlines():
            sha, _, rest = line.partition("\x00")
            date, _, subject = rest.partition("\x00")
            if sha and sha[:12] not in seen_shas:
                seen_shas.add(sha[:12])
                commits.append({"sha": sha[:12], "subject": subject, "_date": date})
    # Each batch returns newest-first within its own path set only; merge by
    # committer date so a newer commit in a later batch is never truncated away.
    commits.sort(key=lambda commit: commit["_date"], reverse=True)
    return [{"sha": commit["sha"], "subject": commit["subject"]}
            for commit in commits[:_HINT_COMMITS]]


def _path_batches(paths: list[str]) -> Any:
    """Group literal pathspecs under both a count cap and an argv byte budget."""
    batch: list[str] = []
    size = 0
    for path in paths:
        argument = f":(literal){path}"
        weight = len(argument.encode("utf-8")) + 1
        if weight > _HINT_ARGV_BUDGET:
            continue  # a single argument this large can never reach git
        if batch and (len(batch) >= _HINT_PATH_BATCH
                      or size + weight > _HINT_ARGV_BUDGET):
            yield batch
            batch, size = [], 0
        batch.append(argument)
        size += weight
    if batch:
        yield batch


def _md(value: Any) -> str:
    """Render receipt-derived text as a Markdown code span with a safe fence.

    Backslash escapes are literal inside code spans, so the fence is a
    backtick run longer than any inside the text (padded when the text itself
    begins or ends with a backtick or space).  Control characters render as
    \\xNN so receipt data can inject neither markup nor terminal codes.
    """
    text = _CONTROL.sub(lambda match: f"\\x{ord(match.group(0)):02x}", str(value))
    longest = max((len(match.group()) for match in re.finditer(r"`+", text)), default=0)
    fence = "`" * (longest + 1)
    pad = " " if text.startswith(("`", " ")) or text.endswith(("`", " ")) else ""
    return f"{fence}{pad}{text}{pad}{fence}"


def _render_markdown(record: dict[str, Any]) -> str:
    lines = [
        "## exitzero run record",
        "",
        f"- Record: `{record['run_id']}`",
        f"- Receipts aggregated: {record['receipts']}"
        + (f" (+{len(record['corrupt_receipts'])} corrupt skipped)"
           if record["corrupt_receipts"] else ""),
        f"- Status: {', '.join(f'{_md(name)} {count}' for name, count in sorted(record['by_status'].items())) or 'none'}",
        f"- Commands: {', '.join(f'{_md(name)} {count}' for name, count in sorted(record['by_command'].items())) or 'none'}",
        "",
    ]
    if record.get("permission_runs"):
        lines.extend(["### Permission decisions", "",
                      "Recorded merge-time decisions; not a filesystem sandbox or a verified human approval."])
        for run in record["permission_runs"]:
            lines.append(f"- Base {_md(run['base_commit'])}: {_md(run['status'])}")
            for change in run["changes"]:
                lines.append(f"  - {_md(change['path'])}: {_md(change['zone'])}, {_md(change['decision'])}")
        lines.append("")
    hints = record["rollback_hints"]
    if hints["implicated_paths"]:
        lines.append("### Rollback hints — implicated paths")
        for entry in hints["implicated_paths"]:
            lines.append(f"- {_md(entry['path'])} — {entry['finding_count']} finding(s): "
                         + ", ".join(_md(rule) for rule in entry["rules"]))
        commits = hints.get("suspect_commits")
        if commits:
            lines.append("")
            lines.append("Recent commits touching these paths:")
            for commit in commits:
                lines.append(f"- {_md(commit['sha'])} {_md(commit['subject'])}")
        lines.append("")
        lines.append("Rollback stays manual: review these paths and revert deliberately.")
    else:
        lines.append("No rollback hints — no non-passing receipts implicated any path.")
    lines.append("")
    return "\n".join(lines)


def _persist_record(root: Path, record: dict[str, Any]) -> tuple[Path, Path]:
    base = f".exitzero/ledger/record-{record['run_id']}"
    json_path = safe_path(root, base + ".json")
    markdown_path = safe_path(root, base + ".md")
    write_atomic(json_path,
                 json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    write_atomic(markdown_path, _render_markdown(record))
    return json_path, markdown_path


def _publish_to_pr(pr: int, body: str, root: Path) -> str | None:
    """Post the record body via `gh`; the only external write, always explicit.

    The body is piped to ``gh`` over stdin: handing it a repository path would
    let a swapped or linked file substitute foreign content between our write
    and ``gh``'s read.
    """

    if shutil.which("gh") is None:
        return "--pr requires the GitHub CLI (gh) on PATH"
    try:
        result = subprocess.run(
            ["gh", "pr", "comment", str(pr), "--body-file", "-"],
            cwd=root, capture_output=True, text=True, timeout=30, input=body)
    except (OSError, subprocess.TimeoutExpired) as error:
        return f"gh invocation failed: {error}"
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown error"
        return f"gh pr comment failed ({result.returncode}): {detail}"
    return None


__all__ = ["API_VERSION", "ledger_publish", "register"]
