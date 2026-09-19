"""Project-local setup diagnostics; never execute configured checks or hooks."""
import json
import math
import os
from pathlib import Path
import subprocess

from .api import Context, Finding
from .files import safe_path
from .hooks import (AGY_HOOK_NAME, SETTINGS_ADAPTERS, _manifest,
                    expected_adapter_command, expected_git_hook, lint_installed)

ADAPTER_PATHS = {
    "cursor": ".cursor/hooks.json",
    **{name: values[0] for name, values in SETTINGS_ADAPTERS.items()},
    "agy": ".agents/hooks.json",
}
ADAPTERS = (*ADAPTER_PATHS, "pre-commit")


def _git_hook(root: Path) -> str | None:
    """Resolve the active local hook; never read external hook directories."""
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--git-path", "hooks/pre-commit"],
            capture_output=True, text=True, timeout=15, check=False)
    except FileNotFoundError:
        return None
    if result.returncode != 0:
        return None
    path = Path(result.stdout.strip())
    path = Path(os.path.abspath(path if path.is_absolute() else root / path))
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return None


def input_paths(root: Path) -> list[str]:
    paths = list(ADAPTER_PATHS.values())
    git_hook = _git_hook(root)
    if git_hook is not None:
        paths.append(git_hook)
    return paths


