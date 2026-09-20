"""Static verification checks for exitzero.

The plugin deliberately uses only the Python standard library.  Checks return
the core ``Finding`` value and never execute repository Python code (the
``command`` check is the explicit exception, and runs only its configured
argv).
"""

from __future__ import annotations

import ast
import math
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable

from exitzero.api import CheckSpec, Context, Finding
from .node_integrity import check_integrity as check_node_integrity, integrity_inputs as node_integrity_inputs

API_VERSION = 1


def register(registry: Any) -> None:
    """Register all verification checks exposed by v1."""

    registry.add_check("python.syntax", check_syntax)
    registry.add_check("python.imports", check_imports, inputs=import_inputs)
    registry.add_check("python.test-quality", check_test_quality)
    registry.add_check("python.test-integrity", check_test_integrity)
    registry.add_check("node.test-integrity", check_node_integrity, inputs=node_integrity_inputs)
    registry.add_check("command", check_command)


def _files(context: Context, spec: CheckSpec) -> list[Path]:
    """Use core's guarded selector, with a small bootstrap fallback.

    The fallback keeps the plugin importable while the core package is being
    assembled.  Once ``exitzero.services`` is present, all path safety remains
    owned by core as specified by the plugin contract.
    """

    patterns = spec.paths
    if not isinstance(patterns, (tuple, list)) or not all(isinstance(pattern, str) and pattern for pattern in patterns):
        raise ValueError("check paths must be a non-empty sequence of non-empty strings")
    if not patterns:
        raise ValueError(f"check {spec.id!r} selected no files")
    from exitzero.services import select_files

    selected = select_files(context.root, patterns)
    if not selected and context.diff is None:
        # A full run selecting nothing means a misconfigured scope; under
        # --diff an empty selection just means every matched path is gone.
        raise ValueError(f"check {spec.id!r} selected no files")
    return [Path(path) for path in selected]


