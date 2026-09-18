"""One runner for CLI, CI and local hook adapters."""
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
import json
import time
import uuid
import subprocess

from . import __version__
from .api import Context, Finding, Registry
from .files import safe_path, select_files, sha256_file
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


def _spec_patterns(context: Context, registry: Registry, spec) -> list[str]:
    patterns = list(spec.paths)
    if spec.kind in registry.check_inputs:
        extra_patterns = registry.check_inputs[spec.kind](context, spec)
        if (not isinstance(extra_patterns, (list, tuple))
                or not all(isinstance(pattern, str) for pattern in extra_patterns)):
            raise ValueError("Plugin returned invalid input patterns")
        patterns.extend(extra_patterns)
    return patterns


def _snapshot(context: Context, registry: Registry, collect: dict | None = None) -> dict[str, str]:
    root, policy = context.root, context.policy
    files = {context.policy_path}
    patterns = []
    for spec in specs(policy):
        spec_patterns = _spec_patterns(context, registry, spec)
        if collect is not None:
            collect[spec.id] = [path.relative_to(root).as_posix()
                                for path in select_files(root, spec_patterns)]
        patterns.extend(spec_patterns)
    files.update(select_files(root, patterns))
    # harness.config_files belongs to the harness plugin's schema; an invalid
    # shape must surface as that plugin's lint finding, not a core TypeError.
    extra = policy.get("harness", {}).get("config_files", [])
    names = ["AGENTS.md", ".cursor/hooks.json", *installed_inputs(root)]
    if isinstance(extra, list):
        names.extend(name for name in extra if isinstance(name, str))
    for name in names:
        candidate = safe_path(root, name)
        if candidate.is_file():
            files.add(candidate)
    return {p.relative_to(root).as_posix(): sha256_file(p) for p in sorted(files)}


def _prior_check_receipts(root: Path):
    """Stored check receipts newest first; malformed or non-check entries are skipped."""
    directory = safe_path(root, ".exitzero/runs")
    if not directory.is_dir():
        return
    files = sorted((p for p in directory.glob("*.json") if p.is_file()),
                   key=lambda p: p.stat().st_mtime_ns, reverse=True)
    for path in files:
        try:
            receipt = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            continue
        if isinstance(receipt, dict) and receipt.get("command") == "check":
            yield receipt


def _reusable_checks(root: Path, current_inputs: dict, policy_sha: str, check_specs: list) -> dict:
    """Map check id -> (receipt, entry) for checks whose recorded pass still holds.

    A prior passing result is reused only when the source receipt ran the same
    tool version against the same policy, the check recorded a per-check input
    list identical to today's selection, and every listed input still hashes to
    the recorded value. Added, deleted or modified inputs re-run the check;
    checks with no declared file inputs can never prove their inputs are stable
    and always re-run.
    """
    reusable: dict = {}
    wanted = {spec.id for spec in check_specs}
    for prior in _prior_check_receipts(root):
        if (prior.get("exit_code") not in (0, 1)
                or prior.get("tool_version") != __version__
                or prior.get("policy_sha256") != policy_sha):
            continue
        prior_inputs = prior.get("inputs")
        if not isinstance(prior_inputs, dict):
            continue
        for entry in prior.get("checks", []):
            if not isinstance(entry, dict):
                continue
            check_id = entry.get("id")
            if check_id not in wanted or check_id in reusable:
                continue
            recorded = entry.get("inputs")
            if (entry.get("status") != "passed" or not isinstance(recorded, list)
                    or not all(isinstance(rel, str) for rel in recorded)):
                continue
            current = current_inputs.get(check_id, [])
            if not current or sorted(recorded) != current:
                continue
            try:
                if all(prior_inputs.get(rel) == sha256_file(safe_path(root, rel))
                       for rel in current):
                    reusable[check_id] = (prior, entry)
            except (ValueError, OSError):
                continue
        if len(reusable) == len(wanted):
            break
    return reusable


def _requirement_findings(receipt: dict, outcomes: dict[str, str], valid: bool) -> list[Finding]:
    findings = []
    for requirement in receipt.get("requirements", []):
        statuses = [outcomes.get(check) for check in requirement["checks"]]
        status = "unverified"
        if receipt["command"] != "lint-config":
            if "failed" in statuses:
                status = "failed"
                findings.append(Finding("core.requirement-failed",
                                        f"Requirement {requirement['id']} has failed mapped checks; inspect their findings."))
            elif valid and statuses and all(value in ("passed", "reused") for value in statuses):
                status = "checks_passed"
            if status == "unverified":
                findings.append(Finding("core.requirement-unverified",
                                        f"Requirement {requirement['id']} lacks valid verification evidence; map checks and complete a stable run."))
        requirement["status"] = status
    return findings


