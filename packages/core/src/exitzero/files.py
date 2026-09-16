"""Repository-scoped reads that avoid credentials and generated directories."""
from pathlib import Path, PurePosixPath

EXCLUDED = frozenset({".git", ".venv", "venv", "node_modules", "__pycache__", ".exitzero", "build", "dist"})
SENSITIVE_DIRS = frozenset({".ssh", ".aws", ".gnupg"})
SENSITIVE_NAMES = frozenset({"auth.json", "credentials", "credentials.json", "tokens.json", "token.json", "id_rsa", "id_ed25519"})


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


def select_files(root: Path, patterns: tuple[str, ...] | list[str]) -> list[Path]:
    selected: set[Path] = set()
    for pattern in patterns:
        validate_relative(pattern)
        if is_sensitive(Path(pattern)):
            raise ValueError("Credential-like paths are not valid inspection targets")
        for candidate in root.glob(pattern):
            relative = candidate.relative_to(root)
            if any(part in EXCLUDED for part in relative.parts) or is_sensitive(relative):
                continue
            safe_path(root, relative.as_posix())
            if candidate.is_file():
                selected.add(candidate)
    return sorted(selected)