def _relative(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.name


def _parse(path: Path, root: Path, rule: str = "python.syntax") -> tuple[ast.AST | None, Finding | None]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError as exc:
        return None, Finding(
            rule,
            "Python syntax is invalid",
            _relative(root, path),
            exc.lineno,
        )
    except (OSError, UnicodeError):
        return None, Finding(
            rule,
            "Unable to read Python source",
            _relative(root, path),
            None,
        )
    return tree, None


def check_syntax(context: Context, spec: CheckSpec) -> list[Finding]:
    """Parse selected Python files without importing or executing them."""

    if not isinstance(spec.options, dict) or spec.options:
        raise ValueError("python.syntax does not accept options")
    findings: list[Finding] = []
    for path in _files(context, spec):
        _, finding = _parse(path, context.root, spec.id)
        if finding is not None:
            findings.append(finding)
    return findings


def _list_option(options: dict[str, Any], name: str, default: list[str]) -> list[str]:
    value = options.get(name, default)
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise ValueError(f"{name} must be a list of non-empty strings")
    return value


def _validate_import_options(options: dict[str, Any]) -> tuple[list[str], list[str]]:
    if not isinstance(options, dict):
        raise ValueError("python.imports options must be a table")
    unknown = set(options).difference({"roots", "allow_modules"})
    if unknown:
        raise ValueError("unsupported python.imports option")
    roots = _list_option(options, "roots", ["."])
    allow_modules = _list_option(options, "allow_modules", [])
    for root in roots:
        path = Path(root)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("roots must be relative and cannot traverse the repository")
    for module in allow_modules:
        if any(not part.isidentifier() for part in module.split(".")):
            raise ValueError("allow_modules must contain dotted module names")
    return roots, allow_modules


def _import_roots(root: Path, roots: list[str]) -> list[tuple[Path, str]]:
    from exitzero.services import safe_path

    resolved = []
    for root_name in roots:
        base = safe_path(root, root_name)
        if not base.is_dir():
            raise ValueError(f"import root does not exist: {root_name}")
        pattern = "**/*.py" if root_name == "." else f"{Path(root_name).as_posix()}/**/*.py"
        resolved.append((base, pattern))
    return resolved


def import_inputs(context: Context, spec: CheckSpec) -> list[str]:
    roots, _ = _validate_import_options(spec.options)
    return [pattern for _, pattern in _import_roots(context.root, roots)]


def _module_index(root: Path, roots: list[str]) -> dict[str, Path]:
    index: dict[str, Path] = {}
    from exitzero.services import select_files

    for base, pattern in _import_roots(root, roots):
        for path in select_files(root, (pattern,)):
            try:
                relative = path.resolve().relative_to(base.resolve())
            except ValueError:
                continue
            parts = list(relative.with_suffix("").parts)
            if parts[-1] == "__init__":
                parts.pop()
            if not parts or any(not part.isidentifier() for part in parts):
                continue
            name = ".".join(parts)
            index.setdefault(name, path)
    return index


def _module_symbols(path: Path) -> set[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, UnicodeError, SyntaxError):
        return set()
    symbols: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            symbols.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets: Iterable[ast.expr]
            if isinstance(node, ast.Assign):
                targets = node.targets
            else:
                targets = (node.target,)
            for target in targets:
                if isinstance(target, ast.Name):
                    symbols.add(target.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                symbols.add(alias.asname or alias.name.split(".")[0])
    return symbols


def _stdlib_module(name: str) -> bool:
    top = name.split(".", 1)[0]
    stdlib = getattr(sys, "stdlib_module_names", frozenset())
    return top in stdlib or top in {"__future__", "builtins"}


def _resolve_relative(node: ast.ImportFrom, current: str) -> str | None:
    current_parts = current.split(".")
    if current_parts and current_parts[-1] == "__init__":
        current_parts.pop()
    elif current_parts:
        current_parts.pop()
    if node.level < 1 or node.level - 1 > len(current_parts):
        return None
    if not current_parts:
        return None
    prefix = current_parts[: len(current_parts) - node.level + 1]
    if node.module:
        prefix.extend(node.module.split("."))
    return ".".join(prefix)


def _source_module(path: Path, resolved: dict[Path, str]) -> str | None:
    name = resolved.get(path.resolve())
    if name is None:
        return None
    return f"{name}.__init__" if path.name == "__init__.py" else name


def _attribute_chain(node: ast.Attribute | ast.Name) -> list[str] | None:
    if isinstance(node, ast.Name):
        return [node.id]
    if not isinstance(node, ast.Attribute):
        return None
    prefix = _attribute_chain(node.value) if isinstance(node.value, (ast.Attribute, ast.Name)) else None
    return prefix + [node.attr] if prefix else None


def _rebound_names(tree: ast.AST, imported: set[str]) -> set[str]:
    """Return imported names that may be rebound in any scope.

    Scope reconstruction is deliberately conservative: if an imported module
    name is reused as an argument, assignment target, or imported symbol,
    attribute checks for that name are skipped to avoid false positives.
    """

    rebound: set[str] = set()

    def add_target(target: ast.AST) -> None:
        if isinstance(target, ast.Name):
            rebound.add(target.id)
        elif isinstance(target, (ast.Tuple, ast.List)):
            for item in target.elts:
                add_target(item)
        elif isinstance(target, ast.Starred):
            add_target(target.value)

    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                add_target(target)
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            add_target(node.target)
        elif isinstance(node, (ast.NamedExpr,)):
            add_target(node.target)
        elif isinstance(node, (ast.comprehension,)):
            add_target(node.target)
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                if item.optional_vars is not None:
                    add_target(item.optional_vars)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            rebound.add(node.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name in imported:
                rebound.add(node.name)
            args = node.args.posonlyargs + node.args.args + node.args.kwonlyargs
            if node.args.vararg:
                args.append(node.args.vararg)
            if node.args.kwarg:
                args.append(node.args.kwarg)
            rebound.update(arg.arg for arg in args)
        elif isinstance(node, ast.ClassDef):
            if node.name in imported:
                rebound.add(node.name)
        elif isinstance(node, ast.Delete):
            for target in node.targets:
                add_target(target)
        elif isinstance(node, ast.ImportFrom):
            rebound.update(alias.asname or alias.name for alias in node.names if alias.name != "*")
    return rebound.intersection(imported)


_GUARD_NONE = 0
_GUARD_MODULE = 1  # catches ModuleNotFoundError only: missing module, not missing symbols
_GUARD_IMPORT = 2  # catches ImportError/bare except: missing modules and symbols

_IMPORT_ERROR_NAMES = {"ImportError": _GUARD_IMPORT, "ModuleNotFoundError": _GUARD_MODULE}


def _guard_kind(handler_type: ast.AST | None) -> int:
    """Classify one except handler: does it catch ImportError or only ModuleNotFoundError?"""

    if handler_type is None:
        return _GUARD_IMPORT
    names = handler_type.elts if isinstance(handler_type, ast.Tuple) else [handler_type]
    kind = _GUARD_NONE
    for item in names:
        if isinstance(item, ast.Name):
            name = item.id
        elif isinstance(item, ast.Attribute):
            name = item.attr
        else:
            continue
        kind = max(kind, _IMPORT_ERROR_NAMES.get(name, _GUARD_NONE))
    return kind


def _collect_guarded(statement: ast.AST, guarded: dict[int, int], kind: int) -> None:
    """Mark imports in a try body without crossing deferred function scopes."""

    stack = [statement]
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            guarded[id(node)] = max(guarded.get(id(node), _GUARD_NONE), kind)
        stack.extend(ast.iter_child_nodes(node))


def _guarded_imports(tree: ast.AST) -> dict[int, int]:
    """Import statements inside try bodies that catch import failures.

    ``try: import optional / except ImportError`` is the standard optional
    dependency pattern, so unresolved-module findings there are suppressed.
    ``except ModuleNotFoundError`` cannot catch a missing imported symbol, so
    symbol findings survive under it. Function bodies inside the try are
    deferred scopes and stay unguarded; class bodies execute immediately and
    remain guarded.
    """

    guarded: dict[int, int] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Try, ast.TryStar)):
            continue
        kind = max((_guard_kind(handler.type) for handler in node.handlers), default=_GUARD_NONE)
        if kind == _GUARD_NONE:
            continue
        for statement in node.body:
            _collect_guarded(statement, guarded, kind)
    return guarded


def check_imports(context: Context, spec: CheckSpec) -> list[Finding]:
    """Resolve local imports and local module attributes from ASTs only."""

    roots, allow_modules = _validate_import_options(spec.options)
    index = _module_index(context.root, roots)
    resolved_index: dict[Path, str] = {}
    for name, candidate in index.items():
        resolved_index.setdefault(candidate.resolve(), name)
    index_prefixes: set[str] = set()
    for name in index:
        parts = name.split(".")
        index_prefixes.update(".".join(parts[:end]) for end in range(1, len(parts)))
    allowed = set(allow_modules)
    findings: list[Finding] = []
    symbols_cache: dict[Path, set[str]] = {}

    def _symbols_of(path: Path) -> set[str]:
        # Each importing file used to re-parse the same target module; cache
        # symbol sets for the duration of this run so parsing is per-module.
        if path not in symbols_cache:
            symbols_cache[path] = _module_symbols(path)
        return symbols_cache[path]

    for path in _files(context, spec):
        tree, syntax_finding = _parse(path, context.root, spec.id)
        if syntax_finding is not None:
            findings.append(syntax_finding)
            continue
        if tree is None:
            continue
        current = _source_module(path, resolved_index)
        imported_modules: dict[str, str] = {}
        guarded = _guarded_imports(tree)
        for node in ast.walk(tree):
            guard = guarded.get(id(node), _GUARD_NONE)
            if isinstance(node, ast.Import):
                for alias in node.names:
                    module = alias.name
                    top = module.split(".", 1)[0]
                    local = module in index or module in index_prefixes
                    if not guard and not local and not _stdlib_module(module) and module not in allowed and top not in allowed:
                        findings.append(Finding(spec.id, f"Import {module!r} cannot be resolved", _relative(context.root, path), node.lineno))
                    if local:
                        imported_modules[alias.asname or top] = module if alias.asname else top
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    module = _resolve_relative(node, current or "")
                else:
                    module = node.module or ""
                if not module:
                    if not guard:
                        findings.append(Finding(spec.id, "Relative import cannot be resolved", _relative(context.root, path), node.lineno))
                    continue
                top = module.split(".", 1)[0]
                local_path = index.get(module)
                local = local_path is not None or module in index_prefixes
                if not local and not _stdlib_module(module) and module not in allowed and top not in allowed:
                    if not guard:
                        findings.append(Finding(spec.id, f"Import {module!r} cannot be resolved", _relative(context.root, path), node.lineno))
                    continue
                if guard >= _GUARD_IMPORT:
                    continue
                if local_path is not None:
                    symbols = _symbols_of(local_path)
                    for alias in node.names:
                        if alias.name == "*":
                            continue
                        if alias.name not in symbols and f"{module}.{alias.name}" not in index:
                            findings.append(Finding(spec.id, f"Imported symbol {alias.name!r} from {module!r} cannot be resolved", _relative(context.root, path), node.lineno))
                elif local:
                    # A package child may be imported even when its package
                    # __init__ does not define the child symbol.
                    for alias in node.names:
                        child = f"{module}.{alias.name}"
                        if child not in index and alias.name != "*":
                            findings.append(Finding(spec.id, f"Imported symbol {alias.name!r} from {module!r} cannot be resolved", _relative(context.root, path), node.lineno))

        shadowed = _rebound_names(tree, set(imported_modules))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute):
                continue
            chain = _attribute_chain(node)
            if not chain:
                continue
            module = imported_modules.get(chain[0])
            if not module or chain[0] in shadowed or module not in index:
                continue
            for attr in chain[1:]:
                child = f"{module}.{attr}"
                if child in index:
                    module = child
                    continue
                if attr in _symbols_of(index[module]):
                    break
                findings.append(Finding(spec.id, f"Module attribute {module}.{attr} cannot be resolved", _relative(context.root, path), node.lineno))
                break
    return findings


