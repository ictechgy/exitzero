"""Compare explicit JS/TS test inventories with one immutable Git baseline."""
from collections import Counter
import hashlib
import os
from pathlib import Path
import re
import subprocess

from exitzero.api import CheckSpec, Context, Finding
from exitzero.services import match_path, select_files

from .javascript import AnalysisError, inventory

MAX_SOURCE_BYTES = 2 * 1024 * 1024
EXTENSIONS = {".js", ".mjs", ".cjs", ".ts", ".mts", ".cts"}


def options(spec: CheckSpec) -> tuple[str, tuple[str, ...]]:
    settings = spec.options
    if not isinstance(settings, dict) or set(settings) - {"base", "functions"}:
        raise ValueError("node.test-integrity accepts only base and functions options")
    base = settings.get("base", "HEAD")
    if not isinstance(base, str) or not base.strip() or base.startswith("-") or "\0" in base:
        raise ValueError("node.test-integrity base must be a non-empty Git ref")
    functions = settings.get("functions", ["test", "it", "describe", "suite"])
    if (not isinstance(functions, list) or not functions or len(functions) > 64
            or any(not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_$][\w$]*", name)
                   for name in functions) or len(set(functions)) != len(functions)):
        raise ValueError("node.test-integrity functions must be unique JavaScript identifier names")
    if spec.reuse:
        raise ValueError("node.test-integrity requires reuse = false because its Git baseline can move")
    return base, tuple(functions)


def integrity_inputs(context: Context, spec: CheckSpec) -> list[str]:
    options(spec)
    if not spec.paths and context.diff is None:
        raise ValueError("node.test-integrity requires explicit test paths")
    return []


def git(root: Path, *args: str) -> bytes:
    env = dict(os.environ, GIT_NO_LAZY_FETCH="1", GIT_OPTIONAL_LOCKS="0", GIT_NO_REPLACE_OBJECTS="1")
    try:
        result = subprocess.run(["git", "-C", str(root), *args], stdin=subprocess.DEVNULL,
                                capture_output=True, timeout=15, env=env, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise ValueError("node.test-integrity requires an available local Git worktree and commit") from None
    if result.returncode:
        raise ValueError("node.test-integrity could not read the local Git baseline")
    return result.stdout


def revision(root: Path, base: str) -> str:
    oid = git(root, "rev-parse", "--verify", "--end-of-options", f"{base}^{{commit}}").decode().strip()
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", oid):
        raise ValueError("Git returned an invalid baseline commit id")
    return oid


def baseline_sources(context: Context, spec: CheckSpec, oid: str) -> dict[str, bytes]:
    prefix = git(context.root, "rev-parse", "--show-prefix").decode("utf-8").rstrip("\n")
    entries = git(context.root, "ls-tree", "-r", "-l", "-z", "--full-tree", oid)
    sources = {}
    for entry in entries.split(b"\0"):
        if not entry:
            continue
        metadata, raw_path = entry.split(b"\t", 1)
        path = raw_path.decode("utf-8")
        if not path.startswith(prefix):
            continue
        relative = path[len(prefix):]
        if not match_path(relative, spec.paths):
            continue
        mode, kind, blob, size = metadata.split()
        if mode not in (b"100644", b"100755") or kind != b"blob":
            raise ValueError("Baseline test inputs must be regular files")
        if int(size) > MAX_SOURCE_BYTES:
            raise ValueError("Baseline test input exceeds the 2 MiB analysis limit")
        sources[relative] = git(context.root, "cat-file", "blob", blob.decode("ascii"))
    return sources


def check_integrity(context: Context, spec: CheckSpec) -> list[Finding]:
    integrity_inputs(context, spec)
    base, functions = options(spec)
    oid = revision(context.root, base)
    baseline = baseline_sources(context, spec, oid)
    current = {}
    for path in select_files(context.root, spec.paths):
        with path.open("rb") as handle:
            source = handle.read(MAX_SOURCE_BYTES + 1)
        if len(source) > MAX_SOURCE_BYTES:
            raise ValueError("Test input exceeds the 2 MiB analysis limit")
        current[path.relative_to(context.root).as_posix()] = source
    if not baseline and not current and context.diff is None:
        raise ValueError("node.test-integrity selected no test files")
    findings: list[Finding] = []
    inventories = []
    for label, sources in (("Baseline", baseline), ("Current", current)):
        per_file = {}
        for path, source in sorted(sources.items()):
            if Path(path).suffix not in EXTENSIONS:
                raise ValueError("node.test-integrity supports .js/.mjs/.cjs/.ts/.mts/.cts; JSX is unsupported")
            try:
                per_file[path] = inventory(source.decode("utf-8-sig"), functions)
            except UnicodeError:
                findings.append(Finding(spec.id, f"{label} test source cannot be analyzed; UTF-8 is required", path))
            except AnalysisError as error:
                # AnalysisError messages are fixed diagnostics, never source text.
                findings.append(Finding(spec.id, f"{label} test source cannot be analyzed: {error}", path))
        inventories.append(per_file)
    old_files, new_files = inventories
    old_tests = sum((counts[0] for counts in old_files.values()), Counter())
    new_tests = sum((counts[0] for counts in new_files.values()), Counter())
    missing = old_tests - new_tests
    # Counts, not sets: deleting one of two same-titled tests still changes the inventory.
    for path, (tests, _) in old_files.items():
        removed = tests & missing
        if removed:
            findings.append(Finding(spec.id, f"Recognized test/suite declaration(s) removed: {sum(removed.values())}; review title changes or scope moves", path))
            missing.subtract(removed)
    old_modes = sum((counts[1] for counts in old_files.values()), Counter())
    new_modes = sum((counts[1] for counts in new_files.values()), Counter())
    added = new_modes - old_modes
    for path, (_, modes) in new_files.items():
        introduced = modes & added
        if introduced:
            findings.append(Finding(spec.id, f"New test suppression/focus marker(s): {sum(introduced.values())}", path))
            added.subtract(introduced)
    # Allow byte-identical file renames, and splits/moves retaining all recognized declarations.
    replacements = Counter(hashlib.sha256(data).digest() for path, data in current.items() if path not in baseline)
    for path in sorted(baseline.keys() - current.keys()):
        digest = hashlib.sha256(baseline[path]).digest()
        if replacements[digest]:
            replacements[digest] -= 1
            continue
        tests = old_files.get(path, (Counter(), Counter()))[0]
        if not tests or old_tests - new_tests:
            findings.append(Finding(spec.id, "Test file deleted or moved outside the selected scope", path))
    if revision(context.root, base) != oid:
        raise ValueError("Git baseline changed while checking test integrity")
    return findings