def _document(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Hook configuration must be an object")
    return value


def _entries(data: dict, adapter: str) -> list[dict]:
    if adapter == "agy":
        owner = data.get(AGY_HOOK_NAME, {})
        entries = owner.get("Stop", []) if isinstance(owner, dict) else []
    else:
        hooks = data.get("hooks", {})
        event = "stop" if adapter == "cursor" else SETTINGS_ADAPTERS[adapter][1]
        entries = hooks.get(event, []) if isinstance(hooks, dict) else []
        if adapter != "cursor" and isinstance(entries, list):
            entries = [entry for group in entries if isinstance(group, dict)
                       for entry in (group.get("hooks") if isinstance(group.get("hooks"), list) else [])]
    return [entry for entry in entries if isinstance(entry, dict)] if isinstance(entries, list) else []


def diagnose(context: Context, required_adapter: str | None = None) -> tuple[list[dict], list[Finding]]:
    if required_adapter is not None and required_adapter not in ADAPTERS:
        raise ValueError("Unsupported doctor adapter")
    manifest = _manifest(context.root)
    recorded = set(manifest["files"]) | set(manifest["entries"])
    drift = {finding.path for finding in lint_installed(context)
             if finding.rule == "harness.hooks-drift"}
    diagnostics: list[dict] = []
    findings: list[Finding] = []
    for adapter in ADAPTERS:
        relative = _git_hook(context.root) if adapter == "pre-commit" else ADAPTER_PATHS[adapter]
        item = {
            "target": adapter, "state": "missing", "runtime": "unverified",
            "path": relative,
            "detail": "No exitzero installation is recorded for this project hook.",
            "next_step": f"If needed, run exitzero hooks install --adapter {adapter}.",
        }
        diagnostics.append(item)
        inactive_git_hook = adapter == "pre-commit" and relative not in recorded and any(
            Path(name).name == "pre-commit" for name in manifest["files"])
        if relative is None:
            item.update(state="unknown", detail="No repository-local Git hook path could be resolved.",
                        next_step="Check Git availability, the repository and core.hooksPath; external hooks need manual setup.")
        else:
            path = safe_path(context.root, relative)
            if os.path.lexists(path) and not path.is_file():
                raise ValueError("Hook configuration must be a regular file")
            if relative in recorded:
                item.update(state="configured", detail="The installed project hook matches its recorded configuration.",
                            next_step="Confirm the hook fires in your client and execution mode; require CI before merge.")
                if relative in drift:
                    item.update(state="misconfigured", detail="The installed hook changed or disappeared.",
                                next_step="Review the hook configuration, then reinstall and rerun doctor.")
                    findings.append(Finding("doctor.hook-drift", item["detail"], relative))
            elif path.is_file():
                item.update(state="unmanaged", detail="A hook configuration exists without an exitzero installation record.",
                            next_step="Review manual integrations; install exitzero if this client should use the gate.")

            if path.is_file() and adapter != "pre-commit":
                try:
                    data = _document(path)
                except (ValueError, UnicodeError):
                    item.update(state="misconfigured", detail="The project hook configuration is not a valid JSON object.")
                    findings.append(Finding("doctor.hook-config", item["detail"], relative))
                    continue
                expected = expected_adapter_command(context.root, context.policy_path, adapter)
                owned = [entry for entry in _entries(data, adapter)
                         if entry.get("command") == expected and entry.get("type", "command") == "command"]
                if relative in recorded and not owned:
                    item.update(state="misconfigured", detail="No installed stop command matches the current checkout, interpreter and policy.",
                                next_step=f"Run exitzero hooks install --adapter {adapter} after reviewing stale hook entries.")
                    findings.append(Finding("doctor.hook-command", item["detail"], relative))
                for entry in owned:
                    timeout = entry.get("timeout")
                    if (type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0):
                        item.update(state="misconfigured", detail="The gate hook needs a positive finite timeout.")
                        findings.append(Finding("doctor.hook-timeout", item["detail"], relative))
                    if adapter == "cursor":
                        if entry.get("failClosed") is not True:
                            item.update(state="misconfigured", detail="The Cursor gate does not explicitly enable failClosed.",
                                        next_step="Set failClosed to true on the exitzero entry, then reinstall and rerun doctor.")
                            findings.append(Finding("doctor.fail-closed", item["detail"], relative))
                        limit = entry.get("loop_limit")
                        if type(limit) is not int or limit < 1:
                            item.update(state="misconfigured", detail="The Cursor repair hook needs a positive loop_limit.",
                                        next_step="Set loop_limit to 1, then reinstall and rerun doctor.")
                            findings.append(Finding("doctor.loop-limit", item["detail"], relative))
                if adapter == "claude" and data.get("allowManagedHooksOnly") is True and relative in recorded:
                    item.update(state="misconfigured", detail="Project settings declare allowManagedHooksOnly; this project hook cannot enforce the gate.",
                                next_step="Use your managed hook deployment or required CI; confirm effective client settings.")
                    findings.append(Finding("doctor.managed-only", item["detail"], relative))
            elif path.is_file() and adapter == "pre-commit" and relative in recorded:
                if path.read_text(encoding="utf-8") != expected_git_hook(context.root, context.policy_path):
                    item.update(state="misconfigured", detail="The Git hook does not match the current checkout, interpreter and policy.",
                                next_step="Review the existing Git hook and reinstall or update its command manually.")
                    findings.append(Finding("doctor.hook-command", item["detail"], relative))
                if not os.access(path, os.X_OK):
                    item.update(state="misconfigured", detail="The installed Git pre-commit hook is not executable.",
                                next_step="Restore hook executable permissions or reinstall the hook.")
                    findings.append(Finding("doctor.hook-executable", item["detail"], relative))

        if inactive_git_hook:
            item.update(state="misconfigured", detail="A pre-commit installation is recorded at a different or unresolved Git hook path.",
                        next_step="Review core.hooksPath and reinstall at the active path; external directories require manual setup.")
            findings.append(Finding("doctor.hook-inactive", item["detail"], relative))
        if required_adapter == adapter and item["state"] != "configured":
            findings.append(Finding("doctor.required-adapter", "The requested adapter is not configured for this project.", relative))
        if adapter == "cursor":
            item["detail"] += " Stop provides repair feedback, not merge protection; headless stop behavior is client-version dependent."
        elif adapter == "agy":
            item["detail"] += " Stop execution in headless mode is not established by this configuration."
        elif adapter == "pre-commit":
            item["detail"] += " Local Git hooks can be bypassed."

    diagnostics.append({
        "target": "CI", "state": "unknown", "runtime": "unverified", "path": None,
        "detail": "Remote required checks, bypass permissions and actual CI execution were not inspected.",
        "next_step": "Follow docs/HOOKS.md#required-ci-setup: run the full gate on the merge checkout, preserve receipts and require the job in branch rules.",
    })
    return diagnostics, findings
