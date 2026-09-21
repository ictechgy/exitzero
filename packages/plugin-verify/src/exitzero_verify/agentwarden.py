"""Optional, local AgentWarden integration.

This adapter treats AgentWarden as an external verifier.  It validates the
files that can affect the result and consumes only the small, documented JSON
verdict; skill content and upstream finding text never enter a gate finding.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import selectors
import shutil
import signal
import subprocess
import time
from typing import Any

from exitzero.api import CheckSpec, Context, Finding
from exitzero.services import is_sensitive, match_path, safe_path, select_files, validate_relative


AGENTWARDEN_VERSION = "0.3.2"
_CONFIG_FIELDS = frozenset({"argv", "config", "version", "timeout", "tool_paths"})
_REQUIRED_LOCK_FIELDS = frozenset({"name", "version", "source", "sha256", "installedAt", "verifiedScore"})
_HEX64 = re.compile(r"[0-9a-fA-F]{64}\Z")
_SHELL_CHARS = frozenset(";&|$`()<>\n\r\x00")
_PACKAGE_FIELDS = frozenset({
    "packageFormat", "packageSha256", "packageEntry", "packageFiles",
})
_MAX_OUTPUT = 1024 * 1024


class _MissingSetup(ValueError):
    """A setup dependency is absent, rather than malformed."""


def _literal(context: Context, relative: str) -> Path:
    validate_relative(relative)
    if any(char in relative for char in "*?[]") or not match_path(relative, ["**/*"]):
        raise ValueError("AgentWarden data requires literal paths outside generated directories")
    return safe_path(context.root, relative)


def _options(context: Context, spec: CheckSpec) -> dict[str, Any]:
    if spec.kind not in {"agentwarden.audit", "agentwarden.scan"}:
        raise ValueError("Unsupported AgentWarden check kind")
    if spec.reuse:
        raise ValueError("AgentWarden checks require reuse = false")
    options = spec.options
    if not isinstance(options, dict) or set(options) - _CONFIG_FIELDS:
        raise ValueError("AgentWarden options accept only argv, config, version and timeout")

    argv = options.get("argv")
    if not isinstance(argv, list) or not argv or len(argv) > 2:
        raise ValueError("AgentWarden argv must be [installed_binary] or ['node', local_cli_path]")
    if any(not isinstance(item, str) or not item or any(char in _SHELL_CHARS for char in item) for item in argv):
        raise ValueError("AgentWarden argv must contain literal executable paths")
    if len(argv) == 2 and argv[0] != "node":
        raise ValueError("AgentWarden's two-token argv must start with node")
    if len(argv) == 1 and Path(argv[0]).name.lower() in {
        "npx", "npm", "pnpm", "yarn", "bun", "deno", "curl", "wget",
        "sh", "bash", "zsh", "fish", "python", "python3",
    }:
        raise ValueError("AgentWarden argv may not invoke a package manager or shell")
    if len(argv) == 2:
        cli_path = argv[1]
        if Path(cli_path).is_absolute():
            raise ValueError("AgentWarden's local node CLI path must be repository-relative")
        validate_relative(cli_path)
        if Path(cli_path).name.startswith("-") or Path(cli_path).suffix.lower() not in {".js", ".mjs", ".cjs", ".ts", ".mts", ".cts"}:
            raise ValueError("AgentWarden's local node CLI path must be a JavaScript or TypeScript file")
        candidate = _literal(context, cli_path)
        if not candidate.is_file():
            raise _MissingSetup("AgentWarden local CLI is missing")

    config = options.get("config")
    if (not isinstance(config, str) or not config or Path(config).is_absolute()
            or Path(config).name.startswith("-")):
        raise ValueError("AgentWarden requires a repository-relative JSON config path")
    validate_relative(config)
    if Path(config).suffix.lower() != ".json":
        raise ValueError("AgentWarden config must be a JSON file")
    config_path = _literal(context, config)
    if not config_path.is_file():
        raise _MissingSetup("AgentWarden config is missing")

    version = options.get("version", AGENTWARDEN_VERSION)
    if version != AGENTWARDEN_VERSION:
        raise ValueError(f"AgentWarden version must be exactly {AGENTWARDEN_VERSION}")
    timeout = options.get("timeout", 60)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) \
            or not math.isfinite(timeout) or timeout <= 0 or timeout > 120:
        raise ValueError("AgentWarden timeout must be finite, positive and at most 120 seconds")

    tool_paths = options.get("tool_paths", [])
    if (not isinstance(tool_paths, list)
            or any(not isinstance(pattern, str) or not pattern for pattern in tool_paths)):
        raise ValueError("AgentWarden tool_paths must be a list of repository globs")
    for pattern in tool_paths:
        validate_relative(pattern)
        if not select_files(context.root, [pattern]):
            raise ValueError("AgentWarden tool_paths selected no files")

    return {"argv": tuple(argv), "config": config, "version": version, "timeout": float(timeout),
            "tool_paths": tuple(tool_paths)}


def _read_config(context: Context, config: str) -> tuple[Path, str | None]:
    path = _literal(context, config)
    try:
        document = _json_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise _MissingSetup("AgentWarden config is missing") from None
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ValueError("AgentWarden config is not valid JSON") from None
    if not isinstance(document, dict):
        raise ValueError("AgentWarden config must be a JSON object")
    if "extends" in document:
        raise ValueError("AgentWarden config inheritance via extends is unsupported")
    baseline = document.get("baseline")
    if baseline is not None and not isinstance(baseline, str):
        raise ValueError("AgentWarden config baseline must be a repository-relative path")
    if isinstance(baseline, str):
        if not baseline or Path(baseline).is_absolute():
            raise ValueError("AgentWarden config baseline must be repository-relative")
        validate_relative(baseline)
        baseline_path = _literal(context, baseline)
        if not baseline_path.is_file():
            raise _MissingSetup("AgentWarden config baseline is missing")
        return path, baseline
    return path, None


def _json_load(text: str) -> object:
    """Parse JSON while rejecting duplicate keys that could confuse policies."""

    def pairs(values: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in values:
            if key in result:
                raise ValueError("duplicate JSON member")
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise ValueError(f"invalid JSON constant: {value}")

    return json.loads(text, object_pairs_hook=pairs, parse_constant=reject_constant)


def _require_file(context: Context, relative: str, message: str) -> Path:
    validate_relative(relative)
    candidate = _literal(context, relative)
    if not candidate.is_file():
        if not candidate.exists():
            raise _MissingSetup(message)
        raise ValueError(message)
    return candidate


def _relative_join(parent: str, child: str) -> str:
    if not isinstance(child, str) or not child or "\\" in child or "\x00" in child:
        raise ValueError("AgentWarden package paths must be literal POSIX paths")
    value = PurePosixPath(child)
    if value.is_absolute() or ".." in value.parts:
        raise ValueError("AgentWarden package paths must remain inside their source directory")
    return str(PurePosixPath(parent) / value)


def _validate_package(context: Context, key: str, source: str, entry: dict[str, Any]) -> list[str]:
    present = _PACKAGE_FIELDS & set(entry)
    if not present:
        return []
    if present != _PACKAGE_FIELDS or entry.get("packageFormat") != "tar.gz":
        raise ValueError("AgentWarden lock package metadata is incomplete")
    if not isinstance(entry.get("packageSha256"), str) or not _HEX64.fullmatch(entry["packageSha256"]):
        raise ValueError("AgentWarden lock package hash is invalid")
    package_entry = entry.get("packageEntry")
    package_files = entry.get("packageFiles")
    if (not isinstance(package_entry, str) or not package_entry or "\\" in package_entry
            or Path(package_entry).is_absolute() or ".." in PurePosixPath(package_entry).parts
            or PurePosixPath(package_entry).name.lower() != "skill.md"):
        raise ValueError("AgentWarden lock package entry is invalid")
    if not isinstance(package_files, list) or not package_files or len(package_files) > 256:
        raise ValueError("AgentWarden lock package files are invalid")
    source_path = _literal(context, source)
    if not source_path.exists():
        raise _MissingSetup("AgentWarden package source is missing")
    if not source_path.is_file():
        raise ValueError("AgentWarden package source must be a regular entry file")
    package_root = PurePosixPath(source).parent.as_posix()
    for directory, directories, files in os.walk(source_path.parent, followlinks=False):
        current = Path(directory)
        for name in [*directories, *files]:
            candidate = current / name
            relative = candidate.relative_to(context.root)
            if candidate.is_symlink() or is_sensitive(relative) or not match_path(relative.as_posix(), ["**/*"]) \
                    or (not candidate.is_dir() and not candidate.is_file()):
                raise ValueError("AgentWarden package contains an unsafe filesystem entry")
        directories[:] = [name for name in directories if not (current / name).is_symlink()]
    package_entry_path = _relative_join(package_root, package_entry)
    if PurePosixPath(package_entry_path) != PurePosixPath(source):
        raise ValueError("AgentWarden package entry does not match its locked source")
    seen: set[str] = set()
    result: list[str] = []
    for item in package_files:
        if (not isinstance(item, dict) or not {"path", "sha256", "size"} <= set(item)):
            raise ValueError("AgentWarden lock package file metadata is invalid")
        path = item.get("path")
        digest = item.get("sha256")
        size = item.get("size")
        if (not isinstance(path, str) or not path or "\\" in path
                or not isinstance(digest, str) or not _HEX64.fullmatch(digest)
                or isinstance(size, bool) or not isinstance(size, int) or size < 0
                or size > 2**53 - 1):
            raise ValueError("AgentWarden lock package file metadata is invalid")
        relative = _relative_join(package_root, path)
        normalized = path.lower()
        if normalized in seen:
            raise ValueError("AgentWarden lock package files contain duplicates")
        seen.add(normalized)
        _require_file(context, relative, "AgentWarden lock package file is missing")
        result.append(relative)
    if package_entry.lower() not in seen:
        raise ValueError("AgentWarden lock package entry is not listed")
    entry_path = _relative_join(package_root, package_entry)
    if not safe_path(context.root, entry_path).is_file():
        raise ValueError("AgentWarden lock package entry is missing")
    return result


def _validate_optional_lock_fields(entry: dict[str, Any]) -> None:
    source_type = entry.get("sourceType")
    if source_type is not None and source_type not in {"local", "remote"}:
        raise ValueError("AgentWarden lock source metadata is invalid")
    remote_fields = ("remoteUrl", "resolvedUrl", "downloadSha256", "digestVerified")
    if any(field in entry for field in remote_fields) and source_type != "remote":
        raise ValueError("AgentWarden lock remote metadata is invalid")
    for field in ("remoteUrl", "resolvedUrl"):
        if field in entry and not isinstance(entry[field], str):
            raise ValueError("AgentWarden lock remote metadata is invalid")
    if "downloadSha256" in entry and (not isinstance(entry["downloadSha256"], str)
                                       or not _HEX64.fullmatch(entry["downloadSha256"])):
        raise ValueError("AgentWarden lock remote hash is invalid")
    if "digestVerified" in entry and not isinstance(entry["digestVerified"], bool):
        raise ValueError("AgentWarden lock digest metadata is invalid")
    signature_fields = {"signatureAlgorithm", "signatureVerified", "signatureKeySha256", "signatureSha256"}
    if signature_fields & set(entry):
        if entry.get("signatureAlgorithm") != "ed25519" or entry.get("signatureVerified") is not True:
            raise ValueError("AgentWarden lock signature metadata is invalid")
        for field in ("signatureKeySha256", "signatureSha256"):
            if not isinstance(entry.get(field), str) or not _HEX64.fullmatch(entry[field]):
                raise ValueError("AgentWarden lock signature metadata is invalid")


def _load_lock(context: Context) -> tuple[dict[str, Any], list[str]]:
    lock_path = safe_path(context.root, "skills.lock")
    if not lock_path.exists():
        raise _MissingSetup("AgentWarden skills.lock is missing")
    if not lock_path.is_file():
        raise ValueError("AgentWarden skills.lock must be a regular file")
    try:
        document = _json_load(lock_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ValueError("AgentWarden skills.lock is not valid JSON") from None
    if (not isinstance(document, dict) or type(document.get("lockfileVersion")) is not int
            or document.get("lockfileVersion") != 1):
        raise ValueError("AgentWarden skills.lock must declare lockfileVersion 1")
    skills = document.get("skills")
    if not isinstance(skills, dict) or not skills:
        raise ValueError("AgentWarden skills.lock must contain a nonempty skills object")
    inputs = ["skills.lock"]
    for key, raw in skills.items():
        if not isinstance(key, str) or not key or not isinstance(raw, dict):
            raise ValueError("AgentWarden skills.lock has an invalid skill entry")
        if not _REQUIRED_LOCK_FIELDS <= set(raw):
            raise ValueError("AgentWarden skills.lock skill entry is missing required fields")
        if raw.get("name") != key or not isinstance(raw.get("version"), str) \
                or not isinstance(raw.get("installedAt"), str):
            raise ValueError("AgentWarden skills.lock skill identity is invalid")
        if not isinstance(raw.get("source"), str) or not raw["source"] \
                or Path(raw["source"]).is_absolute():
            raise ValueError("AgentWarden skills.lock source must be repository-relative")
        source = raw["source"]
        validate_relative(source)
        if not isinstance(raw.get("sha256"), str) or not _HEX64.fullmatch(raw["sha256"]):
            raise ValueError("AgentWarden skills.lock skill hash is invalid")
        score = raw.get("verifiedScore")
        if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score):
            raise ValueError("AgentWarden skills.lock verified score is invalid")
        _validate_optional_lock_fields(raw)
        source_path = _literal(context, source)
        package_inputs = _validate_package(context, key, source, raw)
        if package_inputs:
            package_root = PurePosixPath(source).parent.as_posix()
            package_glob = "**/*" if package_root == "." else f"{package_root.rstrip('/')}/**/*"
            inputs.extend([source, package_glob, *package_inputs])
        else:
            if not source_path.is_file():
                raise _MissingSetup("AgentWarden locked skill source is missing")
            inputs.append(source)
    return document, list(dict.fromkeys(inputs))


def _binary_available(context: Context, argv: tuple[str, ...]) -> None:
    if len(argv) == 2:
        if shutil.which("node") is None:
            raise _MissingSetup("Node.js is not available for AgentWarden")
        return
    executable = argv[0]
    if "/" in executable or executable.startswith("."):
        candidate = Path(executable)
        if not candidate.is_absolute():
            candidate = safe_path(context.root, executable)
        if not candidate.is_file() or not os.access(candidate, os.X_OK):
            raise _MissingSetup("AgentWarden executable is missing")
    elif shutil.which(executable) is None:
        raise _MissingSetup("AgentWarden executable is not available")


def _scan_targets(context: Context, spec: CheckSpec) -> list[str]:
    if not spec.paths:
        raise ValueError("AgentWarden scan requires explicit paths")
    for pattern in spec.paths:
        validate_relative(pattern)
    files = select_files(context.root, list(spec.paths))
    if not files:
        raise ValueError("AgentWarden scan selected no files")
    return [path.relative_to(context.root).as_posix() for path in files]


def _validated(context: Context, spec: CheckSpec) -> tuple[dict[str, Any], Path, list[str]]:
    options = _options(context, spec)
    config_path, baseline = _read_config(context, options["config"])
    inputs = [options["config"]]
    if len(options["argv"]) == 2:
        inputs.append(options["argv"][1])
    inputs.extend(options["tool_paths"])
    if baseline:
        inputs.append(baseline)
    if spec.kind == "agentwarden.audit":
        _, lock_inputs = _load_lock(context)
        inputs.extend(lock_inputs)
    else:
        inputs.extend(_scan_targets(context, spec))
    _binary_available(context, options["argv"])
    return options, config_path, list(dict.fromkeys(inputs))


def agentwarden_inputs(context: Context, spec: CheckSpec) -> list[str]:
    """Validate setup and return every repository file affecting the verdict."""

    try:
        _, _, inputs = _validated(context, spec)
        return inputs
    except (OSError, ValueError):
        if getattr(context, "command", None) != "doctor":
            raise
        return _doctor_inputs(context, spec)


def _doctor_inputs(context: Context, spec: CheckSpec) -> list[str]:
    """Return only guarded literals so doctor can report setup failures."""

    result: list[str] = []

    def add(value: object, *, literal: bool = True) -> None:
        if not isinstance(value, str) or not value or "\x00" in value:
            return
        try:
            validate_relative(value)
            if literal:
                _literal(context, value)
            else:
                safe_path(context.root, value)
        except (OSError, ValueError):
            return
        if value not in result:
            result.append(value)

    options = spec.options if isinstance(spec.options, dict) else {}
    add(options.get("config"))
    argv = options.get("argv")
    if isinstance(argv, list) and len(argv) == 2 and argv[0] == "node":
        add(argv[1])
    tool_paths = options.get("tool_paths")
    if isinstance(tool_paths, list):
        for pattern in tool_paths:
            add(pattern, literal=False)
    if spec.kind == "agentwarden.audit":
        add("skills.lock")
    else:
        for pattern in spec.paths:
            add(pattern, literal=False)
    return result


def _terminate(process: subprocess.Popen) -> None:
    try:
        if os.name == "posix" and process.pid:
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except OSError:
        pass


def _run(argv: tuple[str, ...], args: list[str], root: Path, timeout: float) -> tuple[int, bytes, bytes]:
    if os.name != "posix":
        raise ValueError("AgentWarden execution currently requires POSIX pipe controls")
    try:
        process = subprocess.Popen([*argv, *args], cwd=str(root), shell=False, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   start_new_session=os.name == "posix")
    except OSError:
        raise ValueError("AgentWarden executable could not be started") from None
    streams = {stream.fileno(): (name, stream) for name, stream in
               (("stdout", process.stdout), ("stderr", process.stderr)) if stream is not None}
    all_streams = dict(streams)
    captured = {"stdout": bytearray(), "stderr": bytearray()}
    totals = {"stdout": 0, "stderr": 0}
    selector = selectors.DefaultSelector()
    deadline = time.monotonic() + timeout
    timed_out = False
    output_overflow = False
    try:
        for fd, (name, _) in streams.items():
            os.set_blocking(fd, False)
            selector.register(fd, selectors.EVENT_READ, name)
        while streams:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                _terminate(process)
                break
            events = selector.select(min(remaining, 0.05))
            if not events:
                continue
            for event, _ in events:
                fd = event.fd
                name = event.data
                try:
                    chunk = os.read(fd, 65536)
                except BlockingIOError:
                    continue
                except OSError:
                    chunk = b""
                if not chunk:
                    try:
                        selector.unregister(fd)
                    except (KeyError, ValueError):
                        pass
                    streams.pop(fd, None)
                    continue
                totals[name] += len(chunk)
                if len(captured[name]) < _MAX_OUTPUT:
                    captured[name].extend(chunk[:_MAX_OUTPUT - len(captured[name])])
                if totals[name] > _MAX_OUTPUT:
                    output_overflow = True
                    _terminate(process)
                    streams.clear()
                    break
    finally:
        selector.close()
        if timed_out or output_overflow:
            _terminate(process)
        if process.poll() is None:
            try:
                process.wait(timeout=max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                _terminate(process)
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    pass
        for _, (_, stream) in all_streams.items():
            try:
                stream.close()
            except OSError:
                pass
    if timed_out:
        raise ValueError("AgentWarden command timed out") from None
    if output_overflow:
        raise ValueError("AgentWarden command output exceeded the safety limit") from None
    return process.returncode, bytes(captured["stdout"]), bytes(captured["stderr"])


def _version_check(options: dict[str, Any], root: Path) -> None:
    code, stdout, _ = _run(options["argv"], ["--version"], root, options["timeout"])
    if code != 0:
        raise ValueError("AgentWarden version check failed")
    try:
        text = stdout.decode("utf-8").strip()
    except UnicodeDecodeError:
        raise ValueError("AgentWarden version output was malformed") from None
    if text != f"agentwarden v{AGENTWARDEN_VERSION}":
        raise ValueError("AgentWarden executable reported an unexpected version")


def _report_path(root: Path, value: object) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError("AgentWarden scan report has an invalid file path")
    candidate = Path(value)
    try:
        resolved = candidate.resolve() if candidate.is_absolute() else (root / value).resolve()
        relative = resolved.relative_to(root.resolve()).as_posix()
    except (OSError, ValueError):
        raise ValueError("AgentWarden scan report path escaped the repository") from None
    return relative


def _scan_results(context: Context, payload: object, expected: list[str]) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        raise ValueError("AgentWarden scan JSON was malformed")
    if "results" in payload:
        if (set(payload) - {"totalScanned", "passedCount", "failedCount", "results"}
                or type(payload.get("totalScanned")) is not int
                or type(payload.get("passedCount")) is not int
                or type(payload.get("failedCount")) is not int
                or not isinstance(payload.get("results"), list)):
            raise ValueError("AgentWarden scan JSON was malformed")
        results = payload["results"]
        if (payload["totalScanned"] != len(results)
                or payload["passedCount"] + payload["failedCount"] != len(results)):
            raise ValueError("AgentWarden scan JSON counts were inconsistent")
    else:
        results = [payload]
    if len(results) != len(expected):
        raise ValueError("AgentWarden scan JSON did not cover the selected files")
    expected_set = set(expected)
    seen: set[str] = set()
    for result in results:
        if not isinstance(result, dict) or not isinstance(result.get("passed"), bool) \
                or not isinstance(result.get("findings"), list):
            raise ValueError("AgentWarden scan JSON result was malformed")
        path = _report_path(context.root, result.get("filePath"))
        if path not in expected_set or path in seen:
            raise ValueError("AgentWarden scan JSON covered an unexpected file")
        seen.add(path)
    if seen != expected_set:
        raise ValueError("AgentWarden scan JSON file membership was inconsistent")
    if "results" in payload:
        passed = sum(bool(result["passed"]) for result in results)
        if payload["passedCount"] != passed or payload["failedCount"] != len(results) - passed:
            raise ValueError("AgentWarden scan JSON verdict counts were inconsistent")
    return results


def _audit_result(payload: object, lock: dict[str, Any]) -> bool:
    if not isinstance(payload, dict) or set(payload) - {"auditedAt", "passed", "skills"} \
            or not isinstance(payload.get("passed"), bool) or not isinstance(payload.get("skills"), dict):
        raise ValueError("AgentWarden audit JSON was malformed")
    skills = lock["skills"]
    reported = payload["skills"]
    if set(reported) != set(skills):
        raise ValueError("AgentWarden audit JSON skill membership was inconsistent")
    all_passed = True
    for name, locked in skills.items():
        item = reported[name]
        if not isinstance(item, dict):
            raise ValueError("AgentWarden audit JSON skill entry was malformed")
        required = ("exists", "hashMatch", "policyPassed", "publisherPolicyPassed", "source", "sha256")
        if (any(not isinstance(item.get(field), bool) for field in required[:4])
                or item.get("version") != locked["version"]
                or item.get("source") != locked["source"] or item.get("sha256") != locked["sha256"]):
            raise ValueError("AgentWarden audit JSON did not match skills.lock")
        if "packageMatch" in item and item["packageMatch"] is not None and not isinstance(item["packageMatch"], bool):
            raise ValueError("AgentWarden audit JSON package result was malformed")
        if "packageFormat" in locked and (not isinstance(item.get("packageMatch"), bool)
                                           or item["packageMatch"] != item["hashMatch"]):
            raise ValueError("AgentWarden audit package verdict was inconsistent")
        package_locked = _PACKAGE_FIELDS <= set(locked)
        package_match = item.get("packageMatch")
        if package_locked and not isinstance(package_match, bool):
            raise ValueError("AgentWarden audit JSON package result was malformed")
        if not package_locked and package_match is not None:
            raise ValueError("AgentWarden audit JSON package result was malformed")
        current = all(item[field] for field in required[:4])
        all_passed = all_passed and current
    if payload["passed"] != all_passed:
        raise ValueError("AgentWarden audit JSON verdict was inconsistent")
    return all_passed


def check_agentwarden(context: Context, spec: CheckSpec) -> list[Finding]:
    """Run the pinned local AgentWarden command and return only a generic verdict."""

    options, _, _ = _validated(context, spec)
    _version_check(options, context.root)
    if spec.kind == "agentwarden.audit":
        command = ["audit", "--json", "--config", options["config"]]
    else:
        command = ["scan", "--json", "--config", options["config"], "--", *_scan_targets(context, spec)]
    code, stdout, _ = _run(options["argv"], command, context.root, options["timeout"])
    try:
        payload = _json_load(stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise ValueError("AgentWarden returned malformed JSON") from None
    if spec.kind == "agentwarden.audit":
        lock, _ = _load_lock(context)
        passed = _audit_result(payload, lock)
        if code != int(not passed):
            raise ValueError("AgentWarden audit exit code was inconsistent")
    else:
        results = _scan_results(context, payload, _scan_targets(context, spec))
        passed = all(result["passed"] for result in results)
        if code != int(not passed):
            raise ValueError("AgentWarden scan exit code was inconsistent")
    if not passed:
        return [Finding(spec.id, "AgentWarden reported a security or integrity risk.")]
    return []


def inspect_setup(context: Context, spec: CheckSpec) -> tuple[str, str, str]:
    """Inspect local setup without invoking AgentWarden."""

    if os.name != "posix":
        return ("unknown", "AgentWarden execution currently requires POSIX pipe controls.",
                "Use a supported macOS or Linux verification environment.")
    try:
        _validated(context, spec)
    except _MissingSetup as error:
        return ("missing", str(error), "Install/configure AgentWarden locally, then rerun doctor.")
    except (OSError, ValueError):
        return ("misconfigured", "AgentWarden configuration or local inputs are invalid.",
                "Review the AgentWarden check options and local files, then rerun doctor.")
    return ("configured", "AgentWarden is configured; runtime version and scan results remain unverified.",
            "Run exitzero check to execute the pinned local AgentWarden verifier.")
