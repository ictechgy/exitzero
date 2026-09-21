"""Repository-scoped reads that avoid credentials and generated directories."""
from __future__ import annotations

import fnmatch
import functools
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tempfile

EXCLUDED = frozenset({".git", ".venv", "venv", "node_modules", "__pycache__", ".exitzero", "build", "dist"})
SENSITIVE_DIRS = frozenset({".ssh", ".aws", ".gnupg"})
SENSITIVE_NAMES = frozenset({"auth.json", "credentials", "credentials.json", "tokens.json", "token.json", "id_rsa", "id_ed25519"})
_HASH_CHUNK = 1 << 20
_WILDCARD = re.compile(r"[*?[]")


def open_regular(path: Path, *, root: Path | None = None):
    """Open regular data without following candidate directory/leaf symlinks.

    On platforms with dir_fd, a trusted root descriptor anchors each component;
    renamed parents cannot redirect the read outside that root. Nonblocking open
    lets us reject a swapped FIFO before attempting a read.
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    directory = None
    descriptor = None
    try:
        if root is not None and os.open in os.supports_dir_fd:
            relative = path.relative_to(root)
            validate_relative(relative.as_posix())
            directory = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            for part in relative.parts[:-1]:
                child = os.open(part, flags | getattr(os, "O_DIRECTORY", 0), dir_fd=directory)
                os.close(directory)
                directory = child
            descriptor = os.open(relative.parts[-1], flags, dir_fd=directory)
        else:
            if path.is_symlink():
                raise ValueError("Symlinked inputs are not regular files")
            descriptor = os.open(path, flags)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("Input must be a regular file")
        stream = os.fdopen(descriptor, "rb")
        descriptor = None
        return stream
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if directory is not None:
            os.close(directory)


def sha256_file(path: Path, *, root: Path | None = None) -> str:
    """Hash a file in chunks so large inputs never load fully into memory."""
    digest = hashlib.sha256()
    with open_regular(path, root=root) as stream:
        for chunk in iter(lambda: stream.read(_HASH_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_atomic(path: Path, content: str) -> None:
    """Write text through a temp file plus rename.

    A rename replaces the directory entry, so a hard-linked or swapped target
    is never written through and readers never observe a partial file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".exitzero-write-")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def validate_relative(value: str) -> None:
    if (not isinstance(value, str) or not value or "\\" in value or "\x00" in value
            or PurePosixPath(value).is_absolute() or ".." in PurePosixPath(value).parts
            or ":" in value):
        raise ValueError("Expected a relative repository path without traversal")


def is_sensitive(path: Path) -> bool:
    name = path.name.lower()
    return (name == ".env" or name.startswith(".env.") or name in SENSITIVE_NAMES
            or path.suffix.lower() in {".pem", ".key", ".p12", ".pfx", ".keystore"}
            or any(part.lower() in SENSITIVE_DIRS for part in path.parts))


def safe_path(root: Path, relative: str) -> Path:
    validate_relative(relative)
    candidate = root / relative
    if is_sensitive(Path(relative)):
        raise ValueError("Credential-like paths are not valid inspection targets")
    cursor = root
    for part in Path(relative).parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise ValueError("Symlinks are not valid inspection targets")
    if not candidate.resolve().is_relative_to(root.resolve()):
        raise ValueError("Path escapes repository")
    return candidate


def _match_parts(pattern: tuple[str, ...], candidate: tuple[str, ...]) -> bool:
    """Glob match with real ``**`` (zero or more segments) semantics.

    The memo key is an (index, index) pair so repeated ``**`` segments cost
    O(len(pattern) * len(candidate)) states instead of branching per subset.
    """
    @functools.lru_cache(maxsize=None)
    def match(i: int, j: int) -> bool:
        if i == len(pattern):
            return j == len(candidate)
        if pattern[i] == "**":
            return match(i + 1, j) or (j < len(candidate) and match(i, j + 1))
        return (j < len(candidate) and fnmatch.fnmatchcase(candidate[j], pattern[i])
                and match(i + 1, j + 1))
    return match(0, 0)