def run(root: Path, policy_name: str, command: str, slot: str | None = None, *,
        input_error: bool = False, reuse: bool = False) -> dict:
    started = time.monotonic()
    receipt = {"schema_version": 1, "tool_version": __version__, "run_id": uuid.uuid4().hex,
               "started_at": datetime.now(timezone.utc).isoformat(), "command": command,
               "hook_slot": slot, "policy_sha256": None, "plugins": [], "inputs": {},
               "checks": [], "findings": [], "receipt": None}
    findings: list[Finding] = []
    operational_error = False
    inputs_changed = False
    verification_outcomes: dict[str, str] = {}
    try:
        path = safe_path(root, policy_name)
        if not path.is_file():
            raise ValueError("Policy must be a regular file")
        receipt["policy_sha256"] = sha256_file(path)
        policy = load_policy(path)
        if "requirements" in policy:
            receipt["requirements"] = [{"id": requirement["id"], "checks": list(requirement["checks"]),
                                        "status": "unverified"} for requirement in policy["requirements"]]
        registry = discover(policy["plugins"])
        if command == "lint-config" and not registry.linters:
            raise ValueError("No config linter is registered")
        receipt["plugins"] = policy["plugins"]
        context = Context(root, policy, path)
        if slot == "pre-commit":
            dirty = subprocess.run(["git", "-C", str(root), "diff", "--quiet", "--"],
                                   capture_output=True, timeout=15)
            untracked = subprocess.run(["git", "-C", str(root), "ls-files", "--others", "--exclude-standard"],
                                       capture_output=True, timeout=15)
            if dirty.returncode not in (0, 1) or untracked.returncode != 0:
                raise ValueError("Cannot inspect Git index")
            if dirty.returncode == 1 or untracked.stdout.strip():
                findings.append(Finding("core.index-mismatch", "Stage or stash all working-tree changes before using the pre-commit gate."))
        checks = specs(policy)
        if any(spec.kind not in registry.checks for spec in checks):
            raise ValueError("Policy references an unregistered check kind")
        check_inputs: dict[str, list[str]] = {}
        receipt["inputs"] = _snapshot(context, registry, check_inputs)
        for name, linter in registry.linters.items():
            result = _findings(linter(context))
            findings.extend(result)
            receipt["checks"].append({"id": name, "kind": "config-lint", "status": "failed" if result else "passed", "finding_count": len(result)})
        if command != "lint-config":
            reusable = (_reusable_checks(root, check_inputs, receipt["policy_sha256"], checks)
                        if reuse and slot is None else {})
            for spec in checks:
                if spec.id in reusable:
                    source_receipt, source_entry = reusable[spec.id]
                    verification_outcomes[spec.id] = "reused"
                    receipt["checks"].append({"id": spec.id, "kind": spec.kind, "status": "reused",
                                              "finding_count": source_entry.get("finding_count", 0),
                                              "inputs": check_inputs.get(spec.id, []),
                                              "reused_from": source_receipt["run_id"]})
                    continue
                result = _findings(registry.checks[spec.kind](context, spec))
                findings.extend(result)
                errors = [finding for finding in result if finding.severity == "error"]
                verification_outcomes[spec.id] = "failed" if errors else "passed"
                receipt["checks"].append({"id": spec.id, "kind": spec.kind, "status": verification_outcomes[spec.id],
                                          "finding_count": len(result),
                                          "inputs": check_inputs.get(spec.id, [])})
            if slot is not None:
                for handler in registry.hooks.get(slot, []):
                    findings.extend(_findings(handler(context, slot)))
        if _snapshot(context, registry) != receipt["inputs"]:
            inputs_changed = True
            findings.append(Finding("core.inputs-changed", "Inspected files changed during the run; rerun against stable inputs."))
    except (Exception, SystemExit, KeyboardInterrupt) as error:
        operational_error = True
        # Exception text may contain TOML values, file content or credentials.
        findings.append(Finding("core.error", f"Unable to complete run ({type(error).__name__}); inspect policy, paths and plugin settings."))
    if input_error:
        operational_error = True
        findings.append(Finding("core.hook-input", "Invalid hook input; expected a JSON object and a nonnegative integer loop_count."))
    findings.extend(_requirement_findings(receipt, verification_outcomes, not operational_error and not inputs_changed))
    receipt["exit_code"] = 2 if operational_error else int(any(f.severity == "error" for f in findings))
    receipt["status"] = {0: "passed", 1: "failed", 2: "error"}[receipt["exit_code"]]
    receipt["findings"] = [asdict(f) for f in findings]
    receipt["duration_ms"] = round((time.monotonic() - started) * 1000, 3)
    try:
        persist(root, receipt)
    except (OSError, ValueError):
        receipt["exit_code"], receipt["status"], receipt["receipt"] = 2, "error", None
        for requirement in receipt.get("requirements", []):
            if requirement["status"] == "checks_passed":
                requirement["status"] = "unverified"
        receipt["findings"].append(asdict(Finding("core.receipt", "Cannot persist required run receipt; check directory permissions and symlinks.")))
    return receipt
