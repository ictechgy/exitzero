"""Public command-line interface; check outcomes always come from the runner."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shlex
import subprocess
import sys
from urllib.parse import quote

from . import __version__
from .api import Context, HOOK_SLOTS
from .doctor import ADAPTERS
from .files import safe_path, select_files, validate_relative, write_atomic
from .hooks import (ADAPTER_EVENTS, HOOK_MANAGED_NOTE, copilot_response, cursor_response,
                    install, stop_block_response, valid_push_input)
from .loader import discover
from .policy import DEFAULT_POLICY, load_policy, node_profile_policy, python_profile_policy, sync_agents
from .runner import run


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(prog="exitzero")
    cli.add_argument("--version", action="version", version=__version__)
    cli.add_argument("--root", default=".", help="Repository directory")
    cli.add_argument("--policy", default="exitzero.toml", help="Policy path relative to root")
    commands = cli.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="Create policy and managed AGENTS section")
    init.add_argument("--sync", action="store_true", help="Regenerate only the managed AGENTS section")
    init.add_argument("--profile", choices=("default", "python", "node"), default="default",
                      help="Policy starter profile (python: static checks; node: existing tool commands)")
    init.add_argument("--source-root", action="append", default=[], metavar="PATH",
                      help="Source root; repeat for multiple roots")
    init.add_argument("--allow-module", action="append", default=[], metavar="MODULE",
                      help="External Python module trusted by the imports check")
    init.add_argument("--test-command", metavar="COMMAND",
                      help="Shell-free test command to store as argv")
    init.add_argument("--review-command", action="append", default=[], metavar="COMMAND",
                      help="Shell-free review command; repeatable")
    init.add_argument("--lint-command", metavar="COMMAND", help="Node profile lint command")
    init.add_argument("--typecheck-command", metavar="COMMAND", help="Node profile type-check command")
    init.add_argument("--input-path", action="append", default=[], metavar="GLOB",
                      help="Additional Node input glob, for example JSON test fixtures; repeatable")
    init.add_argument("--test-integrity-base", metavar="REF",
                      help="Opt into Node test deletion/suppression checks against a trusted local Git commit")
    for name in ("check", "lint-config"):
        child = commands.add_parser(name)
        child.add_argument("--format", choices=("human", "json", "sarif"), default="human")
        if name == "check":
            child.add_argument("--trust-base", metavar="REF",
                               help="Enforce permission zones from an independently trusted local Git commit")
            child.add_argument("--reuse", action="store_true",
                               help="Reuse passing check results when a check's selected inputs are unchanged since a prior receipt")
            child.add_argument("--diff", metavar="REF",
                               help="Limit checks to files changed relative to a git ref or range such as origin/main...HEAD")
    doctor = commands.add_parser("doctor", help="Diagnose project policy and hook setup without running checks")
    doctor.add_argument("--format", choices=("human", "json"), default="human")
    doctor.add_argument("--trust-base", metavar="REF", help="Inspect permission zones from a trusted local commit")
    doctor.add_argument("--adapter", choices=ADAPTERS,
                        help="Require this adapter to be configured; other adapters remain optional")
    hooks = commands.add_parser("hooks").add_subparsers(dest="hook_command", required=True)
    installer = hooks.add_parser("install")
    installer.add_argument("--adapter", choices=ADAPTERS, default="cursor")
    installer.add_argument("--trust-base", metavar="REF", help="Operator-selected local authority commit for the hook")
    hook_run = hooks.add_parser("run")
    hook_run.add_argument("--adapter", choices=("generic", *ADAPTER_EVENTS), default="generic")
    hook_run.add_argument("--slot", choices=sorted(HOOK_SLOTS), default="CI")
    hook_run.add_argument("--event", choices=sorted({event for events in ADAPTER_EVENTS.values() for event in events}),
                        default="stop")
    hook_run.add_argument("--format", choices=("human", "json"), default="human")
    authority = hook_run.add_mutually_exclusive_group()
    authority.add_argument("--trust-base", metavar="REF", help="Trusted local Git commit for permission zones")
    authority.add_argument("--use-installed-authority", action="store_true",
                           help="Use the operator pin in the local hook installation manifest")
    pack = commands.add_parser("policy-pack", help="Preview or apply policy-derived AGENTS and client hooks")
    mode = pack.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="Apply the fully preflighted plan")
    mode.add_argument("--check", action="store_true", help="Exit 1 if generated configuration would change; never write")
    pack.add_argument("--trust-base", metavar="REF", help="Operator-selected local authority commit for generated hooks")
    pack.add_argument("--format", choices=("human", "json"), default="human")
    report = commands.add_parser("report")
    report.add_argument("--run-id", help="Select one persisted receipt instead of the latest run")
    report.add_argument("--format", choices=("human", "json", "sarif", "intoto"), default="human")
    plugin = commands.add_parser("plugin")
    plugin.add_argument("name")
    plugin.add_argument("args", nargs=argparse.REMAINDER)
    return cli


def _sarif(receipt: dict) -> dict:
    """Render receipt findings as a SARIF 2.1.0 run for code-scanning tools."""

    rules: list[dict] = []
    rule_index: dict[str, int] = {}
    results: list[dict] = []
    for finding in receipt["findings"]:
        rule_id = finding["rule"]
        if rule_id not in rule_index:
            rule_index[rule_id] = len(rules)
            rules.append({"id": rule_id, "name": rule_id})
        result: dict = {
            "ruleId": rule_id,
            "ruleIndex": rule_index[rule_id],
            "level": "error" if finding.get("severity") == "error" else "warning",
            "message": {"text": finding["message"]},
        }
        if finding.get("path"):
            location: dict = {"artifactLocation": {"uri": quote(finding["path"]), "uriBaseId": "SRCROOT"}}
            if finding.get("line"):
                location["region"] = {"startLine": finding["line"]}
            result["locations"] = [{"physicalLocation": location}]
        results.append(result)
    return {
        "version": "2.1.0",
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "runs": [{
            "tool": {"driver": {
                "name": "exitzero",
                "version": __version__,
                "informationUri": "https://github.com/ictechgy/exitzero",
                "rules": rules,
            }},
            "results": results,
        }],
    }


_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def _scrub(value: object) -> str:
    """Neutralize control characters so hostile paths cannot inject terminal escapes."""
    return _CONTROL.sub(lambda match: f"\\x{ord(match.group(0)):02x}", str(value))


def _intoto(receipt: dict, payload: bytes) -> dict:
    """Wrap a receipt in an unsigned in-toto Statement v1 for CI archival.

    The statement attests that this receipt exists as produced by the gate;
    signatures stay out of scope — local receipts are unsigned evidence.
    """
    return {
        "_type": "https://in-toto.io/Statement/v1",
        "subject": [{"name": receipt.get("receipt") or f"run-{receipt.get('run_id', 'unknown')}",
                     "digest": {"sha256": hashlib.sha256(payload).hexdigest()}}],
        "predicateType": "https://exitzero.dev/attestations/gate/v1",
        "predicate": receipt,
    }


def emit(receipt: dict, output: str, *, receipt_bytes: bytes | None = None) -> None:
    if output == "sarif":
        print(json.dumps(_sarif(receipt), ensure_ascii=False, sort_keys=True))
        return
    if output == "intoto":
        if receipt_bytes is None:
            raise ValueError("in-toto export requires persisted receipt bytes")
        print(json.dumps(_intoto(receipt, receipt_bytes), ensure_ascii=False, sort_keys=True))
        return
    if output == "json":
        print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
        return
    print(f"exitzero: {receipt['status']} (exit {receipt['exit_code']})")
    if receipt.get("command") == "doctor":
        print("Setup diagnosis only: verification checks were not run; runtime enforcement is unverified.")
        for item in receipt.get("diagnostics", []):
            print(f"  {_scrub(item['target'])}: {_scrub(item['state'])} (runtime: {_scrub(item['runtime'])})")
            print(f"    {_scrub(item['detail'])}")
            print(f"    Next: {_scrub(item['next_step'])}")
    reused = [_scrub(check["id"]) for check in receipt.get("checks", []) if check.get("status") == "reused"]
    advisory = [_scrub(check["id"]) for check in receipt.get("checks", [])
                if check.get("status") == "failed" and check.get("blocking") is False]
    if advisory:
        print(f"  Advisory failures (not blocking): {', '.join(advisory)}")
    if reused:
        print(f"  Reused passing evidence (inputs unchanged): {', '.join(reused)}")
    for finding in receipt["findings"]:
        location = f" {_scrub(finding['path'])}" if finding.get("path") else ""
        if finding.get("line"):
            location += f":{finding['line']}"
        print(f"  {_scrub(finding['rule'])}{location}: {_scrub(finding['message'])}")
    if receipt.get("requirements"):
        print("Requirement mappings (check evidence, not semantic proof):")
        for requirement in receipt["requirements"]:
            print(f"  {_scrub(requirement['id'])}: {_scrub(requirement['status'])}")
    print(f"Receipt: {receipt['receipt'] or 'NOT WRITTEN'}")


_COMMAND_OPERATORS = frozenset("|&;<>()")


def _parse_init_command(value: str | None) -> list[str] | None:
    """Parse a user command without invoking a shell or accepting operators."""

    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError("command must be a non-empty string")
    quote: str | None = None
    escaped = False
    for character in value:
        if escaped:
            escaped = False
            continue
        if quote is not None:
            if character == quote:
                quote = None
            elif character == "\\" and quote == '"':
                escaped = True
            continue
        if character in "'\"":
            quote = character
        elif character == "\\":
            escaped = True
        elif character in _COMMAND_OPERATORS or character in "\r\n":
            raise ValueError("commands cannot contain shell operators or control characters")
    if quote is not None or escaped:
        raise ValueError("command has an unterminated quote or escape")
    try:
        argv = shlex.split(value, posix=True)
    except ValueError as error:
        raise ValueError("command has invalid shell-style quoting") from error
    if not argv or any(not isinstance(item, str) or not item for item in argv):
        raise ValueError("command must contain at least one argument")
    return argv


def _init_generation(args: argparse.Namespace) -> str:
    generation_requested = bool(
        args.profile != "default" or args.source_root or args.allow_module
        or args.test_command is not None or args.review_command
        or args.lint_command is not None or args.typecheck_command is not None or args.input_path
        or args.test_integrity_base is not None
    )
    if args.sync and generation_requested:
        raise ValueError("init --sync cannot be combined with policy generation options")
    if args.profile == "default" and generation_requested:
        raise ValueError("--profile python or node is required with generation options")
    if args.profile != "node" and (args.lint_command is not None or args.typecheck_command is not None or args.input_path
                                   or args.test_integrity_base is not None):
        raise ValueError("lint/typecheck commands, input-path and test-integrity-base require --profile node")
    if args.profile == "node" and args.allow_module:
        raise ValueError("allow-module only applies to --profile python")
    if args.profile == "default":
        return DEFAULT_POLICY
    roots = args.source_root or ["."]
    for root in roots:
        validate_relative(root)
        if any(character in root for character in "*?[]"):
            raise ValueError("source roots must be directories, not glob patterns")
    for module in args.allow_module:
        if not module or any(not part.isidentifier() for part in module.split(".")):
            raise ValueError("allow modules must contain dotted module names")
    test_argv = _parse_init_command(args.test_command)
    review_argvs: list[list[str]] = []
    for value in args.review_command:
        argv = _parse_init_command(value)
        if argv is None:
            raise ValueError("review commands must be non-empty strings")
        review_argvs.append(argv)
    if args.profile == "node":
        if args.test_command is None and not safe_path(Path(args.root).resolve(), "package.json").is_file():
            raise ValueError("The default npm test command requires a local package.json; supply --test-command otherwise")
        commands = [("test-command", test_argv if test_argv is not None else ["npm", "test"])]
        for check_id, value in (("lint-command", args.lint_command), ("typecheck-command", args.typecheck_command)):
            command = _parse_init_command(value)
            if command is not None:
                commands.append((check_id, command))
        commands.extend((f"review-{index}", command) for index, command in enumerate(review_argvs, 1))
        for pattern in args.input_path:
            validate_relative(pattern)
        select_files(Path(args.root).resolve(), args.input_path)
        return node_profile_policy(roots, commands, args.input_path, args.test_integrity_base)
    return python_profile_policy(roots, args.allow_module, test_argv, review_argvs)


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    root = Path(args.root).resolve()
    if args.command in {"check", "lint-config", "doctor"}:
        receipt = run(root, args.policy, args.command,
                      reuse=args.command == "check" and args.reuse,
                      diff=args.diff if args.command == "check" else None,
                      doctor_adapter=args.adapter if args.command == "doctor" else None,
                      trust_base=getattr(args, "trust_base", None))
        emit(receipt, args.format)
        return receipt["exit_code"]
    if args.command == "hooks" and args.hook_command == "run":
        hook_trust_base, trust_error = args.trust_base, False
        if args.use_installed_authority:
            from .hooks import _manifest
            from .authority import resolve
            try:
                selected = _manifest(root).get("trust_base")
                if selected is None:
                    raise ValueError("Installed hook authority is missing")
                hook_trust_base = resolve(root, selected)
            except (OSError, ValueError, subprocess.SubprocessError):
                trust_error = True
        events = ADAPTER_EVENTS.get(args.adapter)
        if events is not None:
            if args.event not in events:
                print(f"exitzero: adapter {args.adapter} does not handle event {args.event}; supported: {', '.join(sorted(events))}",
                      file=sys.stderr)
                return 2
            if args.adapter == "cursor":
                try:
                    payload = json.loads(sys.stdin.read(1024 * 1024))
                    if not isinstance(payload, dict) or type(payload.get("loop_count", 0)) is not int or payload.get("loop_count", 0) < 0:
                        raise ValueError("Invalid Cursor payload")
                except (ValueError, OSError, RecursionError):
                    # Still execute and persist the actual gate outcome for every invocation.
                    receipt = run(root, args.policy, "check", events[args.event], input_error=True, trust_base=hook_trust_base, authority_error=trust_error)
                    print(json.dumps({"error": "Invalid Cursor JSON input", "receipt": receipt["receipt"]}))
                    return 2
                receipt = run(root, args.policy, "check", events[args.event], input_error=trust_error, trust_base=hook_trust_base, authority_error=trust_error)
                print(json.dumps(cursor_response(receipt, args.event, payload)))
                # Cursor reads the JSON protocol; generic adapters expose unchanged gate codes.
                return 0 if receipt["exit_code"] != 2 else 2
            # Claude Code, Codex, Gemini and Antigravity share the JSON-object
            # stdin contract; each blocks stop with its own decision word.
            try:
                payload = json.loads(sys.stdin.read(1024 * 1024))
                if not isinstance(payload, dict):
                    raise ValueError("Invalid hook payload")
            except (ValueError, OSError, RecursionError):
                receipt = run(root, args.policy, "check", events[args.event], input_error=True, trust_base=hook_trust_base, authority_error=trust_error)
                if args.adapter == "copilot":
                    print(json.dumps(copilot_response(receipt, args.event)))
                    return 0
                print(json.dumps({"error": f"Invalid {args.adapter} JSON input", "receipt": receipt["receipt"]}))
                return 2
            receipt = run(root, args.policy, "check", events[args.event], input_error=trust_error, trust_base=hook_trust_base, authority_error=trust_error)
            if args.adapter == "copilot":
                print(json.dumps(copilot_response(receipt, args.event)))
                return 0
            print(json.dumps(stop_block_response(receipt,
                                                 "continue" if args.adapter == "agy" else "block")))
            return 0 if receipt["exit_code"] != 2 else 2
        push_error = False
        if args.slot == "pre-push":
            try:
                payload = sys.stdin.read(1024 * 1024 + 1)
                push_error = len(payload) > 1024 * 1024 or not valid_push_input(root, payload)
            except (OSError, UnicodeError):
                push_error = True
        receipt = run(root, args.policy, "check", args.slot, input_error=push_error or trust_error, trust_base=hook_trust_base, authority_error=trust_error)
        emit(receipt, args.format)
        return receipt["exit_code"]
    try:
        path = safe_path(root, args.policy)
        if args.command == "init":
            if not root.is_dir():
                raise ValueError("Root directory must exist")
            generated_policy = _init_generation(args)
            if not args.sync:
                if path.exists():
                    raise ValueError("Policy already exists; use init --sync")
                write_atomic(path, generated_policy)
                ignore = safe_path(root, ".gitignore")
                existing = ignore.read_text(encoding="utf-8") if ignore.is_file() else ""
                ignored = ("/.exitzero/", "node_modules/" if args.profile == "node" else "__pycache__/")
                additions = [item for item in ignored if item not in existing.splitlines()]
                if additions:
                    write_atomic(ignore, existing.rstrip() + ("\n" if existing else "") + "\n".join(additions) + "\n")
                if args.profile != "node" and not select_files(root, ["**/*.py"]):
                    write_atomic(safe_path(root, "exitzero_sample.py"),
                                 '"""Replace this sample with your project checks."""\n\ndef add(left: int, right: int) -> int:\n    return left + right\n')
            policy = load_policy(path)
            sync_agents(root, policy)
            print("Policy and AGENTS.md synchronized. Run exitzero check to verify.")
            return 0
        if args.command == "report":
            directory = safe_path(root, ".exitzero/runs")
            if args.run_id is not None:
                if not re.fullmatch(r"[0-9a-f]{32}", args.run_id):
                    raise ValueError("Invalid receipt run id")
                latest = safe_path(root, f".exitzero/runs/{args.run_id}.json")
            else:
                files = sorted((p for p in directory.glob("*.json") if p.is_file()),
                               key=lambda p: p.stat().st_mtime_ns)
                if not files:
                    raise ValueError("No receipt exists; run check first")
                latest = safe_path(root, files[-1].relative_to(root).as_posix())
            if not latest.is_file():
                raise ValueError("Receipt must be a regular file")
            raw = latest.read_bytes()
            receipt = json.loads(raw)
            if (not isinstance(receipt, dict) or receipt.get("run_id") != latest.stem
                    or receipt.get("receipt") != latest.relative_to(root).as_posix()):
                raise ValueError("Receipt identity does not match its file")
            emit(receipt, args.format, receipt_bytes=raw)
            return 0
        policy = load_policy(path)
        if args.command == "policy-pack":
            from .pack import compile_pack
            report = compile_pack(root, path, policy, apply=args.apply, trust_base=args.trust_base)
            report["exit_code"] = int(args.check and any(change["state"] != "unchanged" for change in report["changes"]))
            if args.format == "json":
                print(json.dumps(report, ensure_ascii=False, sort_keys=True))
            else:
                for change in report["changes"]:
                    print(f"{change['state']}: {_scrub(change['path'])}")
                for limitation in report["limitations"]:
                    print(_scrub(limitation))
                if report.get("receipt"):
                    print("Plan receipt: " + report["receipt"])
            return report["exit_code"]
        if args.command == "hooks":
            relative = install(root, path, args.adapter, trust_base=args.trust_base)
            print("Installed " + relative + ". Run lint-config to verify configuration.")
            note = HOOK_MANAGED_NOTE.get(args.adapter, "")
            if note:
                print(note)
            return 0
        registry = discover(policy["plugins"])
        if args.name not in registry.commands:
            raise ValueError("No such plugin command")
        result = registry.commands[args.name](Context(root, policy, path), args.args)
        if type(result) is not int or result not in (0, 1, 2):
            raise ValueError("Plugin command must return exit code 0, 1 or 2")
        return result
    except Exception as error:
        print(f"exitzero: unable to complete command ({type(error).__name__}); check policy, paths and existing files.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