def _literal(node: ast.AST) -> tuple[bool, Any]:
    if isinstance(node, ast.Constant) and isinstance(node.value, (str, bytes, int, float, bool, type(None))):
        return True, node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.Not, ast.USub, ast.UAdd)):
        valid, value = _literal(node.operand)
        if not valid:
            return False, None
        if isinstance(node.op, ast.Not):
            return True, not value
        return True, -value if isinstance(node.op, ast.USub) else +value
    return False, None


def _constant_assertion(node: ast.expr) -> bool:
    valid, _ = _literal(node)
    if valid:
        return True
    if isinstance(node, ast.Compare) and len(node.ops) == 1:
        left_valid, left = _literal(node.left)
        right_valid, right = _literal(node.comparators[0])
        if left_valid and right_valid:
            op = node.ops[0]
            return isinstance(op, (ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.Is, ast.IsNot, ast.In, ast.NotIn))
    return False


def _vacuous_unittest(node: ast.Call) -> bool:
    if not isinstance(node.func, ast.Attribute) or not isinstance(node.func.value, ast.Name) or node.func.value.id != "self":
        return False
    name = node.func.attr
    if name in {"assertTrue", "assertFalse", "assertIsNone", "assertIsNotNone"} and node.args:
        valid, value = _literal(node.args[0])
        return valid and ((name == "assertTrue" and bool(value)) or (name == "assertFalse" and not bool(value)) or (name == "assertIsNone" and value is None) or (name == "assertIsNotNone" and value is not None))
    if name in {"assertEqual", "assertNotEqual", "assertIs", "assertIsNot"} and len(node.args) >= 2:
        first_valid, first = _literal(node.args[0])
        second_valid, second = _literal(node.args[1])
        if first_valid and second_valid:
            if name in {"assertEqual", "assertIs"}:
                return first == second
            return first != second
    return False


