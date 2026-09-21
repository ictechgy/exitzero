#!/usr/bin/env python3
"""Verify a built exitzero wheel in a fresh, offline environment.

The runner deliberately exercises the installed console script and an installed
pre-commit hook.  Its temporary repository and virtual environment are removed
after the run; receipts and bounded command evidence are copied to the
checkout's release ledger before cleanup.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
import venv
import zipfile
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
MAX_OUTPUT = 64 * 1024
RECEIPT_KEYS = {"schema_version", "tool_version", "run_id", "command", "exit_code", "status", "receipt"}
RECEIPT_STATUS = {0: "passed", 1: "failed", 2: "error"}
RUN_ID = re.compile(r"[0-9a-f]{32}\Z")


class ReleaseFailure(RuntimeError):
    """An installed-wheel acceptance check failed."""


def _runs_directory(repository: Path) -> Path:
    """Return the repository's receipt directory, rejecting symlink escapes."""

    if repository.is_symlink():
        raise ReleaseFailure("temporary repository is symlinked")
    gate = repository / ".exitzero"
    runs = gate / "runs"
    if gate.is_symlink() or runs.is_symlink():
        raise ReleaseFailure("receipt directory contains a symlink")
    return runs


def _validate_receipt(receipt: object, source: Path) -> dict[str, Any]:
    if not isinstance(receipt, dict) or not RECEIPT_KEYS.issubset(receipt):
        raise ReleaseFailure(f"receipt is missing required fields: {source.name}")
    if receipt.get("schema_version") != 1:
        raise ReleaseFailure(f"receipt schema is not version 1: {source.name}")
    run_id = receipt.get("run_id")
    if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id) or source.name != f"{run_id}.json":
        raise ReleaseFailure(f"receipt run_id does not match its filename: {source.name}")
    exit_code = receipt.get("exit_code")
    if type(exit_code) is not int or exit_code not in RECEIPT_STATUS:
        raise ReleaseFailure(f"receipt has an invalid exit code: {source.name}")
    if receipt.get("status") != RECEIPT_STATUS[exit_code]:
        raise ReleaseFailure(f"receipt status disagrees with exit code: {source.name}")
    if receipt.get("receipt") != f".exitzero/runs/{source.name}":
        raise ReleaseFailure(f"receipt path does not match its source: {source.name}")
    return receipt


def _returned_receipt_path(repository: Path, receipt_name: object) -> Path:
    if not isinstance(receipt_name, str):
        raise ReleaseFailure("command did not return a receipt path")
    relative = Path(receipt_name)
    if relative.is_absolute() or ".." in relative.parts or relative.parts != (".exitzero", "runs", relative.name):
        raise ReleaseFailure("returned receipt path is outside .exitzero/runs")
    runs = _runs_directory(repository)
    candidate = repository / relative
    if candidate.parent != runs or any(part.is_symlink() for part in (candidate,)):
        raise ReleaseFailure("returned receipt path contains a symlink")
    if not candidate.is_file() or not candidate.resolve().is_relative_to(runs.resolve()):
        raise ReleaseFailure("returned receipt path is outside .exitzero/runs")
    return candidate


def _artifact_root(run_id: str) -> Path:
    """Create an output directory without following an existing symlink."""

    gate = ROOT / ".exitzero"
    release = gate / "releases"
    for path in (gate, release):
        if path.exists() and path.is_symlink():
            raise ReleaseFailure(f"refusing symlinked evidence directory: {path}")
    release.mkdir(parents=True, exist_ok=True)
    artifact = release / run_id
    artifact.mkdir()
    return artifact


