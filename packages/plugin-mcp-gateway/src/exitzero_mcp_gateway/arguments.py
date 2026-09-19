"""Opt-in constraints for known tools with literal top-level arguments."""
from pathlib import Path
from urllib.parse import urlsplit

from exitzero.services import safe_path


def _origin(value: str) -> tuple[str, str, int]:
    if not isinstance(value, str) or not value or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value) or "\\" in value:
        raise ValueError("Invalid HTTPS URL")
    parts = urlsplit(value)
    host = parts.hostname
    if (parts.scheme != "https" or not host or not host.isascii() or parts.username is not None
            or parts.password is not None or "%" in host):
        raise ValueError("Invalid HTTPS origin")
    return "https", host.lower(), 443 if parts.port is None else parts.port


def load_rules(values: object, cwd: Path) -> dict:
    if not isinstance(values, list):
        raise ValueError("argument_rules must be an array of tables")
    rules = {}
    for rule in values:
        if not isinstance(rule, dict) or set(rule) - {"tool", "keys", "paths", "origins", "enums"}:
            raise ValueError("Invalid argument rule fields")
        tool = rule.get("tool")
        if not isinstance(tool, str) or not tool or any(c in tool for c in "*?[]") or tool in rules:
            raise ValueError("Argument rule tools must be unique exact names")
        keys = rule.get("keys")
        if (not isinstance(keys, list) or any(not isinstance(key, str) or not key for key in keys)
                or len(set(keys)) != len(keys)):
            raise ValueError("Argument rule keys must be unique strings")
        normalized = {"keys": keys}
        for kind in ("paths", "origins", "enums"):
            constraints = rule.get(kind, {})
            if not isinstance(constraints, dict) or any(key not in keys for key in constraints):
                raise ValueError("Argument constraints must reference declared keys")
            normalized[kind] = {}
            for key, allowed in constraints.items():
                if not isinstance(allowed, list) or not allowed or any(not isinstance(value, str) or not value for value in allowed):
                    raise ValueError("Argument constraints need a nonempty string list")
                if kind == "paths":
                    for root in allowed:
                        safe_path(cwd, root)
                if kind == "origins":
                    origins = []
                    for value in allowed:
                        origin = _origin(value)
                        parts = urlsplit(value)
                        if parts.path not in ("", "/") or parts.query or parts.fragment:
                            raise ValueError("Configure HTTPS origins without paths, queries or fragments")
                        origins.append(origin)
                    normalized[kind][key] = origins
                else:
                    normalized[kind][key] = allowed
        rules[tool] = normalized
    return rules


def allowed_arguments(config: dict, tool: str, arguments: object) -> bool:
    rule = config.get("argument_rules", {}).get(tool)
    if rule is None:
        return True
    if not isinstance(arguments, dict) or any(key not in rule["keys"] for key in arguments):
        return False
    try:
        cwd = Path(config["cwd"])
        for kind in ("paths", "origins", "enums"):
            for key, allowed in rule[kind].items():
                value = arguments.get(key)
                if not isinstance(value, str) or not value:
                    return False
                if kind == "enums" and value not in allowed:
                    return False
                if kind == "origins" and _origin(value) not in allowed:
                    return False
                if kind == "paths":
                    # For tools that consume literal paths, never shell expansion.
                    if any(not (c.isalnum() or c in "_-. /:") for c in value) or "~" in value:
                        return False
                    path = Path(value)
                    relative = path.relative_to(cwd).as_posix() if path.is_absolute() else value
                    target = safe_path(cwd, relative)
                    if not any(target.is_relative_to(safe_path(cwd, root)) for root in allowed):
                        return False
        return True
    except (OSError, ValueError, UnicodeError):
        return False