def check_test_quality(context: Context, spec: CheckSpec) -> list[Finding]:
    """Flag only obvious test-quality problems; this is not semantic proof."""

    if not isinstance(spec.options, dict) or spec.options:
        raise ValueError("python.test-quality does not accept options")
    findings: list[Finding] = []
    for path in _files(context, spec):
        tree, syntax_finding = _parse(path, context.root, spec.id)
        if syntax_finding is not None:
            findings.append(syntax_finding)
            continue
        if tree is None:
            continue
        tests: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
                tests.append(node)
        if not tests:
            findings.append(Finding(spec.id, "No test cases found", _relative(context.root, path), 1))
            continue
        for node in tests:
            body = list(node.body)
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
                body.pop(0)
            if not body or all(isinstance(item, (ast.Pass, ast.Expr)) and not (isinstance(item, ast.Expr) and not isinstance(item.value, ast.Constant)) for item in body):
                findings.append(Finding(spec.id, "Test body is empty or documentation-only", _relative(context.root, path), node.lineno))
                continue
            for item in ast.walk(node):
                if isinstance(item, ast.Assert) and _constant_assertion(item.test):
                    findings.append(Finding(spec.id, "Test assertion is obviously constant", _relative(context.root, path), item.lineno))
                elif isinstance(item, ast.Call) and _vacuous_unittest(item):
                    findings.append(Finding(spec.id, "Test assertion is obviously constant", _relative(context.root, path), item.lineno))
    return findings


