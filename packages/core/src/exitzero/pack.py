"""Prepare and publish a bounded set of project-local client integrations."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .files import safe_path, write_atomic
from .hooks import ADAPTER_EVENTS, _manifest, _prepare_target, _resolve_trust_base, prepare_install
from .policy import parse_policy, updated_agents


_GIT_ADAPTERS = frozenset({"pre-commit", "pre-push"})
_KNOWN_ADAPTERS = frozenset((*ADAPTER_EVENTS, *_GIT_ADAPTERS))
_MANIFEST_RELATIVE = ".exitzero/hooks.json"


def _hash(raw: bytes | None) -> str | None:
    return hashlib.sha256(raw).hexdigest() if raw is not None else None


def _adapters(policy: dict) -> list[str]:
    clients = policy.get("clients")
    if not isinstance(clients, dict):
        raise ValueError("Policy clients.adapters must be a nonempty list")
    adapters = clients.get("adapters")
    if (not isinstance(adapters, list) or not adapters
            or any(not isinstance(adapter, str) or not adapter for adapter in adapters)
            or len(set(adapters)) != len(adapters)):
        raise ValueError("Policy clients.adapters must be a nonempty list of unique adapters")
    unknown = sorted(set(adapters) - _KNOWN_ADAPTERS)
    if unknown:
        raise ValueError("Policy clients.adapters contains an unsupported adapter")
    return list(adapters)


def _target(root: Path, relative: str) -> dict:
    path, before, mode = _prepare_target(root, relative)
    return {"relative": relative, "path": path, "before": before,
            "before_mode": mode}


def _append_target(targets: list[dict], target: dict, content: str, mode: int) -> None:
    if any(item["relative"] == target["relative"] for item in targets):
        raise ValueError("Multiple policy adapters resolve to one hook target")
    targets.append({**target, "content": content, "mode": mode})


def _snapshot(root: Path, target: dict) -> tuple[bytes | None, int | None]:
    current = _target(root, target["relative"])
    return current["before"], current["before_mode"]


def _same_snapshot(root: Path, target: dict) -> bool:
    before, mode = _snapshot(root, target)
    return before == target["before"] and mode == target["before_mode"]


def _rollback(root: Path, changed: list[dict]) -> bool:
    """Restore only files that still contain the pack's published state."""
    complete = True
    for target in reversed(changed):
        try:
            path = safe_path(root, target["relative"])
            current, current_mode = _snapshot(root, target)
            desired = target["content"].encode("utf-8")
            if current == target["before"] and current_mode == target["before_mode"]:
                continue
            if current != desired or (current_mode != target["mode"] and not target.get("published")):
                complete = False
                continue
            if target["before"] is None:
                path.unlink()
            else:
                write_atomic(path, target["before"].decode("utf-8"))
                path.chmod(target["before_mode"])
        except Exception:
            complete = False
    return complete


def _publish(root: Path, targets: list[dict]) -> None:
    changed: list[dict] = []
    try:
        for target in targets:
            if (target["before"] == target["content"].encode("utf-8")
                    and target["before_mode"] == target["mode"]):
                continue
            if not _same_snapshot(root, target):
                raise RuntimeError("Pack changed during publication; review concurrent edits and retry")
            changed.append(target)
            path = safe_path(root, target["relative"])
            write_atomic(path, target["content"])
            target["published"] = True
            path.chmod(target["mode"])
    except Exception as error:
        if not _rollback(root, changed):
            raise RuntimeError("Pack application failed and rollback was incomplete") from error
        raise RuntimeError("Pack application failed; changes were rolled back") from error


def compile_pack(root: Path, policy_path: Path, policy: dict, *, apply: bool = False,
                 trust_base: str | None = None) -> dict:
    """Prepare or apply project-local outputs with per-file atomic writes.

    Preparation performs every parse and safety check before returning.  Apply
    takes a second byte/mode snapshot immediately before publication and rolls
    back earlier publications if a later write fails, while leaving concurrent
    edits untouched.
    """
    root = Path(root).resolve()
    policy_path = Path(policy_path).resolve()
    adapters = _adapters(policy)
    try:
        policy_relative = policy_path.relative_to(root).as_posix()
    except ValueError:
        raise ValueError("Policy path must be inside the repository") from None
    policy_target = _target(root, policy_relative)
    if policy_target["before"] is None:
        raise ValueError("Policy must be a regular file before preparing the policy pack")
    try:
        captured_policy = parse_policy(policy_target["before"].decode("utf-8"))
    except (UnicodeError, ValueError, TypeError):
        raise ValueError("Policy changed or is invalid; reload it and retry the policy pack") from None
    if captured_policy != policy:
        raise ValueError("Policy changed or is stale; reload it and retry the policy pack")
    manifest_target = _target(root, _MANIFEST_RELATIVE)
    manifest = _manifest(root)
    effective_trust_base = _resolve_trust_base(root, trust_base)
    if "permissions" in policy and effective_trust_base is None:
        raise ValueError("Permission zones require an operator-supplied trust-base before preparing hooks")
    targets: list[dict] = []

    agents = _target(root, "AGENTS.md")
    current_agents = agents["before"].decode("utf-8") if agents["before"] is not None else ""
    _append_target(targets, agents, updated_agents(current_agents, policy),
                   agents["before_mode"] if agents["before_mode"] is not None else 0o644)

    for adapter in adapters:
        prepared = prepare_install(root, policy_path, adapter, manifest,
                                   trust_base=effective_trust_base, strict_cursor=True)
        manifest = prepared["manifest"]
        target = {"relative": prepared["relative"],
                  "path": safe_path(root, prepared["relative"]),
                  "before": prepared["before"],
                  "before_mode": prepared["before_mode"]}
        _append_target(targets, target, prepared["content"], prepared["mode"])

    manifest_content = json.dumps(manifest, sort_keys=True, indent=2) + "\n"
    _append_target(targets, manifest_target, manifest_content,
                   manifest_target["before_mode"] if manifest_target["before_mode"] is not None else 0o644)

    if apply:
        if (not _same_snapshot(root, policy_target)
                or not all(_same_snapshot(root, target) for target in targets)):
            raise RuntimeError("Pack changed during preparation; review concurrent edits and retry")
        _publish(root, targets)

    changes = []
    for target in targets:
        before = target["before"]
        after = target["content"].encode("utf-8")
        mode_changed = target["before_mode"] != target["mode"]
        changes.append({"path": target["relative"],
                        "state": "unchanged" if before == after and not mode_changed else "update" if before is not None else "create",
                        "before_sha256": _hash(before), "after_sha256": _hash(after)})
    return {"schema_version": 1, "applied": bool(apply), "changes": changes,
            "adapters": adapters,
            "limitations": [
                "Client trust prompts and hook execution modes remain host-specific; require CI for merge protection.",
                "Only declared project-local adapters are managed; global settings, CI configuration and network state are unchanged.",
            ]}
