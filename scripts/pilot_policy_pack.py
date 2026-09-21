#!/usr/bin/env python3
"""Run an offline ExitZero 0.6.0 policy-pack pilot on an archived export.

The input snapshot is intentionally an archive rather than a Git checkout.  It
is validated against the supplied manifest before independent case repositories
are created.  No network access, source checkout, IDE or upstream mutation is
needed by this runner.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import statistics
import subprocess
import sys
import time
import venv
import zipfile
import uuid


ROOT = Path(__file__).resolve().parents[1]
PIN = "62fc59bd5899c25e422d070915f2d2cd8d55ec51"
WHEEL_SHA256 = "8728e188cf23d1755d0be23011b4710033736e51386e29b4fa6c67458d44a1ee"
WHEEL_VERSION = "0.6.0"
AGENTS_BEGIN_MARKERS = ("<!-- exitzero:begin -->", "<!-- aidd-gate:begin -->")
ADAPTER = ROOT / "examples/riskgate/run_tests.py"
MANIFEST_SHA256 = "5c9affb95591419a998fab5384b302e18b3820b6aaf88c7472a3645ce717eaa0"
EXPECTED_SKIP_IDS = (
    "tests.test_emit.Agent2PerfettoValidatorTest.test_exported_trace_validates",
    "tests.test_prototype_parity.PrototypeParityTest.test_riskgate_never_allows_what_prototype_prompts",
    "tests.test_prototype_parity.PrototypeParityTest.test_verdict_mapping",
)
ARCHIVE_EXTRAS = frozenset({".aidd-gate/riskgate-tests.json", "_aidd_gate_pilot_tests.py", "aidd-gate.toml"})
HEX64 = re.compile(r"[0-9a-f]{64}\Z")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def path_label(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return "<external>"


def reject_symlink_chain(path: Path) -> None:
    current = path
    while True:
        if os.path.lexists(current) and current.is_symlink():
            raise ValueError("Symlink path components are not accepted")
        if current.parent == current:
            return
        current = current.parent


def require_regular(path: Path, label: str) -> None:
    reject_symlink_chain(path)
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"{label} must be a regular file")


def canonical_manifest(value: dict[str, str]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def archive_inventory(root: Path, allowed_files: set[str]) -> set[str]:
    reject_symlink_chain(root)
    if not root.is_dir() or root.is_symlink():
        raise ValueError("Snapshot must be a regular directory")
    allowed_dirs = {""}
    for name in allowed_files:
        parts = PurePosixPath(name).parts
        allowed_dirs.update("/".join(parts[:index]) for index in range(1, len(parts)))
    found: set[str] = set()
    pending = [root]
    while pending:
        directory = pending.pop()
        reject_symlink_chain(directory)
        with os.scandir(directory) as entries:
            for entry in entries:
                relative = Path(entry.path).relative_to(root).as_posix()
                if entry.is_symlink():
                    raise ValueError("Snapshot contains a symlink entry")
                if entry.is_dir(follow_symlinks=False):
                    if relative not in allowed_dirs:
                        raise ValueError("Snapshot contains an undeclared directory")
                    pending.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    if relative not in allowed_files:
                        raise ValueError("Snapshot contains an undeclared file")
                    found.add(relative)
                else:
                    raise ValueError("Snapshot contains a non-regular entry")
    return found


def archive_hash(root: Path, allowed_files: set[str]) -> str:
    rows: list[bytes] = []
    for relative in sorted(allowed_files):
        path = root / relative
        if not path.exists():
            continue
        require_regular(path, "Snapshot entry")
        rows.append(relative.encode() + b"\0FILE\0" + sha256(path.read_bytes()).encode())
    return sha256(b"\n".join(rows))


def manifest_data(path: Path) -> dict[str, str]:
    require_regular(path, "Source manifest")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not value or any(
            not isinstance(k, str) or not isinstance(v, str) for k, v in value.items()):
        raise ValueError("Source manifest must be a non-empty path/hash object")
    for name in value:
        candidate = PurePosixPath(name)
        if (candidate.is_absolute() or ".." in candidate.parts or "\\" in name
                or not name or name.startswith("./")
                or any(ord(char) < 32 or ord(char) == 127 for char in name)):
            raise ValueError(f"Unsafe source-manifest path: {name}")
        if not HEX64.fullmatch(value[name]):
            raise ValueError(f"Source manifest hash is not strict SHA-256: {name}")
    if len(value) != 56 or sha256(canonical_manifest(value)) != MANIFEST_SHA256:
        raise ValueError("Source manifest is not the reviewed 56-file manifest")
    return value


def original_bytes(path: Path, relative: str, *, expected: str) -> bytes:
    require_regular(path, "Archived source")
    raw = path.read_bytes()
    if sha256(raw) == expected:
        return raw
    # Only the exact recorded prefix may recover an appended generated section.
    if relative == "AGENTS.md":
        for marker in AGENTS_BEGIN_MARKERS:
            offset = raw.find(marker.encode())
            if offset >= 0:
                candidate = raw[:offset].rstrip() + b"\n"
                if sha256(candidate) == expected:
                    return candidate
    raise ValueError(f"Archived source hash mismatch: {relative}")


def validate_archive(snapshot: Path, manifest_path: Path) -> tuple[dict[str, str], str, str]:
    manifest = manifest_data(manifest_path)
    allowed = set(manifest) | set(ARCHIVE_EXTRAS)
    found = archive_inventory(snapshot, allowed)
    missing = set(manifest) - found
    if missing:
        raise ValueError("Archived source file is missing")
    before = archive_hash(snapshot, allowed)
    for relative, expected in manifest.items():
        original_bytes(snapshot / relative, relative, expected=expected)
    return manifest, before, archive_hash(snapshot, allowed)


def run_process(root: Path | None, argv: list[str], log: Path, env: dict[str, str], *, timeout: int = 180) -> dict:
    started = time.monotonic()
    reject_symlink_chain(log)
    log.parent.mkdir(parents=True, exist_ok=True)
    try:
        result = subprocess.run(argv, cwd=root, env=env, stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as error:
        stdout = error.stdout or ""
        stderr = error.stderr or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", errors="replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        log.write_text(stdout + stderr + "\n[timeout]\n", encoding="utf-8")
        return {"exit_code": 124, "duration_seconds": time.monotonic() - started,
                "log": path_label(log), "stdout": stdout, "stderr": stderr,
                "timed_out": True}
    except OSError:
        log.write_text("[process-start-error]\n", encoding="utf-8")
        return {"exit_code": 125, "duration_seconds": time.monotonic() - started,
                "log": path_label(log), "stdout": "", "stderr": "",
                "start_error": True}
    duration = time.monotonic() - started
    log.write_text(result.stdout + result.stderr, encoding="utf-8")
    return {"exit_code": result.returncode, "duration_seconds": duration,
            "log": path_label(log), "stdout": result.stdout,
            "stderr": result.stderr}


def git(case: Path, args: list[str], log: Path, env: dict[str, str]) -> dict:
    return run_process(case, ["git", "-c", "core.fsmonitor=false", *args], log, env)


def commit(case: Path, message: str, log: Path, env: dict[str, str]) -> str:
    result = git(case, ["-c", "commit.gpgSign=false", "commit", "-qm", message], log, env)
    if result["exit_code"]:
        raise RuntimeError(f"Git commit failed: {message}")
    head = git(case, ["rev-parse", "HEAD"], log.with_name(log.stem + "-head.log"), env)
    if head["exit_code"]:
        raise RuntimeError("Unable to resolve isolated Git commit")
    return head["stdout"].strip()


def policy_text(permissions: bool = True, *, unsupported: bool = False) -> str:
    lines = [
        "version = 1",
        'plugins = ["exitzero_verify", "exitzero_harness"]',
        "",
        "[[checks]]",
        'id = "syntax"',
        'kind = "python.syntax"',
        'paths = ["riskgate/**/*.py", "tests/**/*.py", "_exitzero_pilot_tests.py"]',
        "",
        "[[checks]]",
        'id = "imports"',
        'kind = "python.imports"',
        'paths = ["riskgate/**/*.py", "tests/**/*.py", "_exitzero_pilot_tests.py"]',
        "[checks.options]",
        'roots = ["."]',
        "allow_modules = []",
        "",
        "[[checks]]",
        'id = "test-quality"',
        'kind = "python.test-quality"',
        'paths = ["tests/test_*.py"]',
        "",
        "[[checks]]",
        'id = "regression-suite"',
        'kind = "command"',
        'paths = ["riskgate/**/*.py", "tests/**/*.py", "tests/fixtures/**/*", "riskgate.yaml", "presets/*.yaml", "_exitzero_pilot_tests.py"]',
        "[checks.options]",
        'argv = ["{python}", "_exitzero_pilot_tests.py"]',
        "timeout = 60",
        "",
        "[[checks]]",
        'id = "policy-lint"',
        'kind = "command"',
        'paths = ["riskgate/**/*.py", "riskgate.yaml"]',
        "[checks.options]",
        'argv = ["{python}", "-m", "riskgate", "lint", "riskgate.yaml"]',
        "timeout = 15",
        "",
        "[[checks]]",
        'id = "policy-cases"',
        'kind = "command"',
        'paths = ["riskgate/**/*.py", "riskgate.yaml"]',
        "[checks.options]",
        'argv = ["{python}", "-m", "riskgate", "test", "riskgate.yaml"]',
        "timeout = 15",
        "",
        "[[checks]]",
        'id = "connections"',
        'kind = "python.connections"',
        'paths = ["riskgate/cli.py", "riskgate/policy.py", "tests/test_yamlio.py", "riskgate/yamlio.py"]',
        "[checks.options]",
        'roots = ["."]',
        "[[checks.options.connections]]",
        'source = "riskgate/cli.py"',
        'target = "riskgate/policy.py"',
        'symbol = "check_policy"',
        'within = "_load_checked"',
        'usage = "call"',
        "",
        "[[checks.options.connections]]",
        'source = "tests/test_yamlio.py"',
        'target = "riskgate/yamlio.py"',
        'symbol = "parse"',
        'within = "ScalarTest.test_plain_string"',
        'usage = "call"',
    ]
    if unsupported:
        lines.extend([
            "",
            "[[checks.options.connections]]",
            'source = "tests/test_cli.py"',
            'target = "riskgate/cli.py"',
            'symbol = "main"',
            'within = "run_cli"',
            'usage = "call"',
        ])
    lines.extend(["", "[harness]", "config_files = []",
                  'rules = [{id = "pilot-contract", value = "Verify policy-pack receipts, client setup, permission verdicts, and explicit upstream skips."}]'])
    if permissions:
        lines.extend([
            "",
            "[permissions]",
            'editable = ["riskgate/**/*.py", "README.md"]',
            'protected = ["tests/**"]',
            'immutable = ["AGENTS.md", ".agents/**", ".cursor/**", "riskgate.yaml", "presets/**", "_exitzero_pilot_tests.py"]',
        ])
    lines.extend(["", "[clients]", 'adapters = ["agy", "cursor", "pre-push"]', ""])
    return "\n".join(lines)


def copy_originals(snapshot: Path, manifest: dict[str, str], destination: Path) -> None:
    reject_symlink_chain(destination)
    if destination.exists() and (not destination.is_dir() or destination.is_symlink()):
        raise ValueError("Case destination must be a regular directory")
    destination.mkdir(parents=True)
    for relative in manifest:
        source = snapshot / relative
        require_regular(source, "Archived source")
        target = destination / relative
        reject_symlink_chain(target.parent)
        target.parent.mkdir(parents=True, exist_ok=True)
        reject_symlink_chain(target)
        if os.path.lexists(target):
            raise ValueError("Case destination entry already exists")
        target.write_bytes(original_bytes(source, relative, expected=manifest[relative]))
        target.chmod(source.stat().st_mode & 0o777)
        if sha256(target.read_bytes()) != manifest[relative]:
            raise ValueError(f"Copied source hash mismatch: {relative}")


def isolated_env(venv_root: Path) -> dict[str, str]:
    keep = ("PATH", "HOME", "CODEX_HOME", "TMPDIR", "LANG", "LC_ALL", "SYSTEMROOT")
    env = {key: os.environ[key] for key in keep if key in os.environ}
    env.update({"PIP_CONFIG_FILE": os.devnull, "PIP_NO_INDEX": "1",
                "PIP_DISABLE_PIP_VERSION_CHECK": "1", "PIP_NO_INPUT": "1",
                "PYTHONNOUSERSITE": "1",
                "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_TERMINAL_PROMPT": "0", "GIT_AUTHOR_NAME": "exitzero pilot",
                "GIT_AUTHOR_EMAIL": "pilot@example.invalid",
                "GIT_COMMITTER_NAME": "exitzero pilot",
                "GIT_COMMITTER_EMAIL": "pilot@example.invalid",
                "VIRTUAL_ENV": str(venv_root)})
    env["PATH"] = str(venv_root / "bin") + os.pathsep + env.get("PATH", "")
    return env


def verify_runtime_hashes(wheel: dict[str, str], installed: dict[str, str], source: dict[str, str]) -> None:
    if len(wheel) != 25 or wheel != installed or wheel != source:
        raise RuntimeError("Source, wheel and installed runtime files must all match")


def install_wheel(wheel: Path, artifact: Path) -> tuple[Path, Path, dict]:
    venv_root = artifact / f"venv-{uuid.uuid4().hex[:10]}"
    reject_symlink_chain(artifact)
    venv.EnvBuilder(with_pip=True, clear=False, symlinks=(os.name != "nt")).create(venv_root)
    env = isolated_env(venv_root)
    python = venv_root / "bin/python"
    result = run_process(None, [str(python), "-m", "pip", "install", "--no-index", "--no-deps", str(wheel)],
                         artifact / "wheel-install.log", env, timeout=120)
    if result["exit_code"]:
        raise RuntimeError("Offline wheel installation failed")
    cli = venv_root / "bin/exitzero"
    if not cli.is_file():
        raise RuntimeError("Installed wheel did not provide the exitzero CLI")
    version = run_process(None, [str(cli), "--version"], artifact / "wheel-version.log", env)
    if version["exit_code"] or version["stdout"].strip() != WHEEL_VERSION:
        raise RuntimeError("Installed wheel version probe failed")
    with zipfile.ZipFile(wheel) as package:
        names = [name for name in package.namelist()
                 if name and not name.endswith("/") and ".dist-info/" not in name]
        wheel_runtime = {name: sha256(package.read(name)) for name in names}
    site = Path(run_process(None, [str(python), "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
                           artifact / "wheel-site.log", env)["stdout"].strip())
    installed_runtime = {}
    for name in names:
        path = site / name
        require_regular(path, "Installed wheel runtime file")
        installed_runtime[name] = sha256(path.read_bytes())
    source_runtime = {p.relative_to(src).as_posix(): sha256(p.read_bytes())
                      for src in (ROOT / "packages").glob("*/src") for p in src.rglob("*.py")}
    verify_runtime_hashes(wheel_runtime, installed_runtime, source_runtime)
    runtime_digest = sha256(canonical_manifest(wheel_runtime))
    return python, cli, {"sha256": sha256(wheel.read_bytes()), "version": version["stdout"].strip(),
                         "path": path_label(wheel), "runtime_file_count": len(installed_runtime),
                         "wheel_runtime_sha256": runtime_digest, "installed_runtime_sha256": runtime_digest,
                         "runtime_match": True, "runtime_hashes": wheel_runtime,
                         "source_matches_wheel": source_runtime == wheel_runtime,
                         "python_version": sys.version.split()[0], "platform": sys.platform}


def gate(case: Path, cli: Path, args: list[str], log: Path, env: dict[str, str]) -> dict:
    result = run_process(case, [str(cli), "--root", str(case), *args, "--format", "json"], log, env)
    if not result["stdout"].strip():
        raise RuntimeError(f"ExitZero produced no JSON for {args}; inspect {log}")
    receipt = json.loads(result["stdout"])
    relative = receipt.get("receipt")
    if not isinstance(relative, str) or Path(relative).is_absolute() or ".." in PurePosixPath(relative).parts:
        raise RuntimeError("ExitZero receipt path is unsafe")
    saved = case / relative
    require_regular(saved, "Persisted receipt")
    if (saved.suffix != ".json" or saved.stem != receipt.get("run_id")
            or relative != f".exitzero/runs/{receipt.get('run_id')}.json"
            or not re.fullmatch(r"[0-9a-f]{32}", str(receipt.get("run_id")))
            or receipt.get("receipt") != saved.relative_to(case).as_posix()
            or json.loads(saved.read_text(encoding="utf-8")) != receipt):
        raise RuntimeError("ExitZero JSON output and persisted receipt disagree")
    if result["exit_code"] != receipt.get("exit_code"):
        raise RuntimeError("ExitZero process exit and persisted receipt disagree")
    if receipt.get("tool_version") != WHEEL_VERSION or receipt.get("command") != args[0]:
        raise RuntimeError("ExitZero receipt identifies a different tool or command")
    result.update(receipt=receipt, receipt_path=relative)
    result.pop("stdout", None)
    result.pop("stderr", None)
    return result


def copy_receipt(case: Path, artifact: Path, label: str, relative: str) -> str:
    if not isinstance(relative, str) or Path(relative).is_absolute() or ".." in PurePosixPath(relative).parts:
        raise ValueError("Receipt path is unsafe")
    source = case / relative
    require_regular(source, "Persisted receipt")
    target = artifact / "receipts" / f"{label}.json"
    reject_symlink_chain(target.parent)
    target.parent.mkdir(parents=True, exist_ok=True)
    reject_symlink_chain(target)
    shutil.copy2(source, target)
    return target.relative_to(ROOT).as_posix()


def validate_pack_report(case: Path, artifact: Path, label: str, report: dict,
                         expected_base: str) -> str:
    if (report.get("applied") is not True or report.get("exit_code") != 0
            or report.get("adapters") != ["agy", "cursor", "pre-push"]):
        raise RuntimeError("Policy pack report does not describe the applied client set")
    relative = report.get("receipt")
    if (not isinstance(relative, str) or Path(relative).is_absolute()
            or ".." in PurePosixPath(relative).parts
            or PurePosixPath(relative).parts != (".exitzero", "packs", PurePosixPath(relative).name)):
        raise RuntimeError("Policy pack receipt path is unsafe")
    if not re.fullmatch(r"[0-9a-f]{32}\.json", PurePosixPath(relative).name):
        raise RuntimeError("Policy pack receipt identity is invalid")
    saved = case / relative
    require_regular(saved, "Policy pack receipt")
    if json.loads(saved.read_text(encoding="utf-8")) != report:
        raise RuntimeError("Policy pack output and persisted receipt disagree")
    paths = {change.get("path") for change in report.get("changes", [])}
    if not {"AGENTS.md", ".agents/hooks.json", ".cursor/hooks.json", ".git/hooks/pre-push", ".exitzero/hooks.json"} <= paths:
        raise RuntimeError("Policy pack report omitted a required client target")
    manifest = case / ".exitzero/hooks.json"
    require_regular(manifest, "Installed hook manifest")
    installed = json.loads(manifest.read_text(encoding="utf-8"))
    if installed.get("trust_base") != expected_base:
        raise RuntimeError("Installed hook manifest has the wrong trusted baseline")
    return copy_receipt(case, artifact, label, relative)


def make_artifact() -> Path:
    parent = ROOT / ".exitzero/pilots"
    reject_symlink_chain(parent)
    if parent.exists() and not parent.is_dir():
        raise ValueError("Pilot artifact parent must be a directory")
    parent.mkdir(parents=True, exist_ok=True)
    reject_symlink_chain(parent)
    for _attempt in range(8):
        candidate = parent / f"policy-pack-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
        if not os.path.lexists(candidate):
            candidate.mkdir()
            reject_symlink_chain(candidate)
            return candidate
    raise RuntimeError("Unable to allocate a unique pilot artifact")


def upstream(case: Path, python: Path, artifact: Path, label: str, env: dict[str, str]) -> dict[str, dict]:
    commands = {
        "tests": [str(python), "_exitzero_pilot_tests.py"],
        "policy-lint": [str(python), "-m", "riskgate", "lint", "riskgate.yaml"],
        "policy-cases": [str(python), "-m", "riskgate", "test", "riskgate.yaml"],
    }
    results = {}
    for name, argv in commands.items():
        result = run_process(case, argv, artifact / f"{label}-upstream-{name}.log", env)
        result.pop("stdout", None)
        result.pop("stderr", None)
        results[name] = result
    summary_path = case / ".exitzero/riskgate-tests.json"
    if summary_path.is_file():
        require_regular(summary_path, "Upstream test report")
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        results["tests"]["selection"] = summary
    return results


def test_selection(result: dict) -> dict:
    value = result.get("selection", {})
    skipped = value.get("skipped", [])
    return {"collected": value.get("tests_run"),
            "passed": value.get("tests_run", 0) - value.get("failures", 0) - value.get("errors", 0) - len(skipped),
            "failures": value.get("failures"), "errors": value.get("errors"),
            "skipped": len(skipped), "skip_ids": [item.get("test") for item in skipped]}


def upstream_ok(results: dict[str, dict], *, failing_tests: bool = False) -> bool:
    expected = {"tests": 1 if failing_tests else 0, "policy-lint": 0, "policy-cases": 0}
    try:
        selection = test_selection(results["tests"])
        return ({name: result.get("exit_code") for name, result in results.items()} == expected
                and selection["collected"] == 214 and selection["errors"] == 0
                and selection["failures"] == int(failing_tests)
                and selection["passed"] == 211 - int(failing_tests)
                and sorted(selection["skip_ids"]) == sorted(EXPECTED_SKIP_IDS))
    except (KeyError, TypeError, AttributeError):
        return False


def prepare_case(source_template: Path, name: str, policy: str, python: Path, cli: Path,
                 artifact: Path, env: dict[str, str], manifest: dict[str, str], *, pack: bool = True) -> tuple[Path, str, str, bytes]:
    case = artifact / name
    copy_originals(source_template, manifest, case)
    (case / "exitzero.toml").write_text(policy, encoding="utf-8")
    shutil.copy2(ADAPTER, case / "_exitzero_pilot_tests.py")
    ignore = case / ".gitignore"
    current = ignore.read_text(encoding="utf-8") if ignore.is_file() else ""
    if ".exitzero/" not in current.splitlines():
        ignore.write_text(current.rstrip() + ("\n" if current.strip() else "") + ".exitzero/\n", encoding="utf-8")
    init = git(case, ["init", "-q"], artifact / f"{name}-git-init.log", env)
    if init["exit_code"]:
        raise RuntimeError(f"Git initialization failed for {name}")
    sync = run_process(case, [str(cli), "--root", str(case), "init", "--sync"], artifact / f"{name}-sync.log", env)
    if sync["exit_code"]:
        raise RuntimeError(f"Policy sync failed for {name}")
    add = git(case, ["add", "-A"], artifact / f"{name}-git-add-a.log", env)
    if add["exit_code"]:
        raise RuntimeError(f"Git staging failed for {name}")
    base_a = commit(case, "Pinned policy baseline A", artifact / f"{name}-commit-a.log", env)
    if not pack:
        return case, base_a, base_a, b""
    applied = run_process(case, [str(cli), "--root", str(case), "policy-pack", "--apply",
                                 "--trust-base", base_a, "--format", "json"],
                          artifact / f"{name}-pack-apply-a.log", env)
    if applied["exit_code"]:
        raise RuntimeError(f"Policy pack apply failed for {name}")
    pack_report = json.loads(applied["stdout"])
    validate_pack_report(case, artifact, f"{name}-pack-apply-a", pack_report, base_a)
    hook = case / ".git/hooks/pre-push"
    hook_before = hook.read_bytes() if hook.is_file() else b""
    add_generated = git(case, ["add", "-A"], artifact / f"{name}-git-add-b.log", env)
    if add_generated["exit_code"]:
        raise RuntimeError(f"Generated files could not be staged for {name}")
    base_b = commit(case, "Generated policy pack baseline B", artifact / f"{name}-commit-b.log", env)
    repin = run_process(case, [str(cli), "--root", str(case), "policy-pack", "--apply",
                               "--trust-base", base_b, "--format", "json"],
                        artifact / f"{name}-pack-repin-b.log", env)
    if repin["exit_code"]:
        raise RuntimeError(f"Policy pack repin failed for {name}")
    repin_report = json.loads(repin["stdout"])
    validate_pack_report(case, artifact, f"{name}-pack-repin-b", repin_report, base_b)
    hook_after = hook.read_bytes() if hook.is_file() else b""
    if hook_before != hook_after:
        raise RuntimeError(f"Repinning changed installed hook bytes for {name}")
    return case, base_a, base_b, hook_before


def mutate(case: Path, name: str) -> str | None:
    if name == "baseline":
        return None
    replacements = {
        "harmless-cli-comment": ("riskgate/cli.py", "import argparse\n", "import argparse\n# Offline pilot comment.\n"),
        "real-alias-refactor": ("riskgate/cli.py", "from .policy import check_policy, load_policy", "from .policy import check_policy as validate_policy, load_policy"),
        "wrong-cmd-test-exit": ("riskgate/cli.py", "return 1 if failures else 0", "return 0  # Deliberately incorrect test-command exit status."),
        "protected-test": ("tests/test_yamlio.py", 'parse("defaults: prompt")', 'dict([("defaults", "prompt")])'),
        "policy-broadening": ("exitzero.toml", 'editable = ["riskgate/**/*.py", "README.md"]', 'editable = ["**/*"]'),
    }
    if name == "hook-tamper":
        path = case / ".agents/hooks.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["exitzero"]["Stop"][0]["command"] += " --tampered"
        path.write_text(json.dumps(data, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        return ".agents/hooks.json"
    if name == "unclassified-newfile":
        (case / "pilot-note.txt").write_text("unclassified pilot input\n", encoding="utf-8")
        return "pilot-note.txt"
    relative, old, new = replacements[name]
    path = case / relative
    text = path.read_text(encoding="utf-8")
    if text.count(old) != 1:
        raise ValueError(f"Mutation anchor is not unique: {name}")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")
    if name == "real-alias-refactor":
        path.write_text(path.read_text(encoding="utf-8").replace("check_policy(data, path)", "validate_policy(data, path)"), encoding="utf-8")
    return relative


def run(args: argparse.Namespace) -> tuple[dict, Path]:
    for supplied in (args.snapshot, args.manifest, args.wheel):
        reject_symlink_chain(supplied)
    snapshot = args.snapshot.resolve()
    manifest_path = args.manifest.resolve()
    wheel = args.wheel.resolve()
    require_regular(wheel, "Published wheel")
    manifest, archive_before, archive_after = validate_archive(snapshot, manifest_path)
    if archive_before != archive_after:
        raise RuntimeError("Archive changed during initial validation")
    if sha256(wheel.read_bytes()) != WHEEL_SHA256:
        raise ValueError("Published wheel hash does not match the pinned 0.6.0 artifact")
    artifact = make_artifact()
    summary: dict = {"schema_version": 1, "status": "failed", "source": {
        "kind": "archived-pinned-export", "commit": PIN, "manifest": path_label(manifest_path),
        "file_count": len(manifest), "archive_before_sha256": archive_before,
        "archive_validation_sha256": archive_after, "manifest_sha256": MANIFEST_SHA256}, "wheel": {}, "cases": [],
        "limitations": ["No source working checkout was available; this is the archived pinned export.",
                        "python.connections is AST-only and does not establish runtime reachability.",
                        "The run reports samples and medians only; it does not estimate a general false-positive rate.",
                        "No IDE launch or live client enforcement was attempted."],
        "artifact": artifact.relative_to(ROOT).as_posix()}
    summary["runner"] = {"sha256": sha256(Path(__file__).read_bytes()),
                          "adapter_sha256": sha256(ADAPTER.read_bytes())}
    try:
        python, cli, wheel_info = install_wheel(wheel, artifact)
        summary["wheel"] = wheel_info
        env = isolated_env(artifact / next(p.name for p in artifact.iterdir() if p.name.startswith("venv-")))
        template = artifact / "source-template"
        copy_originals(snapshot, manifest, template)
        (artifact / "source-manifest.json").write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
        base_policy = policy_text()
        summary["policy"] = {"sha256": sha256(base_policy.encode("utf-8")),
                              "permissions": True,
                              "clients": ["agy", "cursor", "pre-push"]}
        cases = ["baseline", "harmless-cli-comment", "real-alias-refactor", "wrong-cmd-test-exit",
                 "protected-test", "policy-broadening", "hook-tamper", "unclassified-newfile"]
        expected = {
            "baseline": set(), "harmless-cli-comment": set(), "real-alias-refactor": set(),
            "wrong-cmd-test-exit": {"regression-suite"}, "protected-test": {"core.permission-protected"},
            "policy-broadening": {"core.permission-immutable"}, "hook-tamper": {"core.permission-immutable"},
            "unclassified-newfile": {"core.permission-unclassified"},
        }
        expected_lint = {"policy-broadening", "hook-tamper"}
        expected_doctor_failure = {"protected-test", "policy-broadening", "hook-tamper", "unclassified-newfile"}
        for name in cases:
            case, base_a, base_b, _hook = prepare_case(template, name, base_policy, python, cli, artifact, env, manifest)
            changed = mutate(case, name)
            check = gate(case, cli, ["check", "--trust-base", base_b], artifact / f"{name}-check.log", env)
            lint = gate(case, cli, ["lint-config"], artifact / f"{name}-lint.log", env)
            doctor = gate(case, cli, ["doctor", "--trust-base", base_b], artifact / f"{name}-doctor.log", env)
            rules = {finding["rule"] for finding in check["receipt"]["findings"]}
            diagnostics = {item["target"]: item["state"] for item in doctor["receipt"].get("diagnostics", [])}
            doctor_setup = {target: diagnostics.get(target) for target in ("agy", "cursor", "pre-push", "permissions")}
            selected = upstream(case, python, artifact, name, env)
            selection = test_selection(selected["tests"])
            for label, item in ((f"{name}-check", check), (f"{name}-lint", lint), (f"{name}-doctor", doctor)):
                item["copied_receipt"] = copy_receipt(case, artifact, label, item["receipt_path"])
            valid = (check["exit_code"] == int(bool(expected[name])) and rules == expected[name]
                     and lint["exit_code"] == int(name in expected_lint)
                     and doctor["exit_code"] == int(name in expected_doctor_failure)
                     and upstream_ok(selected, failing_tests=(name == "wrong-cmd-test-exit"))
                     and (name != "baseline" or doctor_setup == {
                         "agy": "configured", "cursor": "configured",
                         "pre-push": "configured", "permissions": "configured"}))
            summary["cases"].append({"name": name, "status": "passed" if valid else "failed",
                                     "changed_file": changed, "expected_exit_code": int(bool(expected[name])),
                                     "expected_rules": sorted(expected[name]), "found_rules": sorted(rules),
                                     "base_commit_a": base_a, "base_commit_b": base_b,
                                     "check": check, "lint": lint, "doctor": doctor,
                                     "doctor_setup": doctor_setup, "upstream": selected,
                                     "test_selection": selection})
            print(f"{name}: {'PASS' if valid else 'FAIL'} (check={check['exit_code']}, findings={','.join(sorted(rules)) or 'none'})", flush=True)
            if name == "baseline" and not valid:
                raise RuntimeError("Fresh policy-pack baseline failed; mutation scores are invalid")
        # Component control: preserve the same upstream adapter and checks while
        # removing permission zones, proving protected test edits reach plugins.
        control_policy = policy_text(False)
        control, control_a, _control_b, _ = prepare_case(template, "component-control", control_policy, python, cli, artifact, env, manifest)
        mutate(control, "protected-test")
        control_check = gate(control, cli, ["check"], artifact / "component-control-check.log", env)
        control_upstream = upstream(control, python, artifact, "component-control", env)
        control_rules = {f["rule"] for f in control_check["receipt"]["findings"]}
        control_check["copied_receipt"] = copy_receipt(control, artifact, "component-control-check", control_check["receipt_path"])
        control_selection = test_selection(control_upstream["tests"])
        control_ok = (control_check["exit_code"] == 1 and control_rules == {"connections"}
                      and upstream_ok(control_upstream))
        summary["component_control"] = {"status": "passed" if control_ok else "failed", "base_commit_a": control_a,
                                         "check": control_check, "upstream": control_upstream,
                                         "test_selection": control_selection,
                                         "finding": sorted(control_rules),
                                         "purpose": "Protected test rewrite passes direct upstream tests but fails declared connectivity when permission zones are absent."}
        print(f"component-control: {'PASS' if control_ok else 'FAIL'} (connections={'connections' in control_rules})", flush=True)
        # Explicit documented scope limitation: a call inside a with body is
        # intentionally rejected by the conservative AST relation checker.
        probe_policy = policy_text(False, unsupported=True)
        probe, probe_a, _probe_b, _ = prepare_case(template, "unsupported-relation-probe", probe_policy, python, cli, artifact, env, manifest)
        probe_check = gate(probe, cli, ["check"], artifact / "unsupported-relation-probe-check.log", env)
        probe_upstream = upstream(probe, python, artifact, "unsupported-relation-probe", env)
        probe_rules = {f["rule"] for f in probe_check["receipt"]["findings"]}
        probe_check["copied_receipt"] = copy_receipt(probe, artifact, "unsupported-relation-probe-check", probe_check["receipt_path"])
        probe_selection = test_selection(probe_upstream["tests"])
        probe_ok = (probe_check["exit_code"] == 1 and probe_rules == {"connections"}
                    and upstream_ok(probe_upstream))
        summary["unsupported_relation_probe"] = {"status": "passed" if probe_ok else "failed", "base_commit_a": probe_a,
                                                  "check": probe_check, "upstream": probe_upstream,
                                                  "test_selection": probe_selection,
                                                  "finding": sorted(probe_rules),
                                                  "relation": "tests/test_cli.py run_cli -> riskgate/cli.py main inside with body",
                                                  "limitation": "The conservative connection checker marks with-body uses conditional; this relation is not certified."}
        print(f"unsupported-relation-probe: {'PASS' if probe_ok else 'FAIL'} (documented AST limitation)", flush=True)
        timings = []
        timing_evidence = []
        baseline = artifact / "baseline"
        direct_commands = {
            "tests": [str(python), "_exitzero_pilot_tests.py"],
            "policy-lint": [str(python), "-m", "riskgate", "lint", "riskgate.yaml"],
            "policy-cases": [str(python), "-m", "riskgate", "test", "riskgate.yaml"],
        }
        for index in range(3):
            pair: dict[str, float] = {}
            evidence = {}
            order = ("direct", "gate") if index % 2 == 0 else ("gate", "direct")
            for mode in order:
                if mode == "direct":
                    direct_results = [run_process(baseline, command, artifact / f"timing-{index}-{key}.log", env)
                                      for key, command in direct_commands.items()]
                    if any(result["exit_code"] for result in direct_results):
                        raise RuntimeError("Direct timing control failed")
                    pair[mode] = sum(result["duration_seconds"] for result in direct_results)
                    evidence[mode] = [{k: result[k] for k in ("exit_code", "duration_seconds", "log")}
                                      for result in direct_results]
                else:
                    result = gate(baseline, cli, ["check", "--trust-base", summary["cases"][0]["base_commit_b"]],
                                  artifact / f"timing-{index}-gate.log", env)
                    if result["exit_code"]:
                        raise RuntimeError("Baseline timing gate failed")
                    result["copied_receipt"] = copy_receipt(baseline, artifact, f"timing-{index}-gate", result["receipt_path"])
                    pair[mode] = result["duration_seconds"]
                    evidence[mode] = {k: result[k] for k in ("exit_code", "duration_seconds", "log", "copied_receipt")}
            timings.append(pair)
            timing_evidence.append(evidence)
        summary["timings"] = {"samples": timings,
                              "evidence": timing_evidence,
                              "median_seconds": {mode: statistics.median(item[mode] for item in timings)
                                                 for mode in ("direct", "gate")}}
        summary["status"] = "passed" if (
            all(item["status"] == "passed" for item in summary["cases"])
            and summary["component_control"]["status"] == "passed"
            and summary["unsupported_relation_probe"]["status"] == "passed"
        ) else "failed"
    except Exception as error:
        summary["error"] = f"Pilot did not complete ({type(error).__name__}); inspect preserved logs and cases."
    finally:
        allowed_archive = set(manifest) | set(ARCHIVE_EXTRAS)
        try:
            archive_inventory(snapshot, allowed_archive)
            summary["source_archive_after_sha256"] = archive_hash(snapshot, allowed_archive)
            summary["source_archive_preserved"] = summary["source_archive_after_sha256"] == archive_before
        except Exception:
            summary["source_archive_after_sha256"] = None
            summary["source_archive_preserved"] = False
        if not summary["source_archive_preserved"]:
            summary["status"] = "failed"
        output = artifact / "summary.json"
        output.write_text(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return summary, output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True, help="Archived pinned export directory")
    parser.add_argument("--manifest", type=Path, required=True, help="56-file source manifest JSON")
    parser.add_argument("--wheel", type=Path, required=True, help="Published ExitZero 0.6.0 wheel")
    args = parser.parse_args()
    try:
        summary, output = run(args)
    except Exception as error:
        print(f"Pilot: failed before artifact creation ({type(error).__name__})", file=sys.stderr)
        return 1
    print(f"Pilot: {summary['status']}; archive preserved: {summary['source_archive_preserved']}")
    print("Evidence: " + output.relative_to(ROOT).as_posix())
    return 0 if summary["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