def _text_output(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return "" if value is None else str(value)


def _safe_output(value: str, temporary_root: Path, venv_root: Path, wheel: Path) -> str:
    """Bound and redact process output before it becomes durable evidence."""

    value = value.replace(str(temporary_root), "<temporary-repo>")
    value = value.replace(str(venv_root), "<temporary-venv>")
    value = value.replace(str(wheel), "<wheel>")
    value = re.sub(r"(?i)(token|password|secret|api[_-]?key)\s*[:=]\s*\S+", r"\1=<redacted>", value)
    if len(value) > MAX_OUTPUT:
        return value[:MAX_OUTPUT] + "\n<output truncated>\n"
    return value


class ReleaseRun:
    def __init__(self, artifact: Path, wheel: Path, temporary_root: Path, venv_root: Path):
        self.artifact = artifact
        self.receipts_dir = artifact / "receipts"
        self.receipts_dir.mkdir()
        self.wheel = wheel
        self.temporary_root = temporary_root
        self.venv_root = venv_root
        self.commands: list[dict[str, Any]] = []
        self.receipts: dict[str, str] = {}
        self.errors: list[str] = []

    def command(self, label: str, argv: list[str], cwd: Path, env: dict[str, str], timeout: int = 90) -> subprocess.CompletedProcess[str]:
        started = time.monotonic()
        timed_out = False
        try:
            process = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired as error:
            timed_out = True
            process = subprocess.CompletedProcess(argv, 124, _text_output(error.stdout), _text_output(error.stderr))
        record = {
            "label": label,
            "argv": [_safe_output(str(item), self.temporary_root, self.venv_root, self.wheel) for item in argv],
            "cwd": _safe_output(str(cwd), self.temporary_root, self.venv_root, self.wheel),
            "exit_code": process.returncode,
            "timed_out": timed_out,
            "duration_ms": round((time.monotonic() - started) * 1000, 3),
            "stdout": _safe_output(_text_output(process.stdout), self.temporary_root, self.venv_root, self.wheel),
            "stderr": _safe_output(_text_output(process.stderr), self.temporary_root, self.venv_root, self.wheel),
        }
        self.commands.append(record)
        if timed_out:
            raise ReleaseFailure(f"{label} exceeded {timeout} seconds")
        return process

    def archive_receipts(self, stage: str, repository: Path) -> list[dict[str, Any]]:
        runs = _runs_directory(repository)
        if not runs.is_dir():
            return []
        archived: list[dict[str, Any]] = []
        for source in sorted(runs.glob("*.json")):
            if source.is_symlink() or not source.resolve().is_relative_to(runs.resolve()):
                raise ReleaseFailure(f"receipt source is outside the temporary runs directory: {source.name}")
            try:
                receipt = json.loads(source.read_text(encoding="utf-8"))
            except (OSError, ValueError) as error:
                raise ReleaseFailure(f"invalid run receipt {source.name}: {type(error).__name__}") from error
            receipt = _validate_receipt(receipt, source)
            run_id = receipt["run_id"]
            key = source.resolve().as_posix()
            destination_name = f"{stage}-{run_id}.json"
            destination = self.receipts_dir / destination_name
            if key not in self.receipts:
                shutil.copy2(source, destination)
                self.receipts[key] = f"receipts/{destination_name}"
            archived.append({"source": f".exitzero/runs/{source.name}", "path": self.receipts[key], "receipt": receipt})
        return archived

    def save(self, summary: dict[str, Any]) -> None:
        (self.artifact / "commands.json").write_text(json.dumps(self.commands, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        summary["commands_evidence"] = "commands.json"
        summary["receipts"] = sorted(self.receipts.values())
        summary["receipt_count"] = len(self.receipts)
        summary["errors"] = [*summary.get("errors", []), *self.errors]
        (self.artifact / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def _environment(venv_root: Path) -> dict[str, str]:
    env = {
        "PATH": str(venv_root / "bin") + os.pathsep + os.environ.get("PATH", ""),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "PIP_CONFIG_FILE": os.devnull,
        "PIP_NO_INDEX": "1",
        "PIP_NO_CACHE_DIR": "1",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PYTHONNOUSERSITE": "1",
    }
    return env


def _apply_workspace_errors(summary: dict[str, Any], state: dict[str, Any]) -> None:
    errors = [error for error in state.get("errors", []) if isinstance(error, str)]
    if errors:
        summary.setdefault("errors", []).extend(errors)
        summary["status"] = "failed"


@contextmanager
def _temporary_workspace(state: dict[str, Any]):
    """Keep the temporary repository alive long enough to archive its receipts."""

    with tempfile.TemporaryDirectory(prefix="exitzero-release-") as temporary:
        try:
            yield Path(temporary)
        finally:
            release = state.get("release")
            repository = state.get("repository")
            if isinstance(release, ReleaseRun) and isinstance(repository, Path):
                try:
                    release.archive_receipts("final", repository)
                except Exception as error:
                    state.setdefault("errors", []).append(
                        f"receipt archive failed ({type(error).__name__})"
                    )


def _python(venv_root: Path) -> Path:
    candidate = venv_root / "bin" / "python"
    if not candidate.is_file():
        raise ReleaseFailure(f"virtualenv Python was not created: {candidate}")
    return candidate


def _git(run: ReleaseRun, repo: Path, env: dict[str, str], args: list[str], label: str, expected: int | None = 0) -> subprocess.CompletedProcess[str]:
    process = run.command(label, ["git", "-C", str(repo), *args], repo, env)
    if expected is not None and process.returncode != expected:
        raise ReleaseFailure(f"{label} exited {process.returncode}, expected {expected}")
    return process


def _commit(run: ReleaseRun, repo: Path, env: dict[str, str], message: str, label: str, expected: int) -> subprocess.CompletedProcess[str]:
    args = ["-c", "user.name=exitzero release test", "-c", "user.email=release-test@example.invalid",
            "-c", "commit.gpgSign=false", "-c", "tag.gpgSign=false", "commit", "-m", message]
    return _git(run, repo, env, args, label, expected)


def _run_names(repository: Path) -> set[str]:
    runs = repository / ".exitzero" / "runs"
    return {path.name for path in runs.glob("*.json")} if runs.is_dir() else set()


def _verify_hook_receipt(
    run: ReleaseRun,
    repository: Path,
    before: set[str],
    stage: str,
    expected_exit: int,
    expected_rule: str | None = None,
) -> dict[str, Any]:
    entries = run.archive_receipts(stage, repository)
    fresh = [entry for entry in entries if Path(entry["source"]).name not in before]
    if len(fresh) != 1:
        raise ReleaseFailure(f"{stage} did not produce exactly one new gate receipt")
    receipt = fresh[0]["receipt"]
    if receipt.get("hook_slot") != "pre-commit" or receipt.get("exit_code") != expected_exit:
        raise ReleaseFailure(f"{stage} receipt has the wrong hook slot or exit code")
    if receipt.get("status") != {0: "passed", 1: "failed", 2: "error"}[expected_exit]:
        raise ReleaseFailure(f"{stage} receipt status disagrees with its exit code")
    if expected_rule is not None:
        findings = receipt.get("findings", [])
        if not any(isinstance(finding, dict) and finding.get("rule") == expected_rule for finding in findings):
            raise ReleaseFailure(f"{stage} receipt is missing finding {expected_rule}")
    return receipt


def _head(run: ReleaseRun, repo: Path, env: dict[str, str], label: str) -> str:
    process = _git(run, repo, env, ["rev-parse", "HEAD"], label)
    return process.stdout.strip()


def _json_cli(run: ReleaseRun, cli: Path, repo: Path, env: dict[str, str], args: list[str], label: str, expected: int) -> dict[str, Any]:
    process = run.command(label, [str(cli), "--root", str(repo), *args, "--format", "json"], repo, env)
    if process.returncode != expected:
        raise ReleaseFailure(f"{label} exited {process.returncode}, expected {expected}")
    try:
        payload = json.loads(process.stdout)
    except json.JSONDecodeError as error:
        raise ReleaseFailure(f"{label} did not emit JSON: {type(error).__name__}") from error
    if not isinstance(payload, dict):
        raise ReleaseFailure(f"{label} JSON output is not an object")
    receipt_path = _returned_receipt_path(repo, payload.get("receipt"))
    try:
        saved = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ReleaseFailure(f"{label} receipt is unreadable: {type(error).__name__}") from error
    saved = _validate_receipt(saved, receipt_path)
    if payload != saved or saved.get("exit_code") != expected:
        raise ReleaseFailure(f"{label} JSON output and saved receipt disagree")
    return payload


def _report(run: ReleaseRun, cli: Path, repo: Path, env: dict[str, str], expected: dict[str, Any], label: str) -> None:
    process = run.command(label, [str(cli), "--root", str(repo), "report", "--format", "json"], repo, env)
    if process.returncode != 0:
        raise ReleaseFailure(f"{label} exited {process.returncode}")
    try:
        payload = json.loads(process.stdout)
    except json.JSONDecodeError as error:
        raise ReleaseFailure(f"{label} did not emit JSON: {type(error).__name__}") from error
    if payload != expected:
        raise ReleaseFailure(f"{label} output differs from the latest saved receipt")


def _validate_wheel(wheel: Path) -> dict[str, Any]:
    if not wheel.is_file() or wheel.suffix != ".whl":
        raise ReleaseFailure(f"wheel does not exist or is not a .whl file: {wheel}")
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        required = {"exitzero/__init__.py", "exitzero/cli.py", "exitzero_verify/__init__.py", "exitzero_harness/__init__.py"}
        missing = sorted(required - names)
        if missing:
            raise ReleaseFailure("wheel is missing runtime packages: " + ", ".join(missing))
        metadata_names = sorted(name for name in names if name.endswith(".dist-info/METADATA"))
        if len(metadata_names) != 1:
            raise ReleaseFailure("wheel must contain exactly one dist-info METADATA file")
        metadata = archive.read(metadata_names[0]).decode("utf-8", errors="strict")
        dependencies = [line for line in metadata.splitlines() if line.startswith("Requires-Dist:")]
        if dependencies:
            raise ReleaseFailure("wheel declares runtime dependencies: " + ", ".join(dependencies))
        entry_points = [name for name in names if name.endswith(".dist-info/entry_points.txt")]
        if not entry_points:
            raise ReleaseFailure("wheel has no entry point metadata")
        entry_text = archive.read(entry_points[0]).decode("utf-8", errors="strict")
        if "verify = exitzero_verify" not in entry_text or "harness = exitzero_harness" not in entry_text:
            raise ReleaseFailure("wheel is missing verify/harness plugin entry points")
    return {"path": str(wheel), "sha256": digest, "size": wheel.stat().st_size}


def _verify_node_integrity(run: ReleaseRun, cli: Path, temporary: Path, env: dict[str, str]) -> dict[str, Any]:
    """Prove the installed static check, without requiring Node or npm dependencies."""
    repo = temporary / "node-repo"
    repo.mkdir()
    original = "test('kept', () => {});\ntest('removed', () => {});\n"
    test = repo / "test.js"
    test.write_text(original, encoding="utf-8")
    try:
        init = run.command("node-profile-init", [str(cli), "--root", str(repo), "init", "--profile", "node",
                           "--test-command", '{python} -c "raise SystemExit(0)"',
                           "--test-integrity-base", "HEAD"], repo, env)
        if init.returncode:
            raise ReleaseFailure("installed Node integrity initialization failed")
        _git(run, repo, env, ["init", "-q"], "node-git-init")
        _git(run, repo, env, ["add", "."], "node-git-add")
        _commit(run, repo, env, "Node test baseline", "node-git-commit", 0)
        cases = []
        for label, source, expected in (
            ("baseline", original, 0),
            ("deletion", original.splitlines(keepends=True)[0], 1),
            ("skip", original.replace("test('removed'", "test.skip('removed'"), 1),
            ("repair", original, 0),
        ):
            test.write_text(source, encoding="utf-8")
            receipt = _json_cli(run, cli, repo, env, ["check"], "node-integrity-" + label, expected)
            if expected and {f["rule"] for f in receipt["findings"]} != {"test-integrity"}:
                raise ReleaseFailure("installed Node integrity case failed for an unexpected reason")
            cases.append({"name": label, "exit_code": expected, "run_id": receipt["run_id"]})
        return {"name": "installed-node-test-integrity", "status": "passed", "cases": cases}
    finally:
        run.archive_receipts("node-integrity", repo)


def _verify_policy_pack_and_authority(run: ReleaseRun, cli: Path, temporary: Path,
                                      env: dict[str, str]) -> dict[str, Any]:
    """Exercise installed policy packing, trusted authority and connections."""
    repo = temporary / "policy-authority"
    repo.mkdir()
    (repo / "app.py").write_text("from lib import handler\nhandler()\n", encoding="utf-8")
    (repo / "lib.py").write_text("def handler():\n    return 1\n", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests/test_app.py").write_text("def test_placeholder():\n    assert True\n", encoding="utf-8")
    init = run.command("authority-init", [str(cli), "--root", str(repo), "init", "--profile", "python"], repo, env)
    if init.returncode:
        raise ReleaseFailure("installed authority scenario initialization failed")
    policy_text = '''version = 1
plugins = ["verify", "harness"]

[[checks]]
id = "syntax"
kind = "python.syntax"
paths = ["app.py", "lib.py", "tests/**/*.py"]

[[checks]]
id = "connections"
kind = "python.connections"
paths = ["app.py"]
[checks.options]
roots = ["."]
connections = [{source = "app.py", target = "lib.py", symbol = "handler", within = "<module>", usage = "call"}]

[harness]
config_files = []
rules = []

[clients]
adapters = ["cursor", "pre-push"]

[permissions]
editable = ["app.py"]
protected = ["tests/**"]
immutable = [".cursor/**", "AGENTS.md"]
'''
    policy = repo / "exitzero.toml"
    policy.write_text(policy_text, encoding="utf-8")
    sync = run.command("authority-sync-policy", [str(cli), "--root", str(repo), "init", "--sync"], repo, env)
    if sync.returncode:
        raise ReleaseFailure("installed authority policy synchronization failed")
    _git(run, repo, env, ["init", "-q"], "authority-git-init")
    _git(run, repo, env, ["add", "."], "authority-git-add-a")
    baseline_a = _commit(run, repo, env, "authority baseline A", "authority-git-commit-a", 0).stdout
    baseline_a = _head(run, repo, env, "authority-head-a")

    def pack(label: str, base: str) -> dict[str, Any]:
        process = run.command(label, [str(cli), "--root", str(repo), "policy-pack", "--apply",
                                      "--trust-base", base, "--format", "json"], repo, env)
        if process.returncode != 0:
            raise ReleaseFailure(f"{label} exited {process.returncode}")
        try:
            value = json.loads(process.stdout)
        except json.JSONDecodeError as error:
            raise ReleaseFailure(f"{label} did not emit JSON: {type(error).__name__}") from error
        if not isinstance(value, dict) or value.get("applied") is not True:
            raise ReleaseFailure(f"{label} did not report an applied pack")
        return value

    pack_a = pack("authority-pack-a", baseline_a)
    cursor = repo / ".cursor/hooks.json"
    cursor_a = cursor.read_bytes()
    if b"--use-installed-authority" not in cursor_a or (repo / ".git/hooks/pre-commit").exists():
        raise ReleaseFailure("installed pack did not produce the expected cursor authority hook set")
    _git(run, repo, env, ["add", "."], "authority-git-add-b")
    _commit(run, repo, env, "authority generated hooks", "authority-git-commit-b", 0)
    baseline_b = _head(run, repo, env, "authority-head-b")
    pack_b = pack("authority-pack-b", baseline_b)
    changed_pack_paths = sorted(change["path"] for change in pack_b.get("changes", [])
                                 if isinstance(change, dict) and change.get("state") != "unchanged")
    if changed_pack_paths != [".exitzero/hooks.json"] or cursor.read_bytes() != cursor_a:
        raise ReleaseFailure("authority repack changed more than the installed manifest")

    cases: list[dict[str, Any]] = []

    def evidence(name: str, receipt: dict[str, Any]) -> None:
        permissions = receipt.get("permissions", {})
        changes = permissions.get("changes", []) if isinstance(permissions, dict) else []
        cases.append({"name": name, "run_id": receipt.get("run_id"),
                      "status": permissions.get("status") if isinstance(permissions, dict) else None,
                      "changes": [{key: change.get(key) for key in ("path", "zone", "decision", "change")}
                                  for change in changes if isinstance(change, dict) and "path" in change]})

    editable_source = "from lib import handler\nhandler()\n# editable\n"
    (repo / "app.py").write_text(editable_source, encoding="utf-8")
    accepted = _json_cli(run, cli, repo, env, ["check", "--trust-base", baseline_b], "authority-editable", 0)
    run.archive_receipts("authority-editable", repo)
    evidence("editable", accepted)
    (repo / "app.py").write_text("from lib import handler\n# registration removed\n", encoding="utf-8")
    deleted_registration = _json_cli(run, cli, repo, env, ["check", "--trust-base", baseline_b],
                                      "authority-connection-deleted", 1)
    run.archive_receipts("authority-connection-deleted", repo)
    if not any(finding.get("rule") == "connections" for finding in deleted_registration.get("findings", [])):
        raise ReleaseFailure("installed python.connections did not reject deleted registration")
    evidence("connection-deleted", deleted_registration)
    (repo / "app.py").write_text(editable_source, encoding="utf-8")
    (repo / "tests/new.py").write_text("value = 1\n", encoding="utf-8")
    protected = _json_cli(run, cli, repo, env, ["check", "--trust-base", baseline_b], "authority-protected", 1)
    run.archive_receipts("authority-protected", repo)
    if protected.get("checks") or not any(finding.get("rule") == "core.permission-protected"
                                          for finding in protected.get("findings", [])):
        raise ReleaseFailure("protected authority change was not rejected before checks")
    evidence("protected", protected)
    (repo / "tests/new.py").unlink()
    original_policy = policy.read_text(encoding="utf-8")
    weakened_policy = original_policy.split("\n[permissions]\n", 1)[0] + "\n"
    policy.write_text(weakened_policy, encoding="utf-8")
    weakened = _json_cli(run, cli, repo, env, ["check", "--trust-base", baseline_b], "authority-weakened-policy", 1)
    run.archive_receipts("authority-weakened-policy", repo)
    if weakened.get("checks") or not any(finding.get("rule") == "core.permission-immutable"
                                         for finding in weakened.get("findings", [])):
        raise ReleaseFailure("candidate policy weakening was not rejected before plugins")
    evidence("weakened-policy", weakened)
    policy.write_text(original_policy, encoding="utf-8")
    repaired = _json_cli(run, cli, repo, env, ["check", "--trust-base", baseline_b], "authority-repaired", 0)
    run.archive_receipts("authority-repaired", repo)
    evidence("repaired", repaired)
    doctor = _json_cli(run, cli, repo, env, ["doctor", "--trust-base", baseline_b], "authority-doctor", 0)
    run.archive_receipts("authority-doctor", repo)
    states = {item.get("target"): item.get("state") for item in doctor.get("diagnostics", [])
              if isinstance(item, dict)}
    if states.get("cursor") != "configured" or states.get("pre-push") != "configured":
        raise ReleaseFailure("installed doctor did not recognize declared adapters")
    return {"name": "installed-policy-pack-authority-connections", "status": "passed",
            "pack_a": {"applied": pack_a.get("applied")},
            "pack_b_changed_paths": changed_pack_paths,
            "cursor_hook_sha256": hashlib.sha256(cursor_a).hexdigest(),
            "cases": cases,
            "doctor": {"cursor": states.get("cursor"), "pre-push": states.get("pre-push")}}


def run(wheel: Path) -> tuple[dict[str, Any], Path]:
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:10]
    artifact = _artifact_root(run_id)
    wheel = wheel.resolve()
    summary: dict[str, Any] = {"schema_version": 1, "tool": "exitzero release wheel runner", "run_id": run_id,
                               "status": "failed", "wheel": {"path": str(wheel)}, "checks": [], "errors": []}
    try:
        summary["wheel"] = _validate_wheel(wheel)
    except Exception as error:
        summary["errors"].append(f"{type(error).__name__}: {error}")
        (artifact / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        return summary, artifact
    release: ReleaseRun | None = None
    workspace_state: dict[str, Any] = {}
    try:
        with _temporary_workspace(workspace_state) as temporary_root:
            venv_root = temporary_root / "venv"
            venv.EnvBuilder(with_pip=False, clear=False, symlinks=(os.name != "nt")).create(venv_root)
            python = _python(venv_root)
            env = _environment(venv_root)
            release = ReleaseRun(artifact, wheel, temporary_root, venv_root)
            workspace_state["release"] = release
            ensurepip = release.command("ensurepip", [str(python), "-m", "ensurepip", "--upgrade"], temporary_root, env, timeout=180)
            if ensurepip.returncode != 0:
                raise ReleaseFailure(f"ensurepip exited {ensurepip.returncode}")
            install = release.command("install-wheel", [str(python), "-m", "pip", "install", "--no-index", "--no-deps", "--disable-pip-version-check", str(wheel)], temporary_root, env, timeout=180)
            if install.returncode != 0:
                raise ReleaseFailure(f"offline wheel install exited {install.returncode}")
            cli = venv_root / "bin" / "exitzero"
            if not cli.is_file():
                raise ReleaseFailure(f"installed console script is missing: {cli}")
            repo = temporary_root / "repo"
            repo.mkdir()
            workspace_state["repository"] = repo
            init = release.command("init", [str(cli), "--root", str(repo), "init"], repo, env)
            if init.returncode != 0:
                raise ReleaseFailure(f"installed init exited {init.returncode}")
            policy = repo / "exitzero.toml"
            policy.write_text(re.sub(r"^plugins\s*=.*$", 'plugins = ["verify", "harness"]', policy.read_text(encoding="utf-8"), count=1, flags=re.MULTILINE), encoding="utf-8")
            sync = release.command("init-sync-entry-point-aliases", [str(cli), "--root", str(repo), "init", "--sync"], repo, env)
            if sync.returncode != 0:
                raise ReleaseFailure(f"installed init --sync exited {sync.returncode}")
            check = _json_cli(release, cli, repo, env, ["check"], "check-installed-aliases", 0)
            release.archive_receipts("check", repo)
            if check.get("plugins") != ["verify", "harness"]:
                raise ReleaseFailure("installed plugin entry-point aliases were not discovered")
            _report(release, cli, repo, env, check, "report-after-check")
            lint = _json_cli(release, cli, repo, env, ["lint-config"], "lint-config-installed", 0)
            release.archive_receipts("lint", repo)
            _report(release, cli, repo, env, lint, "report-after-lint")
            doctor = _json_cli(release, cli, repo, env, ["doctor"], "doctor-installed", 0)
            release.archive_receipts("doctor", repo)
            if (doctor.get("command") != "doctor" or not doctor.get("diagnostics")
                    or any(item.get("runtime") != "unverified" for item in doctor["diagnostics"])):
                raise ReleaseFailure("installed doctor did not preserve setup/runtime distinction")
            _git(release, repo, env, ["init", "-q"], "git-init")
            _git(release, repo, env, ["add", "."], "git-add-baseline")
            _commit(release, repo, env, "baseline", "git-commit-baseline", 0)
            install_hook = release.command("hooks-install-pre-commit", [str(cli), "--root", str(repo), "hooks", "install", "--adapter", "pre-commit"], repo, env)
            if install_hook.returncode != 0:
                raise ReleaseFailure(f"pre-commit hook installation exited {install_hook.returncode}")
            valid = repo / "accepted.py"
            valid.write_text("def accepted() -> int:\n    return 1\n", encoding="utf-8")
            _git(release, repo, env, ["add", "accepted.py"], "git-add-valid")
            valid_before = _run_names(repo)
            accepted = _commit(release, repo, env, "accept valid staged source", "git-commit-valid", 0)
            if accepted.returncode != 0:
                raise ReleaseFailure("valid staged source was rejected by the installed hook")
            valid_receipt = _verify_hook_receipt(release, repo, valid_before, "commit-valid", 0)
            before_rejected = _head(release, repo, env, "git-head-before-rejected")
            broken = repo / "broken.py"
            broken.write_text("def broken(:\n", encoding="utf-8")
            _git(release, repo, env, ["add", "broken.py"], "git-add-invalid")
            invalid_before = _run_names(repo)
            rejected = _commit(release, repo, env, "reject invalid staged source", "git-commit-invalid", 1)
            invalid_receipt = _verify_hook_receipt(release, repo, invalid_before, "commit-invalid", 1, "syntax")
            after_rejected = _head(release, repo, env, "git-head-after-rejected")
            if before_rejected != after_rejected:
                raise ReleaseFailure("failed hook changed HEAD")
            broken.write_text("def broken() -> int:\n    return 2\n", encoding="utf-8")
            _git(release, repo, env, ["add", "broken.py"], "git-add-repaired")
            repaired_before = _run_names(repo)
            repaired = _commit(release, repo, env, "accept repaired staged source", "git-commit-repaired", 0)
            if repaired.returncode != 0:
                raise ReleaseFailure("repaired staged source was rejected by the installed hook")
            repaired_receipt = _verify_hook_receipt(release, repo, repaired_before, "commit-repaired", 0)
            final_head = _head(release, repo, env, "git-head-final")
            if final_head == before_rejected:
                raise ReleaseFailure("repaired commit did not advance HEAD")
            if not release.receipts:
                raise ReleaseFailure("no gate receipts were archived")
            node_integrity = _verify_node_integrity(release, cli, temporary_root, env)
            policy_authority = _verify_policy_pack_and_authority(release, cli, temporary_root, env)
            summary["checks"] = [
                {"name": "installed-cli-init", "status": "passed"},
                {"name": "installed-check-receipt-equality", "status": "passed", "exit_code": check["exit_code"]},
                {"name": "installed-lint-config-receipt-equality", "status": "passed", "exit_code": lint["exit_code"]},
                {"name": "installed-doctor-receipt-equality", "status": "passed", "exit_code": doctor["exit_code"]},
                {"name": "plugin-entry-points-verify-harness", "status": "passed"},
                {"name": "git-hook-valid-commit", "status": "passed", "exit_code": accepted.returncode,
                 "hook_slot": valid_receipt["hook_slot"]},
                {"name": "git-hook-invalid-commit", "status": "passed", "exit_code": rejected.returncode,
                 "head_unchanged": True, "hook_slot": invalid_receipt["hook_slot"], "finding": "syntax"},
                {"name": "git-hook-repaired-commit", "status": "passed", "exit_code": repaired.returncode,
                 "hook_slot": repaired_receipt["hook_slot"]},
                node_integrity,
                policy_authority,
            ]
            summary["status"] = "passed"
    except Exception as error:
        summary["status"] = "failed"
        summary["errors"].append(f"{type(error).__name__}: {error}")
        summary["errors"].extend(workspace_state.get("errors", []))
        if release is not None:
            release.save(summary)
        else:
            (artifact / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        return summary, artifact
    _apply_workspace_errors(summary, workspace_state)
    if release is None:
        raise ReleaseFailure("release runner did not initialize its evidence writer")
    release.save(summary)
    return summary, artifact


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify an exitzero wheel offline in a fresh virtualenv and Git repository")
    parser.add_argument("--wheel", required=True, type=Path, help="Path to the wheel to install")
    args = parser.parse_args(argv)
    try:
        summary, artifact = run(args.wheel)
    except Exception as error:
        print(f"release_check: ERROR ({type(error).__name__}: {error})", file=sys.stderr)
        return 1
    print(f"release_check: {summary['status'].upper()} (receipts={summary.get('receipt_count', 0)})")
    print(f"Evidence: {artifact / 'summary.json'}")
    return 0 if summary["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