_SKIP_MARKERS = {
    ("pytest", "mark", "skip"), ("pytest", "mark", "skipif"),
    ("pytest", "mark", "xfail"), ("pytest", "skip"), ("pytest", "xfail"),
    ("unittest", "skip"), ("unittest", "skipIf"), ("unittest", "skipUnless"),
    ("unittest", "expectedFailure"), ("self", "skipTest"),
}


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    """Run one bounded git read; missing tools become operational errors."""
    try:
        return subprocess.run(["git", "-C", str(root), *args],
                              capture_output=True, text=True, timeout=15, check=False)
    except FileNotFoundError:
        raise ValueError("python.test-integrity requires a git executable on PATH") from None
    except subprocess.TimeoutExpired:
        raise ValueError("git operation for python.test-integrity timed out") from None


def _name_status(output: str) -> list[tuple[str, str, str | None]]:
    """Parse NUL-separated ``git diff --name-status -z`` records."""
    fields = output.split("\0")
    entries: list[tuple[str, str, str | None]] = []
    index = 0
    while index < len(fields):
        status = fields[index]
        if not status:
            index += 1
            continue
        if status[0] in ("R", "C"):
            if index + 2 >= len(fields):
                break
            entries.append((status[0], fields[index + 1], fields[index + 2]))
            index += 3
        else:
            if index + 1 >= len(fields):
                break
            entries.append((status[0], fields[index + 1], None))
            index += 2
    return entries


