"""Strict policy parsing and deterministic managed documentation."""
import hashlib
import json
from pathlib import Path
import re
import tomllib

from .api import CheckSpec
from .files import safe_path, validate_relative

BEGIN = "<!-- aidd-gate:begin -->"
END = "<!-- aidd-gate:end -->"
NAME = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,99}\Z")
DEFAULT_POLICY = '''version = 1
plugins = ["aidd_gate_verify", "aidd_gate_harness"]

[[checks]]
id = "syntax"
kind = "python.syntax"
paths = ["**/*.py"]

[harness]
config_files = []
rules = []
'''


def load_policy(path: Path) -> dict:
    policy = tomllib.loads(path.read_text(encoding="utf-8"))
    if set(policy) - {"version", "plugins", "checks", "harness"}:
        raise ValueError("Unknown top-level policy key")
    if type(policy.get("version")) is not int or policy["version"] != 1:
        raise ValueError("Policy version must be 1")
    plugins = policy.get("plugins")
    if (not isinstance(plugins, list) or not plugins
            or any(not isinstance(p, str) or not NAME.fullmatch(p) for p in plugins)
            or len(set(plugins)) != len(plugins)):
        raise ValueError("plugins must be a nonempty list of unique module or entry-point names")
    checks = policy.get("checks")
    if not isinstance(checks, list) or not checks:
        raise ValueError("At least one verification check is required")
    ids: set[str] = set()
    for check in checks:
        if not isinstance(check, dict) or set(check) - {"id", "kind", "paths", "options"}:
            raise ValueError("Invalid check fields")
        for key in ("id", "kind"):
            if not isinstance(check.get(key), str) or not NAME.fullmatch(check[key]):
                raise ValueError("Each check requires valid id and kind")
        if check["id"] in ids:
            raise ValueError("Check ids must be unique")
        ids.add(check["id"])
        paths = check.get("paths", [])
        if not isinstance(paths, list):
            raise ValueError("Check paths must be an array")
        for pattern in paths:
            validate_relative(pattern)
        if not isinstance(check.get("options", {}), dict):
            raise ValueError("Check options must be a table")
    if not isinstance(policy.get("harness", {}), dict):
        raise ValueError("harness must be a table")
    return policy


def specs(policy: dict) -> list[CheckSpec]:
    return [CheckSpec(c["id"], c["kind"], tuple(c.get("paths", [])), c.get("options", {}))
            for c in policy["checks"]]


def render_agents(policy: dict) -> str:
    digest = hashlib.sha256(json.dumps(policy, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    lines = [BEGIN, "## aidd-gate policy", "", "Generated from policy. Edit the TOML, then run `aidd-gate init --sync`.",
             "Run `aidd-gate check` before merge; keep the JSON receipt as evidence.",
             "Run `aidd-gate lint-config` after changing agent configuration.", "", "Required checks:"]
    for spec in specs(policy):
        scope = ", ".join(spec.paths) or "configured command"
        lines.append(f"- `{spec.id}`: `{spec.kind}` ({scope})")
    for rule in policy.get("harness", {}).get("rules", []):
        if isinstance(rule, dict) and isinstance(rule.get("id"), str) and isinstance(rule.get("value"), str):
            lines.append(f"- Rule `{rule['id']}`: {rule['value']}")
    lines.extend(["", f"Policy SHA-256: `{digest}`", END])
    return "\n".join(lines)


def sync_agents(root: Path, policy: dict) -> None:
    path = safe_path(root, "AGENTS.md")
    current = path.read_text(encoding="utf-8") if path.exists() else ""
    section = render_agents(policy)
    if BEGIN not in current and END not in current:
        updated = current.rstrip() + ("\n\n" if current.strip() else "") + section + "\n"
    elif current.count(BEGIN) == current.count(END) == 1 and current.index(BEGIN) < current.index(END):
        start, end = current.index(BEGIN), current.index(END) + len(END)
        updated = current[:start] + section + current[end:]
    else:
        raise ValueError("AGENTS.md has ambiguous managed section markers; repair them first")
    path.write_text(updated, encoding="utf-8")
