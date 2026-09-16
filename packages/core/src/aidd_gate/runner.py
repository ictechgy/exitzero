"""One runner for CLI, CI and local hook adapters."""
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import time
import uuid
import subprocess

from . import __version__
from .api import Context, Finding
from .files import safe_path, select_files
from .ledger import persist
from .hooks import installed_inputs
from .loader import discover
from .policy import load_policy, specs


def _findings(values: list[Finding]) -> list[Finding]:
    if not isinstance(values, list):
        raise ValueError("Plugin returned invalid findings")
    for finding in values:
        if (not isinstance(finding, Finding)
                or not isinstance(finding.rule, str) or not finding.rule
                or not isinstance(finding.message, str)
                or (finding.path is not None and not isinstance(finding.path, str))
                or (finding.line is not None and (type(finding.line) is not int or finding.line < 1))
                or finding.severity not in ("error", "warning")):
            raise ValueError("Plugin returned invalid findings")
    return values


def _snapshot(root: Path, policy: dict, policy_path: Path) -> dict[str, str]:
    files = {policy_path}
    for spec in specs(policy):
        files.update(select_files(root, spec.paths))
    for name in ["AGENTS.md", ".cursor/hooks.json", *installed_inputs(root), *policy.get("harness", {}).get("config_files", [])]:
        candidate = safe_path(root, name)
        if candidate.is_file():
            files.add(candidate)
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(files)}


def run(root: Path, policy_name: str, command: str, slot: str | None = None, *, input_error: bool = False) -> dict:
    started = time.monotonic()
    receipt = {"schema_version": 1, "tool_version": __version__, "run_id": uuid.uuid4().hex,
               "started_at": datetime.now(timezone.utc).isoformat(), "command": command,
               "hook_slot": slot, "policy_sha256": None, "plugins": [], "inputs": {},
               "checks": [], "findings": [], "receipt": None}
    findings: list[Finding] = []
    operational_error = False
    try:
        path = safe_path(root, policy_name)
        receipt["policy_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        policy = load_policy(path)
        registry = discover(policy["plugins"])
        if command == "lint-config" and not registry.linters:
            raise ValueError("No config linter is registered")
        receipt["plugins"] = policy["plugins"]
        context = Context(root, policy, path)
        if slot == "pre-commit":
            dirty = subprocess.run(["git", "-C", str(root), "diff", "--quiet", "--"], capture_output=True)
            untracked = subprocess.run(["git", "-C", str(root), "ls-files", "--others", "--exclude-standard"], capture_output=True)
            if dirty.returncode not in (0, 1) or untracked.returncode != 0:
                raise ValueError("Cannot inspect Git index")
            if dirty.returncode == 1 or untracked.stdout.strip():
                findings.append(Finding("core.index-mismatch", "Stage or stash all working-tree changes before using the pre-commit gate."))
        checks = specs(policy)
        if any(spec.kind not in registry.checks for spec in checks):
            raise ValueError("Policy references an unregistered check kind")
        receipt["inputs"] = _snapshot(root, policy, path)
        for name, linter in registry.linters.items():
            result = _findings(linter(context))
            findings.extend(result)
            receipt["checks"].append({"id": name, "kind": "config-lint", "status": "failed" if result else "passed", "finding_count": len(result)})
        if command != "lint-config":
            for spec in checks:
                result = _findings(registry.checks[spec.kind](context, spec))
                findings.extend(result)
                receipt["checks"].append({"id": spec.id, "kind": spec.kind, "status": "failed" if result else "passed", "finding_count": len(result)})
            if slot is not None:
                for handler in registry.hooks.get(slot, []):
                    findings.extend(_findings(handler(context, slot)))
        if _snapshot(root, policy, path) != receipt["inputs"]:
            findings.append(Finding("core.inputs-changed", "Inspected files changed during the run; rerun against stable inputs."))
    except (Exception, SystemExit, KeyboardInterrupt) as error:
        operational_error = True
        # Exception text may contain TOML values, file content or credentials.
        findings.append(Finding("core.error", f"Unable to complete run ({type(error).__name__}); inspect policy, paths and plugin settings."))
    if input_error:
        operational_error = True
        findings.append(Finding("core.hook-input", "Invalid hook input; expected a JSON object and a nonnegative integer loop_count."))
    receipt["exit_code"] = 2 if operational_error else int(any(f.severity == "error" for f in findings))
    receipt["status"] = {0: "passed", 1: "failed", 2: "error"}[receipt["exit_code"]]
    receipt["findings"] = [asdict(f) for f in findings]
    receipt["duration_ms"] = round((time.monotonic() - started) * 1000, 3)
    try:
        persist(root, receipt)
    except (OSError, ValueError):
        receipt["exit_code"], receipt["status"], receipt["receipt"] = 2, "error", None
        receipt["findings"].append(asdict(Finding("core.receipt", "Cannot persist required run receipt; check directory permissions and symlinks.")))
    return receipt