def _test_stats(source: str) -> tuple[set[str], int, dict[str, int]] | None:
    """Count test cases, assertions and suppression markers in one source.

    Returns ``None`` when the source cannot be parsed so callers can decide
    whether that is an analyzable baseline or a weakened worktree file.
    Assertions are ``assert`` statements, ``self.assert*`` calls and
    ``pytest.raises`` expectations; markers are attribute chains such as
    ``pytest.mark.skip`` that neutralize a test without removing it.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    tests: set[str] = set()
    assertions = 0
    markers: dict[str, int] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
            tests.add(node.name)
        elif isinstance(node, ast.Assert):
            assertions += 1
        elif isinstance(node, ast.Call):
            chain = _attribute_chain(node.func) if isinstance(node.func, (ast.Attribute, ast.Name)) else None
            if chain and (len(chain) >= 2 and chain[0] == "self" and chain[1].startswith("assert")
                          or tuple(chain) == ("pytest", "raises")):
                assertions += 1
        elif isinstance(node, ast.Attribute):
            chain = _attribute_chain(node)
            if chain and tuple(chain) in _SKIP_MARKERS:
                key = ".".join(chain)
                markers[key] = markers.get(key, 0) + 1
    return tests, assertions, markers


def _validate_integrity_options(options: dict[str, Any]) -> tuple[str, bool, bool, int]:
    if not isinstance(options, dict):
        raise ValueError("python.test-integrity options must be a table")
    if set(options).difference({"base", "allow_deletions", "allow_skip_markers", "max_removed_assertions"}):
        raise ValueError("unsupported python.test-integrity option")
    base = options.get("base", "HEAD")
    # A leading dash would smuggle a git flag through the ref argument.
    if not isinstance(base, str) or not base.strip() or base.startswith("-"):
        raise ValueError("python.test-integrity base must be a non-empty git ref")
    deletions = options.get("allow_deletions", False)
    markers = options.get("allow_skip_markers", False)
    if not isinstance(deletions, bool) or not isinstance(markers, bool):
        raise ValueError("allow_deletions and allow_skip_markers must be booleans")
    removed = options.get("max_removed_assertions", 0)
    if isinstance(removed, bool) or not isinstance(removed, int) or removed < 0:
        raise ValueError("max_removed_assertions must be a nonnegative integer")
    return base, deletions, markers, removed


def _compare_test_file(context: Context, spec: CheckSpec, base: str, prefix: str,
                       baseline_path: str, worktree_path: str,
                       allow_skip_markers: bool, max_removed: int) -> list[Finding]:
    """Compare one baseline test file against its worktree successor."""
    from collections import Counter
    from exitzero.services import safe_path

    show = _git(context.root, "show", f"{base}:{prefix}{baseline_path}")
    if show.returncode != 0:
        return [Finding(spec.id, "Baseline test source cannot be read", baseline_path)]
    baseline_stats = _test_stats(show.stdout)
    if baseline_stats is None:
        return [Finding(spec.id, "Baseline test source cannot be analyzed", baseline_path)]
    try:
        current_text = safe_path(context.root, worktree_path).read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        current_text = None
    current_stats = _test_stats(current_text) if current_text is not None else None
    baseline_tests, baseline_assertions, baseline_markers = baseline_stats
    current_tests, current_assertions, current_markers = (
        current_stats if current_stats is not None else (set(), 0, {}))
    findings: list[Finding] = []
    removed = sorted(baseline_tests - current_tests)
    if removed:
        findings.append(Finding(spec.id, "Test case(s) removed: " + ", ".join(removed[:5])
                                + ("..." if len(removed) > 5 else ""), worktree_path))
    dropped = baseline_assertions - current_assertions
    if dropped > max_removed:
        findings.append(Finding(spec.id, f"Test assertions reduced from {baseline_assertions} to {current_assertions}",
                                worktree_path))
    if not allow_skip_markers:
        added = Counter(current_markers)
        added.subtract(Counter(baseline_markers))
        for marker, count in sorted(added.items()):
            if count > 0:
                findings.append(Finding(spec.id, f"New test suppression marker {marker} (+{count})", worktree_path))
    return findings


def check_test_integrity(context: Context, spec: CheckSpec) -> list[Finding]:
    """Detect weakened tests relative to a git baseline.

    Flags test files deleted since ``base`` (default ``HEAD``), removed test
    cases, newly introduced skip/xfail markers and net assertion loss in
    modified test files. The baseline is git state outside hashed file
    inputs, so this check declares no inputs and is never reused: unchanged
    worktree files can still drift when the base ref moves.
    """

    base, allow_deletions, allow_skip_markers, max_removed = _validate_integrity_options(spec.options)
    if _git(context.root, "rev-parse", "--verify", f"{base}^{{commit}}").returncode != 0:
        raise ValueError(f"python.test-integrity base {base!r} is not a resolvable commit")
    prefix_result = _git(context.root, "rev-parse", "--show-prefix")
    if prefix_result.returncode != 0:
        raise ValueError("python.test-integrity requires the project root inside a git worktree")
    prefix = prefix_result.stdout.strip()
    # --relative keeps diff paths root-relative even in a repository subdirectory.
    diff = _git(context.root, "diff", "--name-status", "-z", "--relative", base, "--")
    if diff.returncode != 0:
        raise ValueError("git diff for python.test-integrity failed")
    patterns = spec.paths
    if not isinstance(patterns, (tuple, list)) or not all(isinstance(item, str) and item for item in patterns):
        raise ValueError("check paths must be a non-empty sequence of non-empty strings")
    from exitzero.services import match_path

    findings: list[Finding] = []
    for status, old, new in _name_status(diff.stdout):
        if not match_path(old, patterns):
            continue
        if status == "D" or (status == "R" and (new is None or not match_path(new, patterns))):
            if not allow_deletions:
                detail = "Test file deleted relative to baseline" if status == "D" else \
                    "Test file moved out of checked scope"
                findings.append(Finding(spec.id, detail, old))
            continue
        if status in ("M", "T", "R"):
            target = new if status == "R" else old
            findings.extend(_compare_test_file(context, spec, base, prefix, old, target,
                                               allow_skip_markers, max_removed))
    return findings


def _validate_command_options(options: dict[str, Any]) -> tuple[list[str], float]:
    if not isinstance(options, dict):
        raise ValueError("command options must be a table")
    if set(options).difference({"argv", "timeout"}):
        raise ValueError("unsupported command option")
    argv = options.get("argv")
    if not isinstance(argv, list) or not argv or not all(isinstance(item, str) and item for item in argv):
        raise ValueError("command argv must be a non-empty list of strings")
    timeout = options.get("timeout", 30)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0 or timeout > 300:
        raise ValueError("command timeout must be greater than zero and at most 300 seconds")
    return [sys.executable if item == "{python}" else item for item in argv], float(timeout)


def check_command(context: Context, spec: CheckSpec) -> list[Finding]:
    """Run a configured argv with a bounded timeout and generic findings."""

    argv, timeout = _validate_command_options(spec.options)
    kwargs: dict[str, Any] = {"cwd": str(context.root), "shell": False, "stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if os.name == "posix":
        kwargs["start_new_session"] = True
    elif hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        process = subprocess.Popen(argv, **kwargs)
    except OSError:
        return [Finding(spec.id, "Configured command could not be started", category="execution")]
    try:
        process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except OSError:
            process.kill()
        process.wait()
        return [Finding(spec.id, "Configured command timed out", category="execution")]
    if process.returncode:
        return [Finding(spec.id, f"Configured command failed with exit code {process.returncode}",
                        category="execution" if process.returncode < 0 else "violation")]
    return []
