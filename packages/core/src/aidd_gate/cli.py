"""Public command-line interface; check outcomes always come from the runner."""
import argparse
import json
from pathlib import Path
import sys

from . import __version__
from .api import Context, HOOK_SLOTS
from .files import safe_path, select_files
from .hooks import CURSOR_EVENTS, cursor_response, install
from .loader import discover
from .policy import DEFAULT_POLICY, load_policy, sync_agents
from .runner import run


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(prog="aidd-gate")
    cli.add_argument("--version", action="version", version=__version__)
    cli.add_argument("--root", default=".", help="Repository directory")
    cli.add_argument("--policy", default="aidd-gate.toml", help="Policy path relative to root")
    commands = cli.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="Create policy and managed AGENTS section")
    init.add_argument("--sync", action="store_true", help="Regenerate only the managed AGENTS section")
    for name in ("check", "lint-config"):
        child = commands.add_parser(name)
        child.add_argument("--format", choices=("human", "json"), default="human")
    hooks = commands.add_parser("hooks").add_subparsers(dest="hook_command", required=True)
    installer = hooks.add_parser("install")
    installer.add_argument("--adapter", choices=("cursor", "pre-commit"), default="cursor")
    hook_run = hooks.add_parser("run")
    hook_run.add_argument("--adapter", choices=("generic", "cursor"), default="generic")
    hook_run.add_argument("--slot", choices=sorted(HOOK_SLOTS), default="CI")
    hook_run.add_argument("--event", choices=sorted(CURSOR_EVENTS), default="stop")
    hook_run.add_argument("--format", choices=("human", "json"), default="human")
    report = commands.add_parser("report")
    report.add_argument("--format", choices=("human", "json"), default="human")
    plugin = commands.add_parser("plugin")
    plugin.add_argument("name")
    plugin.add_argument("args", nargs=argparse.REMAINDER)
    return cli


def emit(receipt: dict, output: str) -> None:
    if output == "json":
        print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
        return
    print(f"aidd-gate: {receipt['status']} (exit {receipt['exit_code']})")
    for finding in receipt["findings"]:
        location = f" {finding['path']}" if finding.get("path") else ""
        if finding.get("line"):
            location += f":{finding['line']}"
        print(f"  {finding['rule']}{location}: {finding['message']}")
    print(f"Receipt: {receipt['receipt'] or 'NOT WRITTEN'}")


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    root = Path(args.root).resolve()
    if args.command in {"check", "lint-config"}:
        receipt = run(root, args.policy, args.command)
        emit(receipt, args.format)
        return receipt["exit_code"]
    if args.command == "hooks" and args.hook_command == "run":
        if args.adapter == "cursor":
            try:
                payload = json.loads(sys.stdin.read(1024 * 1024))
                if not isinstance(payload, dict) or type(payload.get("loop_count", 0)) is not int or payload.get("loop_count", 0) < 0:
                    raise ValueError("Invalid Cursor payload")
            except (ValueError, OSError):
                # Still execute and persist the actual gate outcome for every invocation.
                receipt = run(root, args.policy, "check", CURSOR_EVENTS[args.event])
                print(json.dumps({"error": "Invalid Cursor JSON input", "receipt": receipt["receipt"]}))
                return 2
            receipt = run(root, args.policy, "check", CURSOR_EVENTS[args.event])
            print(json.dumps(cursor_response(receipt, args.event, payload)))
            # Cursor reads the JSON protocol; generic adapters expose unchanged gate codes.
            return 0 if receipt["exit_code"] != 2 else 2
        receipt = run(root, args.policy, "check", args.slot)
        emit(receipt, args.format)
        return receipt["exit_code"]
    try:
        path = safe_path(root, args.policy)
        if args.command == "init":
            if not root.is_dir():
                raise ValueError("Root directory must exist")
            if not args.sync:
                if path.exists():
                    raise ValueError("Policy already exists; use init --sync")
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(DEFAULT_POLICY, encoding="utf-8")
                ignore = safe_path(root, ".gitignore")
                existing = ignore.read_text(encoding="utf-8") if ignore.exists() else ""
                additions = [item for item in ("/.aidd-gate/", "__pycache__/") if item not in existing.splitlines()]
                if additions:
                    ignore.write_text(existing.rstrip() + ("\n" if existing else "") + "\n".join(additions) + "\n", encoding="utf-8")
                if not select_files(root, ["**/*.py"]):
                    example = safe_path(root, "aidd_gate_sample.py")
                    example.write_text('"""Replace this sample with your project checks."""\n\ndef add(left: int, right: int) -> int:\n    return left + right\n', encoding="utf-8")
            policy = load_policy(path)
            sync_agents(root, policy)
            print("Policy and AGENTS.md synchronized. Run aidd-gate check to verify.")
            return 0
        if args.command == "report":
            directory = safe_path(root, ".aidd-gate/runs")
            files = sorted(directory.glob("*.json"), key=lambda p: p.stat().st_mtime_ns)
            if not files:
                raise ValueError("No receipt exists; run check first")
            latest = safe_path(root, files[-1].relative_to(root).as_posix())
            emit(json.loads(latest.read_text(encoding="utf-8")), args.format)
            return 0
        policy = load_policy(path)
        if args.command == "hooks":
            print("Installed " + install(root, path, args.adapter) + ". Run lint-config to verify configuration.")
            return 0
        registry = discover(policy["plugins"])
        if args.name not in registry.commands:
            raise ValueError("No such plugin command")
        result = registry.commands[args.name](Context(root, policy, path), args.args)
        if type(result) is not int or result not in (0, 1, 2):
            raise ValueError("Plugin command must return exit code 0, 1 or 2")
        return result
    except Exception as error:
        print(f"aidd-gate: unable to complete command ({type(error).__name__}); check policy, paths and existing files.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
