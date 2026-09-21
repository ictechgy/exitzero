"""Bounded, AST-only checks that connect an imported symbol to a use site.

The check establishes a declared static relationship between two explicit
Python files.  It does not import code, execute code, or claim runtime
reachability.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import re
from typing import Iterable

from exitzero.api import CheckSpec, Context, Finding
from exitzero.services import safe_path, select_files


MAX_SOURCE_BYTES = 2 * 1024 * 1024
_IDENTIFIER = re.compile(r"[A-Za-z_]\w*\Z")


@dataclass(frozen=True)
class Connection:
    source: str
    target: str
    symbol: str
    within: str
    usage: str
    consumer: str | None = None


@dataclass
class ModuleIndex:
    paths: dict[str, set[Path]]
    names: dict[Path, set[str]]

    def resolve(self, name: str) -> Path | None:
        candidates = self.paths.get(name, set())
        return next(iter(candidates)) if len(candidates) == 1 else None

    def is_ambiguous(self, name: str) -> bool:
        return len(self.paths.get(name, set())) > 1


@dataclass(frozen=True)
class Binding:
    """A source name and the exact target expression it denotes."""

    name: str
    expression: tuple[str, ...]
    line: int


def _dotted(value: object, *, allow_module: bool = True) -> bool:
    if not isinstance(value, str) or not value or "\x00" in value:
        return False
    if not allow_module and value == "<module>":
        return False
    return all(_IDENTIFIER.fullmatch(part) for part in value.split("."))


def _literal_path(value: object) -> bool:
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        return False
    path = PurePosixPath(value)
    return (not path.is_absolute() and ".." not in path.parts
            and not any(any(char in part for char in "*?[]") for part in path.parts)
            and value.endswith(".py"))


def _validate_options(options: object) -> tuple[list[str], list[Connection]]:
    if not isinstance(options, dict):
        raise ValueError("python.connections options must be a table")
    if set(options) - {"roots", "connections"}:
        raise ValueError("unsupported python.connections option")
    roots = options.get("roots", ["."])
    if (not isinstance(roots, list) or not roots
            or any(not isinstance(root, str) or not root or "\x00" in root
                   or "\\" in root for root in roots)):
        raise ValueError("python.connections roots must be a non-empty list of relative paths")
    for root in roots:
        path = PurePosixPath(root)
        if path.is_absolute() or ".." in path.parts or any(
                any(char in part for char in "*?[]") for part in path.parts):
            raise ValueError("python.connections roots must be literal repository paths")
    raw_connections = options.get("connections")
    if not isinstance(raw_connections, list) or not raw_connections:
        raise ValueError("python.connections requires a non-empty connections list")
    connections: list[Connection] = []
    required = {"source", "target", "symbol", "within", "usage"}
    for item in raw_connections:
        keys = set(item) if isinstance(item, dict) else set()
        if not isinstance(item, dict) or keys not in (required, required | {"consumer"}):
            raise ValueError("python.connections entries have an invalid schema")
        source, target, symbol = item.get("source"), item.get("target"), item.get("symbol")
        within, usage = item.get("within"), item.get("usage")
        if not _literal_path(source) or not _literal_path(target):
            raise ValueError("python.connections source and target must be literal .py paths")
        if not isinstance(symbol, str) or not _IDENTIFIER.fullmatch(symbol):
            raise ValueError("python.connections symbol must be a top-level Python name")
        if not (within == "<module>" or _dotted(within)):
            raise ValueError("python.connections within must be <module> or a dotted name")
        if usage not in {"call", "argument"}:
            raise ValueError("python.connections usage must be call or argument")
        consumer = item.get("consumer")
        if usage == "argument":
            if not _dotted(consumer):
                raise ValueError("python.connections argument usage requires a consumer")
        elif "consumer" in item:
            raise ValueError("python.connections call usage does not accept consumer")
        connections.append(Connection(source, target, symbol, within, usage, consumer))
    return roots, connections


def _check_paths(context: Context, roots: list[str], connections: list[Connection]) -> None:
    for root in roots:
        candidate = safe_path(context.root, root)
        if not candidate.is_dir():
            raise ValueError("python.connections root does not exist")
    for connection in connections:
        for relative in (connection.source, connection.target):
            candidate = safe_path(context.root, relative)
            if not candidate.is_file():
                raise ValueError("python.connections source and target must be files")


def connection_inputs(context: Context, spec: CheckSpec) -> list[str]:
    """Validate configuration and fingerprint explicit files plus module roots."""

    roots, connections = _validate_options(spec.options)
    _check_paths(context, roots, connections)
    patterns: list[str] = []
    for root in roots:
        pattern = "**/*.py" if root == "." else f"{PurePosixPath(root).as_posix()}/**/*.py"
        if pattern not in patterns:
            patterns.append(pattern)
    for connection in connections:
        for relative in (connection.source, connection.target):
            if relative not in patterns:
                patterns.append(relative)
    return patterns


def _module_name(relative: Path) -> str | None:
    parts = list(relative.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    if not parts or any(not _IDENTIFIER.fullmatch(part) for part in parts):
        return None
    return ".".join(parts)


def _module_index(root: Path, roots: list[str]) -> ModuleIndex:
    paths: dict[str, set[Path]] = {}
    names: dict[Path, set[str]] = {}
    for root_name in roots:
        base = safe_path(root, root_name)
        pattern = "**/*.py" if root_name == "." else f"{PurePosixPath(root_name).as_posix()}/**/*.py"
        for path in select_files(root, (pattern,)):
            try:
                relative = path.resolve().relative_to(base.resolve())
            except ValueError:
                continue
            name = _module_name(relative)
            if name is None:
                continue
            resolved = path.resolve()
            paths.setdefault(name, set()).add(resolved)
            names.setdefault(resolved, set()).add(name)
    return ModuleIndex(paths, names)


def _source_module(path: Path, index: ModuleIndex) -> str | None:
    names = index.names.get(path.resolve(), set())
    if len(names) != 1:
        return None
    name = next(iter(names))
    return f"{name}.__init__" if path.name == "__init__.py" else name


def _relative_module(node: ast.ImportFrom, current: str | None) -> str | None:
    if node.level == 0:
        return node.module or ""
    if not current:
        return None
    parts = current.split(".")
    # ``_source_module`` marks package initializers as ``pkg.__init__`` so
    # that this calculation retains the package context for relative imports.
    package = parts[:-1]
    count = node.level - 1
    if not package or count >= len(package):
        return None
    prefix = package[:len(package) - count]
    if node.module:
        prefix += node.module.split(".")
    return ".".join(prefix)


def _parse(path: Path) -> ast.Module | None:
    try:
        with path.open("rb") as stream:
            data = stream.read(MAX_SOURCE_BYTES + 1)
        if len(data) > MAX_SOURCE_BYTES:
            return None
        return ast.parse(data.decode("utf-8-sig"), filename=str(path))
    except (OSError, UnicodeError, SyntaxError, ValueError):
        return None


def _target_names(value: ast.AST) -> set[str]:
    if isinstance(value, ast.Name):
        return {value.id}
    if isinstance(value, (ast.Tuple, ast.List)):
        result: set[str] = set()
        for item in value.elts:
            result.update(_target_names(item))
        return result
    if isinstance(value, ast.Starred):
        return _target_names(value.value)
    if isinstance(value, ast.Attribute):
        root = _chain(value)
        return {root[0]} if root else set()
    return set()


def _declares_symbol(tree: ast.Module, symbol: str) -> bool:
    declarations = 0
    redefined = False
    for node, conditional in _walk_scope_context(tree.body):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name == symbol:
                declarations += 1
                redefined |= conditional
            continue
        if isinstance(node, ast.Import):
            if any((alias.asname or alias.name.split(".", 1)[0]) == symbol for alias in node.names):
                redefined = True
        elif isinstance(node, ast.ImportFrom):
            if any(alias.name != "*" and (alias.asname or alias.name) == symbol
                   for alias in node.names):
                redefined = True
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            values = node.targets if isinstance(node, ast.Assign) else (node.target,)
            redefined |= any(symbol in _target_names(value) for value in values)
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            redefined |= symbol in _target_names(node.target)
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            redefined |= any(symbol in _target_names(item.optional_vars)
                             for item in node.items if item.optional_vars is not None)
        elif isinstance(node, ast.ExceptHandler) and node.name == symbol:
            redefined = True
        elif isinstance(node, ast.NamedExpr):
            redefined |= symbol in _target_names(node.target)
        elif isinstance(node, ast.Delete):
            redefined |= any(symbol in _target_names(value) for value in node.targets)
        elif isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name == symbol:
            redefined = True
        elif isinstance(node, ast.MatchMapping) and node.rest == symbol:
            redefined = True
    return declarations == 1 and not redefined


def _chain(node: ast.AST) -> tuple[str, ...] | None:
    if isinstance(node, ast.Name):
        return (node.id,)
    if isinstance(node, ast.Attribute):
        prefix = _chain(node.value)
        return prefix + (node.attr,) if prefix else None
    return None


def _scope_chain(tree: ast.Module, within: str) -> list[ast.AST] | None:
    """Return module through the selected function, including lexical ancestors."""

    if within == "<module>":
        return [tree]
    wanted = tuple(within.split("."))
    found: list[list[ast.AST]] = []

    def visit(body: Iterable[ast.stmt], prefix: tuple[str, ...], chain: list[ast.AST]) -> None:
        for statement in body:
            if not isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            current = prefix + (statement.name,)
            candidate = chain + [statement]
            if current == wanted:
                if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    found.append(candidate)
                continue
            visit(statement.body, current, candidate)

    visit(tree.body, (), [tree])
    return found[0] if len(found) == 1 else None


def _walk_scope(statements: Iterable[ast.stmt]) -> Iterable[ast.AST]:
    """Walk one lexical scope while excluding nested function/class/lambda bodies."""

    stack = list(statements)
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        stack.extend(reversed(list(ast.iter_child_nodes(node))))


def _walk_scope_context(statements: Iterable[ast.stmt]) -> Iterable[tuple[ast.AST, bool]]:
    """Walk one scope while marking conditional and repeated regions."""

    def children(node: ast.AST, conditional: bool) -> list[tuple[ast.AST, bool]]:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            return []
        if isinstance(node, ast.If):
            return ([(node.test, conditional)]
                    + [(child, True) for child in (*node.body, *node.orelse)])
        if isinstance(node, (ast.For, ast.AsyncFor)):
            return ([(node.iter, conditional), (node.target, conditional)]
                    + [(child, True) for child in (*node.body, *node.orelse)])
        if isinstance(node, ast.While):
            return ([(node.test, conditional)]
                    + [(child, True) for child in (*node.body, *node.orelse)])
        if isinstance(node, (ast.Try, ast.TryStar)):
            return ([(child, conditional) for child in node.body]
                    + [(child, True) for handler in node.handlers for child in (handler,)]
                    + [(child, True) for child in (*node.orelse, *node.finalbody)])
        if isinstance(node, (ast.With, ast.AsyncWith)):
            items = [(child, conditional) for item in node.items
                     for child in (item.context_expr, item.optional_vars) if child is not None]
            return items + [(child, True) for child in node.body]
        if isinstance(node, ast.Match):
            return ([(node.subject, conditional)]
                    + [(case, True) for case in node.cases])
        if isinstance(node, ast.BoolOp):
            return [(value, conditional if index == 0 else True)
                    for index, value in enumerate(node.values)]
        if isinstance(node, ast.IfExp):
            return [(node.test, conditional), (node.body, True), (node.orelse, True)]
        if isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            return [(child, True) for child in ast.iter_child_nodes(node)]
        return [(child, conditional) for child in ast.iter_child_nodes(node)]

    stack = [(statement, False) for statement in reversed(list(statements))]
    while stack:
        node, conditional = stack.pop()
        yield node, conditional
        stack.extend(reversed(children(node, conditional)))


def _scope_imports(statements: Iterable[ast.stmt]) -> tuple[list[ast.AST], set[str], bool, bool]:
    imports: list[ast.AST] = []
    names: set[str] = set()
    wildcard = False
    conditional = False
    for node, is_conditional in _walk_scope_context(statements):
        if isinstance(node, ast.Import):
            imports.append(node)
            conditional |= is_conditional
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".", 1)[0])
        elif isinstance(node, ast.ImportFrom):
            imports.append(node)
            conditional |= is_conditional
            for alias in node.names:
                if alias.name == "*":
                    wildcard = True
                else:
                    names.add(alias.asname or alias.name)
    return imports, names, wildcard, conditional


def _import_counts(imports: Iterable[ast.AST]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for node in imports:
        aliases = node.names if isinstance(node, (ast.Import, ast.ImportFrom)) else ()
        for alias in aliases:
            if alias.name == "*":
                continue
            name = alias.asname or (alias.name.split(".", 1)[0]
                                    if isinstance(node, ast.Import) else alias.name)
            counts[name] = counts.get(name, 0) + 1
    return counts


def _target_bindings(imports: Iterable[ast.AST], current: str | None, index: ModuleIndex,
                     target: Path, symbol: str) -> list[Binding]:
    result: list[Binding] = []
    for node in imports:
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported = index.resolve(alias.name)
                if imported is None or imported != target.resolve():
                    continue
                if alias.asname:
                    expression = (alias.asname, symbol)
                    result.append(Binding(alias.asname, expression, node.lineno))
                else:
                    pieces = tuple(alias.name.split("."))
                    result.append(Binding(pieces[0], (pieces[0],) + pieces[1:] + (symbol,), node.lineno))
        elif isinstance(node, ast.ImportFrom):
            module = _relative_module(node, current)
            if module is None or index.is_ambiguous(module):
                continue
            module_path = index.resolve(module)
            for alias in node.names:
                if alias.name == "*":
                    continue
                local = alias.asname or alias.name
                if module_path == target.resolve() and alias.name == symbol:
                    result.append(Binding(local, (local,), node.lineno))
                child = f"{module}.{alias.name}" if module else alias.name
                if index.resolve(child) == target.resolve() and not index.is_ambiguous(child):
                    result.append(Binding(local, (local, symbol), node.lineno))
    return result


def _binding_names(statements: Iterable[ast.stmt], node: ast.AST, *, include_imports: bool = False) -> set[str]:
    rebound: set[str] = set()

    def targets(value: ast.AST) -> None:
        if isinstance(value, ast.Name):
            rebound.add(value.id)
        elif isinstance(value, ast.Attribute):
            chain = _chain(value)
            if chain:
                rebound.add(chain[0])
        elif isinstance(value, (ast.Tuple, ast.List)):
            for item in value.elts:
                targets(item)
        elif isinstance(value, ast.Starred):
            targets(value.value)

    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        args = node.args.posonlyargs + node.args.args + node.args.kwonlyargs
        rebound.update(argument.arg for argument in args)
        if node.args.vararg:
            rebound.add(node.args.vararg.arg)
        if node.args.kwarg:
            rebound.add(node.args.kwarg.arg)
    for child in _walk_scope(statements):
        if include_imports and isinstance(child, ast.Import):
            for alias in child.names:
                rebound.add(alias.asname or alias.name.split(".", 1)[0])
        elif include_imports and isinstance(child, ast.ImportFrom):
            for alias in child.names:
                if alias.name != "*":
                    rebound.add(alias.asname or alias.name)
        elif isinstance(child, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            values = child.targets if isinstance(child, ast.Assign) else (child.target,)
            for value in values:
                targets(value)
        elif isinstance(child, (ast.For, ast.AsyncFor)):
            targets(child.target)
        elif isinstance(child, ast.comprehension):
            targets(child.target)
        elif isinstance(child, (ast.With, ast.AsyncWith)):
            for item in child.items:
                if item.optional_vars:
                    targets(item.optional_vars)
        elif isinstance(child, ast.ExceptHandler) and child.name:
            rebound.add(child.name)
        elif isinstance(child, ast.NamedExpr):
            targets(child.target)
        elif isinstance(child, ast.Delete):
            for value in child.targets:
                targets(value)
        elif isinstance(child, (ast.MatchAs, ast.MatchStar)):
            if child.name:
                rebound.add(child.name)
        elif isinstance(child, ast.MatchMapping) and child.rest:
            rebound.add(child.rest)
        elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            rebound.add(child.name)
    return rebound


def _connection_finding(spec: CheckSpec, message: str, path: str, line: int | None = None) -> Finding:
    return Finding(spec.id, message, path, line)


def _check_one(context: Context, spec: CheckSpec, connection: Connection,
               index: ModuleIndex) -> Finding | None:
    source = safe_path(context.root, connection.source)
    target = safe_path(context.root, connection.target)
    target_tree = _parse(target)
    if target_tree is None:
        return _connection_finding(spec, "Connection target cannot be analyzed", connection.target)
    if not _declares_symbol(target_tree, connection.symbol):
        return _connection_finding(spec, "Connection target does not declare the requested symbol", connection.target)
    target_names = index.names.get(target.resolve(), set())
    if (len(target_names) != 1
            or any(index.is_ambiguous(name) for name in target_names)):
        return _connection_finding(spec, "Connection target module is missing or ambiguous", connection.target)
    source_tree = _parse(source)
    if source_tree is None:
        return _connection_finding(spec, "Connection source cannot be analyzed", connection.source)
    chain = _scope_chain(source_tree, connection.within)
    if chain is None:
        return _connection_finding(spec, "Connection scope is missing or ambiguous", connection.source)
    scope_node = chain[-1]
    statements = list(getattr(scope_node, "body", ()))
    source_names = index.names.get(source.resolve(), set())
    if len(source_names) > 1:
        return _connection_finding(spec, "Connection source module is ambiguous", connection.source)
    module = _source_module(source, index)
    module_imports, _, wildcard, module_conditional = _scope_imports(source_tree.body)
    selected_imports, _, selected_wildcard, selected_conditional = _scope_imports(statements)
    if wildcard or selected_wildcard:
        return _connection_finding(spec, "Connection source uses unsupported wildcard imports", connection.source)
    if module_conditional or selected_conditional:
        return _connection_finding(spec, "Connection source uses unsupported conditional imports", connection.source)
    imports = module_imports if connection.within == "<module>" else module_imports + selected_imports
    bindings = _target_bindings(imports, module, index, target, connection.symbol)
    if not bindings:
        return _connection_finding(spec, "Connection source does not import the target symbol", connection.source)
    if len({binding.name for binding in bindings}) != len(bindings):
        return _connection_finding(spec, "Connection target binding is ambiguous", connection.source)
    import_counts = _import_counts(module_imports)
    if connection.within != "<module>":
        for name, count in _import_counts(selected_imports).items():
            import_counts[name] = import_counts.get(name, 0) + count
    rebound = _binding_names(statements, scope_node)
    if connection.within != "<module>":
        rebound.update(_binding_names(source_tree.body, source_tree))
        for ancestor in chain[1:-1]:
            rebound.update(_binding_names(getattr(ancestor, "body", ()), ancestor,
                                          include_imports=True))
    valid_bindings = [binding for binding in bindings
                      if binding.name not in rebound and import_counts.get(binding.name, 0) <= 1]
    if not valid_bindings:
        return _connection_finding(spec, "Connection target binding is shadowed", connection.source,
                                   bindings[0].line)
    calls: list[ast.Call] = []
    for node, conditional in _walk_scope_context(statements):
        if conditional:
            continue
        if not isinstance(node, ast.Call):
            continue
        if connection.usage == "call" and any(_chain(node.func) == binding.expression
                                               for binding in valid_bindings):
            calls.append(node)
        elif connection.usage == "argument" and _chain(node.func) == tuple(connection.consumer.split(".")):
            values = [*node.args, *(keyword.value for keyword in node.keywords)]
            if any(_chain(value) == binding.expression
                   for value in values for binding in valid_bindings):
                calls.append(node)
    if not calls:
        return _connection_finding(spec, "Connection has no matching use in the selected scope", connection.source)
    return None


def check_connections(context: Context, spec: CheckSpec) -> list[Finding]:
    """Check each declared source-to-target connectivity relation."""

    roots, connections = _validate_options(spec.options)
    _check_paths(context, roots, connections)
    index = _module_index(context.root, roots)
    findings: list[Finding] = []
    for connection in connections:
        finding = _check_one(context, spec, connection, index)
        if finding is not None:
            findings.append(finding)
    return findings


__all__ = ["check_connections", "connection_inputs"]
