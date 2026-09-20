#!/usr/bin/env python3
"""Exercise the Node profile on a pinned p-limit checkout, without installing packages."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import statistics
import subprocess
import sys
import time
import uuid

from pilot_support import CLI, ROOT, digest, export, safe_path, tracked_state

PIN = "a8a6fbec4e0e866d6d779b10889bb4f5567e70eb"
COMMANDS = {
    "test-command": ["node", "node_modules/ava/entrypoints/cli.mjs"],
    "lint-command": ["node", "node_modules/xo/dist/cli.js"],
    "typecheck-command": ["node", "node_modules/tsd/dist/cli.js"],
}
CASES = {
    "baseline": set(),
    "benign-comment": set(),
    "syntax-error": {"test-command", "lint-command"},
    "wrong-active-count": {"test-command"},
    "wrong-type": {"typecheck-command"},
    "lint-error": {"lint-command"},
    "deleted-test": set(),  # Deliberate blind spot: commands do not enforce test integrity.
    "agents-drift": {"harness.agents.drift"},
}


def mutate(case: Path, name: str) -> None:
    replacements = {
        "benign-comment": ("index.js", "import Queue", "// Pilot: behavior is unchanged.\nimport Queue"),
        "syntax-error": ("index.js", "let activeCount = 0;", "let activeCount = ;"),
        "wrong-active-count": ("index.js", "get: () => activeCount,", "get: () => activeCount + 1,"),
        "wrong-type": ("index.d.ts", "readonly activeCount: number;", "readonly activeCount: string;"),
        "lint-error": ("index.js", "let activeCount = 0;", "let activeCount = 0;;"),
        "deleted-test": ("test.js", "test('accepts additional arguments', async t => {\n"
                         "\tconst limit = pLimit(1);\n\tconst symbol = Symbol('test');\n\n"
                         "\tawait limit(a => t.is(a, symbol), symbol);\n});\n\n", ""),
        "skipped-test": ("test.js", "test('accepts additional arguments',",
                         "test.skip('accepts additional arguments',"),
        "agents-drift": ("AGENTS.md", "Policy SHA-256:", "Stale policy SHA-256:"),
    }
    if name == "baseline":
        return
    relative, before, after = replacements[name]
    path = case / relative
    original = path.read_text()
    if original.count(before) != 1:
        raise ValueError("Pinned mutation anchor is not unique")
    path.write_text(original.replace(before, after, 1), encoding="utf-8")


def execute(case: Path, argv: list[str], log: Path, env: dict) -> dict:
    started = time.monotonic()
    result = subprocess.run(argv, cwd=case, env=env, stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, timeout=120)
    log.write_text(result.stdout + result.stderr, encoding="utf-8")
    return {"exit_code": result.returncode, "duration_seconds": time.monotonic() - started,
            "log": log.relative_to(ROOT).as_posix(), "stdout": result.stdout}


def gate(case: Path, args: list[str], log: Path, env: dict) -> dict:
    result = execute(case, [sys.executable, str(CLI), "--root", str(case), *args,
                            "--format", "json"], log, env)
    receipt = json.loads(result.pop("stdout"))
    saved = safe_path(case, receipt["receipt"])
    if json.loads(saved.read_text()) != receipt or result["exit_code"] != receipt["exit_code"]:
        raise AssertionError("Gate exit, stdout and persisted receipt disagree")
    result.update(receipt=receipt, receipt_path=saved.relative_to(ROOT).as_posix())
    return result


def run(source: Path, dependencies: Path, integrity: bool = False) -> tuple[dict, Path]:
    before = tracked_state(source)
    if before["head"] != PIN:
        raise ValueError("Source HEAD differs from the reviewed pilot revision")
    lock = dependencies / "package-lock.json"
    modules = dependencies / "node_modules"
    if not lock.is_file() or not modules.is_dir():
        raise ValueError("Provide an existing dependency installation and its package-lock.json")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    artifact = safe_path(ROOT, f".exitzero/pilots/node-{stamp}-{uuid.uuid4().hex[:8]}")
    artifact.mkdir(parents=True)
    summary = {"schema_version": 1, "source_commit": PIN, "status": "failed", "cases": [],
               "skips": [], "integrity_enabled": integrity,
               "known_blind_spots": [] if integrity else ["deleted-test"]}
    env = {key: value for key, value in os.environ.items()
           if key in {"PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "SYSTEMROOT"}}
    for name in ("npm-user.conf", "npm-global.conf"):
        (artifact / name).write_text("")
    env.update(NPM_CONFIG_USERCONFIG=str(artifact / "npm-user.conf"),
               NPM_CONFIG_GLOBALCONFIG=str(artifact / "npm-global.conf"),
               NPM_CONFIG_CACHE=str(artifact / "npm-cache"), NPM_CONFIG_OFFLINE="true",
               NPM_CONFIG_IGNORE_SCRIPTS="true", NPM_CONFIG_UPDATE_NOTIFIER="false", NO_COLOR="1",
               GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_AUTHOR_NAME="exitzero pilot", GIT_AUTHOR_EMAIL="pilot@example.invalid",
               GIT_COMMITTER_NAME="exitzero pilot", GIT_COMMITTER_EMAIL="pilot@example.invalid")
    try:
        template = artifact / "template"
        manifest = export(source, template, PIN)
        (artifact / "source-manifest.json").write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
        summary["source_file_count"] = len(manifest)
        summary["code_sha256"] = {p.relative_to(ROOT).as_posix(): digest(p.read_bytes()) for p in sorted([
            *ROOT.glob("packages/*/src/**/*.py"), CLI, Path(__file__).resolve(), ROOT / "scripts/pilot_support.py"])}
        shutil.copy2(lock, artifact / "package-lock.json")
        shutil.copy2(lock, template / "package-lock.json")
        summary["lock_sha256"] = digest(lock.read_bytes())
        summary["versions"] = {name: json.loads((modules / name / "package.json").read_text())["version"]
                               for name in ("ava", "xo", "tsd", "typescript", "yocto-queue")}
        for tool in ("node", "npm"):
            result = execute(template, [tool, "--version"], artifact / f"{tool}-version.log", env)
            if result["exit_code"]:
                raise AssertionError("Tool version probe failed")
            summary["versions"][tool] = result["stdout"].strip()
        (template / "node_modules").symlink_to(modules, target_is_directory=True)
        upstream = execute(template, ["npm", "test"], artifact / "upstream.log", env)
        summary["upstream"] = upstream
        if upstream["exit_code"] != 0 or not re.search(r"\b30 tests passed\b", upstream["stdout"]):
            raise AssertionError("Unmodified upstream baseline must pass all 30 tests")
        # First exercise the default one-command onboarding, then a fresh split profile.
        default = artifact / "default"
        shutil.copytree(template, default, symlinks=True)
        init = execute(default, [sys.executable, str(CLI), "--root", str(default), "init",
                                 "--profile", "node"], artifact / "default-init.log", env)
        if init["exit_code"]:
            raise AssertionError("Default Node profile initialization failed")
        summary["default_init"] = init
        summary["default_check"] = gate(default, ["check"], artifact / "default-check.log", env)
        if summary["default_check"]["exit_code"]:
            raise AssertionError("Default npm test profile failed")
        argv = [sys.executable, str(CLI), "--root", str(template), "init", "--profile", "node"]
        for check, command in COMMANDS.items():
            argv.extend(["--" + check, " ".join(command)])
        if integrity:
            # Seed a local baseline from the reviewed export; never commit in the source checkout.
            for label, command in (
                ("init", ["init", "--quiet"]),
                ("add", ["add", "--", *manifest]),
                ("commit", ["-c", "commit.gpgsign=false", "commit", "--quiet", "-m", "Pinned pilot baseline"]),
            ):
                result = execute(template, ["git", "-c", "core.hooksPath=" + os.devnull, *command],
                                 artifact / f"baseline-git-{label}.log", env)
                if result["exit_code"]:
                    raise AssertionError("Could not seed the isolated Git baseline")
            result = execute(template, ["git", "rev-parse", "HEAD"], artifact / "baseline-git-head.log", env)
            if result["exit_code"]:
                raise AssertionError("Could not identify the isolated Git baseline")
            summary["baseline_commit"] = result["stdout"].strip()
            argv.extend(["--test-integrity-base", summary["baseline_commit"]])
        summary["split_init"] = execute(template, argv, artifact / "split-init.log", env)
        if summary["split_init"]["exit_code"]:
            raise AssertionError("Split Node profile initialization failed")
        expectations = dict(CASES)
        if integrity:
            expectations["deleted-test"] = {"test-integrity"}
            expectations["skipped-test"] = {"test-integrity", "lint-command"}
        for name, expected in expectations.items():
            case = artifact / name
            shutil.copytree(template, case, symlinks=True,
                            ignore=shutil.ignore_patterns(".exitzero"))
            mutate(case, name)
            direct = {check: execute(case, command, artifact / f"{name}-{check}.log", env)
                      for check, command in COMMANDS.items()}
            check = gate(case, ["check"], artifact / f"{name}-check.log", env)
            lint = gate(case, ["lint-config"], artifact / f"{name}-config.log", env)
            found = {finding["rule"] for finding in check["receipt"]["findings"]}
            direct_failures = {key for key, result in direct.items() if result["exit_code"] != 0}
            counts = re.findall(r"\b(\d+) tests passed\b", direct["test-command"]["stdout"])
            count = int(counts[-1]) if counts else None
            valid = (check["exit_code"] == int(bool(expected)) and found == expected
                     and direct_failures == expected.intersection(COMMANDS)
                     and lint["exit_code"] == int(name == "agents-drift"))
            if "test-command" not in expected:
                valid = valid and count == (29 if name in ("deleted-test", "skipped-test") else 30)
            summary["cases"].append({"name": name, "status": "passed" if valid else "failed",
                                     "expected_rules": sorted(expected), "test_count": count,
                                     "direct": direct, "check": check, "lint": lint})
            print(f"{name}: {'PASS' if valid else 'FAIL'} (check={check['exit_code']}, tests={count})", flush=True)
            if name == "baseline" and not valid:
                raise AssertionError("Split-profile baseline failed; mutations were not scored")
        if any(case["status"] != "passed" for case in summary["cases"]):
            raise AssertionError("A mutation did not match its declared outcome")
        # Exercise the real hook dispatch used by CI, including its failure path.
        summary["ci_slots"] = {}
        for name, expected_exit in (("baseline", 0), ("wrong-active-count", 1)):
            result = gate(artifact / name, ["hooks", "run", "--slot", "CI"],
                          artifact / f"{name}-ci.log", env)
            summary["ci_slots"][name] = result
            if result["exit_code"] != expected_exit or result["receipt"].get("hook_slot") != "CI":
                raise AssertionError("CI slot did not preserve the expected gate verdict")
        timings = []
        for index in range(3):
            pair = {}
            for mode in (("direct", "gate") if index % 2 == 0 else ("gate", "direct")):
                if mode == "direct":
                    results = [execute(artifact / "baseline", command,
                                       artifact / f"timing-{index}-{check}.log", env)
                               for check, command in COMMANDS.items()]
                    if any(result["exit_code"] for result in results):
                        raise AssertionError("Timing control failed")
                    pair[mode] = sum(result["duration_seconds"] for result in results)
                else:
                    result = gate(artifact / "baseline", ["check"], artifact / f"timing-{index}-gate.log", env)
                    if result["exit_code"]:
                        raise AssertionError("Timing gate failed")
                    pair[mode] = result["duration_seconds"]
            timings.append(pair)
        summary["timings"] = timings
        summary["median_seconds"] = {mode: statistics.median(p[mode] for p in timings) for mode in ("direct", "gate")}
        summary["status"] = "passed" if all(c["status"] == "passed" for c in summary["cases"]) else "failed"
    except Exception as error:
        summary["error"] = f"Pilot did not complete ({type(error).__name__}); inspect preserved logs."
    finally:
        summary["source_unchanged"] = tracked_state(source) == before
        if not summary["source_unchanged"]:
            summary["status"] = "failed"
        output = artifact / "summary.json"
        output.write_text(json.dumps(summary, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return summary, output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="Pinned local p-limit checkout")
    parser.add_argument("--dependencies", type=Path, required=True,
                        help="Prepared directory containing node_modules and package-lock.json")
    parser.add_argument("--integrity", action="store_true",
                        help="Seed a local Git baseline and require test deletion/skip detection")
    args = parser.parse_args()
    summary, output = run(args.source.resolve(), args.dependencies.resolve(), args.integrity)
    print(f"Pilot: {summary['status']}; source unchanged: {summary['source_unchanged']}")
    print("Evidence: " + output.relative_to(ROOT).as_posix())
    return 0 if summary["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
