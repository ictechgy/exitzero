"""Compare worktree edits with permission zones from an explicitly trusted commit.

This is a merge-time check, not a filesystem sandbox. It never executes repository
code, applies Git filters, follows symlinks or accepts policy from the candidate.
"""
import hashlib
import os
from pathlib import Path
import re
import stat
import subprocess

from .api import Finding
from .files import is_sensitive, match_path, open_regular, safe_path, validate_relative

_MAX_FILES = 20000
_MAX_FILE_BYTES = 32 * 1024 * 1024
_MAX_TOTAL_BYTES = 256 * 1024 * 1024
_HEX = re.compile(r"(?:[a-f0-9]{40}|[a-f0-9]{64})\Z")


class AuthorityDenied(Exception):
    """A diagnosed policy violation, distinct from incomplete inspection."""


def validate_permissions(value: object) -> None:
    if not isinstance(value, dict) or set(value) != {"editable", "protected", "immutable"}:
        raise ValueError("permissions requires editable, protected and immutable arrays")
    for patterns in value.values():
        if not isinstance(patterns, list) or any(not isinstance(p, str) for p in patterns):
            raise ValueError("Permission zones must be arrays of repository patterns")
        for pattern in patterns:
            validate_relative(pattern)
            if pattern.endswith("/") or is_sensitive(Path(pattern)):
                raise ValueError("Permission zones require non-secret file patterns")


def _git(root: Path, *args: str) -> bytes:
    env = {**os.environ, "GIT_NO_LAZY_FETCH": "1", "GIT_NO_REPLACE_OBJECTS": "1",
           "GIT_TERMINAL_PROMPT": "0"}
    result = subprocess.run(["git", "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false",
                             "-C", str(root), *args], capture_output=True, timeout=15, env=env)
    if result.returncode or len(result.stdout) > _MAX_TOTAL_BYTES:
        raise ValueError("Unable to inspect the local trusted Git baseline")
    return result.stdout


def resolve(root: Path, ref: str) -> str:
    if not isinstance(ref, str) or not ref.strip() or ref.startswith("-") or "\0" in ref:
        raise ValueError("trust-base must name an available local commit")
    value = _git(root, "rev-parse", "--verify", ref + "^{commit}").decode().strip()
    if not _HEX.fullmatch(value):
        raise ValueError("Invalid trusted commit")
    return value


def _path(raw: bytes) -> str:
    relative = raw.decode("utf-8")
    validate_relative(relative)
    if any(ord(c) < 32 or ord(c) == 127 for c in relative) or is_sensitive(Path(relative)):
        raise ValueError("Unsupported or credential-like path in permission inspection")
    return relative


