"""IDE adapters delegate to the shared runner and preserve existing hooks."""
import hashlib
import json
from pathlib import Path
import shlex
import subprocess
import sys

from .api import Context, Finding
from .files import safe_path

CURSOR_EVENTS = {"beforeShellExecution": "PreToolUse", "beforeMCPExecution": "PreToolUse", "afterFileEdit": "PostToolUse", "stop": "PostToolUse"}


def _launcher() -> list[str]:
    checkout = Path(__file__).resolve().parents[4] / "bin" / "aidd-gate"
    return [sys.executable, str(checkout)] if checkout.is_file() else [sys.executable, "-m", "aidd_gate"]


def expected_cursor_command(root: Path, policy_path: Path, slot: str = "PostToolUse") -> str:
    event = "stop" if slot == "PostToolUse" else "beforeShellExecution"
    return shlex.join([*_launcher(), "--root", str(root), "--policy", str(policy_path.relative_to(root)),
                       "hooks", "run", "--adapter", "cursor", "--event", event])


def _manifest(root: Path) -> dict:
    path = safe_path(root, ".aidd-gate/hooks.json")
    if path.exists():
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("version") != 1 or not isinstance(value.get("files"), dict):
            raise ValueError("Invalid hook installation manifest")
        return value
    return {"version": 1, "files": {}}


def install(root: Path, policy_path: Path, adapter: str) -> str:
    manifest = _manifest(root)
    if adapter == "cursor":
        relative = ".cursor/hooks.json"
        path = safe_path(root, relative)
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"version": 1, "hooks": {}}
        if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("hooks"), dict):
            raise ValueError("Invalid existing Cursor hook configuration")
        hooks = data["hooks"].setdefault("stop", [])
        if not isinstance(hooks, list) or any(not isinstance(h, dict) or not isinstance(h.get("command"), str) for h in hooks):
            raise ValueError("Invalid existing Cursor stop hooks")
        command = expected_cursor_command(root, policy_path)
        if not any(h["command"] == command for h in hooks):
            hooks.append({"command": command})
        content = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    else:
        # Git executes hooks relative to the worktree root. Respect custom hook paths.
        result = subprocess.run(["git", "-C", str(root), "rev-parse", "--git-path", "hooks/pre-commit"],
                                capture_output=True, text=True, check=False)
        if result.returncode != 0:
            raise ValueError("pre-commit adapter requires a Git repository")
        candidate = Path(result.stdout.strip())
        candidate = candidate if candidate.is_absolute() else root / candidate
        try:
            relative = candidate.relative_to(root).as_posix()
        except ValueError:
            raise ValueError("External Git hook directories require manual CLI installation") from None
        path = safe_path(root, relative)
        command = shlex.join([*_launcher(), "--root", str(root), "--policy", policy_path.relative_to(root).as_posix(),
                              "hooks", "run", "--slot", "pre-commit"])
        content = "#!/bin/sh\n# aidd-gate managed hook\nexec " + command + "\n"
        if path.exists() and path.read_text(encoding="utf-8") != content:
            raise ValueError("Existing pre-commit hook preserved; chain the CLI manually")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    if adapter == "pre-commit":
        path.chmod(path.stat().st_mode | 0o111)
    manifest["files"][relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    target = safe_path(root, ".aidd-gate/hooks.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return relative


def lint_installed(context: Context) -> list[Finding]:
    manifest = _manifest(context.root)
    findings = []
    for relative, digest in manifest["files"].items():
        path = safe_path(context.root, relative)
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            findings.append(Finding("harness.hooks-drift", "Installed hook changed or disappeared; review and reinstall it.", relative))
    return findings


def cursor_response(receipt: dict, event: str, payload: dict) -> dict:
    passed = receipt["exit_code"] == 0
    if event in {"beforeShellExecution", "beforeMCPExecution"}:
        return {"permission": "allow" if passed else "deny", "user_message": "aidd-gate: " + receipt["status"],
                "agent_message": "Review the aidd-gate run receipt." if not passed else ""}
    if event == "stop" and not passed and payload.get("loop_count", 0) < 1:
        return {"followup_message": "aidd-gate failed. Run aidd-gate check, inspect the receipt, and fix the reported checks."}
    return {}
