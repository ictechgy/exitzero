"""Local snapshot and receipt helpers shared by repository pilot runners."""
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "bin/exitzero"
sys.path.insert(0, str(ROOT / "packages/core/src"))
from exitzero.files import safe_path


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def git(source: Path, *args: str) -> bytes:
    return subprocess.check_output(["git", "-C", str(source), *args], stderr=subprocess.PIPE)


def tracked_state(source: Path) -> dict:
    names = git(source, "ls-files", "-z").decode().split("\0")
    return {"head": git(source, "rev-parse", "HEAD").decode().strip(),
            "status_sha256": digest(git(source, "status", "--porcelain")),
            "files": {name: digest(safe_path(source, name).read_bytes()) for name in names if name}}


def export(source: Path, destination: Path, commit: str) -> dict[str, str]:
    destination.mkdir()
    archive = git(source, "archive", commit)
    with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
        for member in bundle.getmembers():
            target = safe_path(destination, member.name)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                with bundle.extractfile(member) as handle, target.open("xb") as output:
                    output.write(handle.read())
                target.chmod(0o755 if member.mode & 0o111 else 0o644)
            else:
                raise ValueError("Pilot export requires ordinary files and directories")
    return {p.relative_to(destination).as_posix(): digest(p.read_bytes())
            for p in sorted(destination.rglob("*")) if p.is_file()}


def execute(root: Path, argv: list[str], log: Path) -> dict:
    process = subprocess.run(argv, cwd=root, capture_output=True, text=True, timeout=120)
    log.write_text(process.stdout + process.stderr, encoding="utf-8")
    return {"exit_code": process.returncode, "stdout": process.stdout, "stderr": process.stderr,
            "log": log.relative_to(ROOT).as_posix()}


def gate(case: Path, command: str, log: Path, python: str = sys.executable) -> dict:
    outcome = execute(case, [python, str(CLI), "--root", str(case), command, "--format", "json"], log)
    receipt = json.loads(outcome["stdout"])
    saved = safe_path(case, receipt["receipt"])
    if json.loads(saved.read_text()) != receipt or outcome["exit_code"] != receipt["exit_code"]:
        raise AssertionError("Gate exit code, JSON output and saved receipt disagree")
    outcome["receipt_path"] = saved.relative_to(ROOT).as_posix()
    outcome["receipt"] = receipt
    return outcome