def _worktree(root: Path, baseline: dict, algorithm: str) -> dict:
    paths = set(baseline)
    paths.update(_path(name) for name in _git(root, "ls-files", "--cached", "--others",
                                            "--exclude-standard", "-z").split(b"\0") if name)
    if len(paths) > _MAX_FILES:
        raise ValueError("Permission inspection file budget exceeded")
    snapshot, total = {}, 0
    for relative in sorted(paths):
        path = safe_path(root, relative)
        if not os.path.lexists(path):
            continue
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > _MAX_FILE_BYTES:
            raise ValueError("Permission inspection requires bounded regular files")
        before_size = metadata.st_size
        total += before_size
        if total > _MAX_TOTAL_BYTES:
            raise ValueError("Permission inspection byte budget exceeded")
        # No clean filters or assume-unchanged/skip-worktree index flags influence
        # these raw bytes. The second snapshot checks membership and modes too.
        with open_regular(path, root=root) as stream:
            metadata = os.fstat(stream.fileno())
            data = stream.read(_MAX_FILE_BYTES + 1)
        if len(data) > _MAX_FILE_BYTES:
            raise ValueError("Permission inspection byte budget exceeded")
        total += len(data) - before_size
        if total > _MAX_TOTAL_BYTES:
            raise ValueError("Permission inspection byte budget exceeded")
        blob = hashlib.new(algorithm, b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
        snapshot[relative] = {"blob": blob, "sha256": hashlib.sha256(data).hexdigest(),
                              "mode": "100755" if metadata.st_mode & 0o111 else "100644"}
    return snapshot


def inspect(root: Path, policy_name: str, ref: str) -> tuple[dict, list[Finding], dict]:
    """Return a bounded decision record and opaque state for finish()."""
    from .policy import parse_policy

    safe_path(root, policy_name)
    top = _git(root, "rev-parse", "--show-toplevel").decode().strip()
    if Path(top).resolve() != root.resolve():
        raise ValueError("Permission inspection requires the Git worktree root")
    commit = resolve(root, ref)
    algorithm = _git(root, "rev-parse", "--show-object-format").decode().strip()
    if algorithm not in {"sha1", "sha256"}:
        raise ValueError("Unsupported Git object format")
    baseline = {}
    for entry in _git(root, "ls-tree", "-rz", "--full-tree", commit).split(b"\0"):
        if not entry:
            continue
        metadata, raw = entry.split(b"\t", 1)
        mode, kind, blob = metadata.decode("ascii").split()
        if kind != "blob" or mode not in {"100644", "100755"} or not _HEX.fullmatch(blob):
            raise ValueError("Permission baseline requires regular files; submodules and links are unsupported")
        baseline[_path(raw)] = {"mode": mode, "blob": blob}
        if len(baseline) > _MAX_FILES:
            raise ValueError("Permission inspection file budget exceeded")
    if policy_name not in baseline:
        raise ValueError("Trusted commit does not contain the selected policy")
    raw_policy = _git(root, "cat-file", "blob", baseline[policy_name]["blob"])
    if len(raw_policy) > 1024 * 1024:
        raise ValueError("Trusted policy is too large")
    policy = parse_policy(raw_policy.decode("utf-8"))
    if "permissions" not in policy:
        raise ValueError("Trusted policy must declare permission zones")
    zones = policy["permissions"]
    worktree = _worktree(root, baseline, algorithm)
    changes, findings = [], []
    for relative in sorted(set(baseline) | set(worktree)):
        before, after = baseline.get(relative), worktree.get(relative)
        if before and after and all(before[key] == after[key] for key in ("mode", "blob")):
            continue
        zone = "unclassified"
        for name in ("immutable", "protected", "editable"):
            if match_path(relative, zones[name]):
                zone = name
                break
        if relative == policy_name:
            zone = "immutable"  # The candidate can never rewrite its authority.
        decision = {"editable": "allowed", "protected": "review_required",
                    "immutable": "denied", "unclassified": "denied"}[zone]
        changes.append({"path": relative, "zone": zone, "decision": decision,
                        "change": "added" if before is None else "deleted" if after is None else "modified",
                        "before_blob": before["blob"] if before else None,
                        "after_sha256": after["sha256"] if after else None})
        if decision != "allowed":
            findings.append(Finding("core.permission-" + zone,
                                    "Protected change requires independent review." if zone == "protected" else
                                    "Change is outside the trusted edit authority.", relative))
    record = {"base_commit": commit, "policy_sha256": hashlib.sha256(raw_policy).hexdigest(),
              "status": "failed" if findings else "passed", "changes": changes,
              "coverage": "tracked-and-unignored", "git_object_format": algorithm}
    state = {"ref": ref, "commit": commit, "baseline": baseline, "algorithm": algorithm,
             "worktree": worktree, "policy": policy}
    return record, findings, state


def finish(root: Path, state: dict) -> bool:
    """False if the authority reference or inspected worktree changed mid-run."""
    return (resolve(root, state["ref"]) == state["commit"]
            and _worktree(root, state["baseline"], state["algorithm"]) == state["worktree"])