def _could_match_under(pattern: tuple[str, ...], directory: tuple[str, ...]) -> bool:
    """True when some file strictly beneath ``directory`` could match ``pattern``."""
    @functools.lru_cache(maxsize=None)
    def match(i: int, j: int) -> bool:
        if j == len(directory):
            # Every non-empty remainder can match some deeper path.
            return i < len(pattern)
        if i == len(pattern):
            return False
        if pattern[i] == "**":
            return any(match(i + 1, k) for k in range(j, len(directory) + 1))
        return (fnmatch.fnmatchcase(directory[j], pattern[i])
                and match(i + 1, j + 1))
    return match(0, 0)


def _literal_prefix(parts: tuple[str, ...]) -> tuple[str, ...]:
    """Leading segments before the first wildcard; bounds where a walk starts."""
    prefix = []
    for part in parts:
        if _WILDCARD.search(part):
            break
        prefix.append(part)
    return tuple(prefix)


def match_path(relative: str, patterns: tuple[str, ...] | list[str]) -> bool:
    """Match a repository-relative path against check glob patterns.

    Complements ``select_files`` for paths that no longer exist in the
    worktree — for example deleted files reported by a VCS diff, which a
    filesystem walk can never select. Same semantics: real ``**``, excluded
    and trailing-slash segments never match, patterns are validated.
    """
    candidate = PurePosixPath(relative).parts
    if not candidate or any(part in EXCLUDED for part in candidate):
        return False
    for pattern in patterns:
        validate_relative(pattern)
        if pattern.endswith("/"):
            continue
        parts = PurePosixPath(pattern).parts
        if any(part in EXCLUDED for part in parts):
            continue
        if _match_parts(tuple(parts), tuple(candidate)):
            return True
    return False


def select_files(root: Path, patterns: tuple[str, ...] | list[str]) -> list[Path]:
    """Choose repository files matching glob patterns.

    Traversal prunes directories policy can never select (EXCLUDED and
    credential-like), so a giant ``node_modules`` costs one cheap ``readdir``
    instead of a full ``glob`` descent.  Literal patterns resolve directly.
    A symlinked directory inside the walked area fails the run rather than
    silently narrowing what the gate can see.
    """
    wildcard_parts: list[tuple[str, ...]] = []
    selected: set[Path] = set()
    for pattern in patterns:
        validate_relative(pattern)
        if is_sensitive(Path(pattern)):
            raise ValueError("Credential-like paths are not valid inspection targets")
        if pattern.endswith("/"):
            # A directory-only pattern can never select a regular file.
            continue
        parts = PurePosixPath(pattern).parts
        if any(part in EXCLUDED for part in parts):
            continue
        if any(_WILDCARD.search(part) for part in parts):
            wildcard_parts.append(tuple(parts))
            continue
        candidate = safe_path(root, pattern)
        if candidate.is_file():
            selected.add(candidate)
    if wildcard_parts:
        _walk_candidates(root, wildcard_parts, selected)
    return sorted(selected)


def _raise_walk_error(error: OSError) -> None:
    raise error


def _walk_candidates(root: Path, wildcard_parts: list[tuple[str, ...]], selected: set[Path]) -> None:
    prefixes = {_literal_prefix(parts) for parts in wildcard_parts}
    minimal = [prefix for prefix in prefixes
               if not any(len(other) < len(prefix) and prefix[: len(other)] == other
                          for other in prefixes)]
    for prefix in minimal:
        if any(part in EXCLUDED for part in prefix):
            continue
        base = safe_path(root, "/".join(prefix)) if prefix else root
        if not base.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(base, onerror=_raise_walk_error):
            current = Path(dirpath)
            keep = []
            for name in dirnames:
                relative = (current / name).relative_to(root)
                if (name in EXCLUDED or is_sensitive(relative)
                        or not any(_could_match_under(parts, relative.parts)
                                   for parts in wildcard_parts)):
                    continue
                if (current / name).is_symlink():
                    raise ValueError("Symlinked directories are not valid inspection targets")
                keep.append(name)
            dirnames[:] = keep
            for name in filenames:
                candidate = current / name
                relative = candidate.relative_to(root)
                if (any(part in EXCLUDED for part in relative.parts)
                        or is_sensitive(relative)
                        or not any(_match_parts(parts, relative.parts) for parts in wildcard_parts)):
                    continue
                safe_path(root, relative.as_posix())
                if candidate.is_file():
                    selected.add(candidate)
