import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "core" / "src"))
sys.path.insert(0, str(ROOT / "packages" / "plugin-ledger" / "src"))

from exitzero.api import Context  # noqa: E402
from exitzero_ledger import _load_receipts, ledger_publish  # noqa: E402


def make_context(root: Path) -> Context:
    return Context(root=root, policy={}, policy_path=root / "exitzero.toml")


def write_receipt(root: Path, run_id: str, command: str = "check",
                  status: str = "passed", started_at: str = "2026-09-16T00:00:00+00:00",
                  findings: list | None = None) -> None:
    runs = root / ".exitzero" / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    receipt = {"schema_version": 1, "run_id": run_id, "command": command,
               "status": status, "exit_code": {"passed": 0, "failed": 1}.get(status, 2),
               "started_at": started_at, "findings": findings or [],
               "receipt": f".exitzero/runs/{run_id}.json"}
    (runs / f"{run_id}.json").write_text(json.dumps(receipt), encoding="utf-8")


def latest_record(root: Path) -> dict:
    records = sorted((root / ".exitzero" / "ledger").glob("record-*.json"))
    assert records, "ledger record was not persisted"
    return json.loads(records[-1].read_text(encoding="utf-8"))


class LedgerArgsTests(unittest.TestCase):
    def test_usage_errors_exit_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            context = make_context(Path(tmp))
            self.assertEqual(ledger_publish(context, ["--nope"]), 2)
            self.assertEqual(ledger_publish(context, ["--pr"]), 2)
            self.assertEqual(ledger_publish(context, ["--pr", "abc"]), 2)
            self.assertEqual(ledger_publish(context, ["--pr", "0"]), 2)
            self.assertEqual(ledger_publish(context, ["--since", "not-a-date"]), 2)

    def test_invalid_or_option_like_base_exits_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context = make_context(root)
            self.assertEqual(ledger_publish(context, ["--base", "--output=/tmp/x"]), 2)
            self.assertEqual(ledger_publish(context, ["--base", "no-such-ref"]), 2)


class LedgerRecordTests(unittest.TestCase):
    def test_empty_runs_still_persist_a_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(ledger_publish(make_context(root), []), 0)
            record = latest_record(root)
            self.assertEqual(record["schema_version"], 1)
            self.assertEqual(record["receipts"], 0)
            self.assertEqual(record["rollback_hints"]["implicated_paths"], [])
            markdown = sorted((root / ".exitzero" / "ledger").glob("record-*.md"))[-1]
            self.assertIn("run record", markdown.read_text(encoding="utf-8"))

    def test_aggregates_and_implicates_finding_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_receipt(root, "aaa", status="passed")
            write_receipt(root, "bbb", status="failed", command="hooks run",
                          findings=[{"rule": "syntax", "path": "src/bad.py",
                                     "message": "x"},
                                    {"rule": "syntax", "path": "src/bad.py",
                                     "message": "y"}])
            write_receipt(root, "ccc", status="error",
                          findings=[{"rule": "core.error"}])
            runs = root / ".exitzero" / "runs"
            (runs / "junk.json").write_text("{{{{", encoding="utf-8")
            (runs / "empty.json").write_text("{}", encoding="utf-8")
            (runs / "badshape.json").write_text(
                json.dumps({"run_id": "b", "command": "check", "status": "failed",
                            "findings": None}), encoding="utf-8")
            self.assertEqual(ledger_publish(make_context(root), []), 0)
            record = latest_record(root)
            self.assertEqual(record["receipts"], 3)
            self.assertEqual(sorted(record["corrupt_receipts"]),
                             ["badshape.json", "empty.json", "junk.json"])
            self.assertEqual(record["by_status"], {"passed": 1, "failed": 1, "error": 1})
            self.assertEqual(record["findings_by_rule"], {"syntax": 2, "core.error": 1})
            hints = record["rollback_hints"]["implicated_paths"]
            self.assertEqual(hints, [{"path": "src/bad.py", "finding_count": 2,
                                      "rules": ["syntax"]}])

    def test_passing_run_findings_count_but_stay_out_of_hints(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_receipt(root, "warned", status="passed",
                          findings=[{"rule": "advice", "path": "src/ok.py",
                                     "message": "nit"}])
            self.assertEqual(ledger_publish(make_context(root), []), 0)
            record = latest_record(root)
            self.assertEqual(record["findings_by_rule"], {"advice": 1})
            self.assertEqual(record["rollback_hints"]["implicated_paths"], [])

    def test_symlinked_receipt_is_corrupt_not_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_receipt(root, "real")
            outside = root / "outside.json"
            outside.write_text(json.dumps(
                {"run_id": "x", "command": "check", "status": "failed",
                 "findings": [{"rule": "syntax", "path": "secret.py",
                               "message": "m"}]}), encoding="utf-8")
            os.symlink(outside, root / ".exitzero" / "runs" / "linked.json")
            self.assertEqual(ledger_publish(make_context(root), []), 0)
            record = latest_record(root)
            self.assertEqual(record["receipts"], 1)
            self.assertEqual(record["corrupt_receipts"], ["linked.json"])
            self.assertEqual(record["rollback_hints"]["implicated_paths"], [])

    def test_since_filters_old_receipts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_receipt(root, "old", started_at="2026-09-01T00:00:00+00:00")
            write_receipt(root, "new", started_at="2026-09-15T00:00:00+00:00")
            self.assertEqual(ledger_publish(
                make_context(root), ["--since", "2026-09-10T00:00:00Z"]), 0)
            record = latest_record(root)
            self.assertEqual(record["receipts"], 1)
            self.assertEqual(record["run_refs"], [".exitzero/runs/new.json"])


class LedgerGitTests(unittest.TestCase):
    def test_suspect_commits_listed_when_git_history_exists(self):
        if not subprocess.run(["git", "--version"], capture_output=True).returncode == 0:
            self.skipTest("git is unavailable")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                   "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
            subprocess.run(["git", "init", "-q"], cwd=root, env=env, check=True)
            (root / "src").mkdir()
            (root / "src" / "bad.py").write_text("x = 1\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=root, env=env, check=True)
            subprocess.run(["git", "commit", "-qm", "add bad"], cwd=root, env=env,
                           check=True)
            write_receipt(root, "f1", status="failed",
                          findings=[{"rule": "syntax", "path": "src/bad.py",
                                     "message": "x"}])
            self.assertEqual(ledger_publish(make_context(root), []), 0)
            record = latest_record(root)
            commits = record["rollback_hints"].get("suspect_commits")
            self.assertTrue(commits and commits[0]["subject"] == "add bad")

    def test_no_git_repo_notes_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_receipt(root, "f1", status="failed",
                          findings=[{"rule": "syntax", "path": "x.py", "message": "x"}])
            self.assertEqual(ledger_publish(make_context(root), []), 0)
            record = latest_record(root)
            self.assertIn("suspect_commits_note", record["rollback_hints"])


class LedgerPublishTests(unittest.TestCase):
    def test_pr_flag_invokes_gh_explicitly(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_receipt(root, "aaa")
            bin_dir = root / "bin"
            bin_dir.mkdir()
            marker = root / "gh-argv.txt"
            stub = bin_dir / "gh"
            stub.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > "{marker}"\n',
                            encoding="utf-8")
            stub.chmod(0o755)
            with mock.patch.dict(os.environ, {"PATH": f"{bin_dir}:{os.environ['PATH']}"}):
                code = ledger_publish(make_context(root), ["--pr", "42"])
            self.assertEqual(code, 0)
            self.assertEqual(marker.read_text().splitlines()[:3], ["pr", "comment", "42"])

    def test_pr_without_gh_exits_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_receipt(root, "aaa")
            empty = root / "empty-bin"
            empty.mkdir()
            with mock.patch.dict(os.environ, {"PATH": str(empty)}):
                self.assertEqual(ledger_publish(make_context(root), ["--pr", "7"]), 2)


class LedgerSecurityRegressionTests(unittest.TestCase):
    """Review findings: hostile receipt content, argv batching, body piping."""

    def test_nul_and_control_finding_paths_do_not_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_receipt(root, "f1", status="failed", findings=[
                {"rule": "r", "path": "a\x00b.py", "message": "x"},
                {"rule": "r", "path": "tab\ty.py", "message": "x"},
                {"rule": "r", "path": "ok.py", "message": "x"},
            ])
            self.assertEqual(ledger_publish(make_context(root), []), 0)
            paths = [e["path"] for e in latest_record(root)["rollback_hints"]["implicated_paths"]]
            self.assertEqual(paths, ["ok.py"])

    def test_markdown_body_escapes_receipt_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_receipt(root, "f1", status="failed", findings=[
                {"rule": "syntax`bad", "path": "x`evil`.py", "message": "x"},
            ])
            self.assertEqual(ledger_publish(make_context(root), []), 0)
            body = next((root / ".exitzero" / "ledger").glob("record-*.md")).read_text()
            # A backslash is literal inside a code span, so the fence itself
            # must grow past the embedded backtick run.
            self.assertIn("``x`evil`.py``", body)
            self.assertNotIn("`x`evil`.py` ", body)

    def test_base_validation_disables_lazy_fetch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_receipt(root, "aaa")
            marker = root / "git-env.txt"
            bin_dir = root / "bin"
            bin_dir.mkdir()
            stub = bin_dir / "git"
            stub.write_text(
                f'#!/bin/sh\nprintf "%s" "$GIT_NO_LAZY_FETCH" > "{marker}"\nprintf "0123456789abcdef\\n"\n',
                encoding="utf-8")
            stub.chmod(0o755)
            with mock.patch.dict(os.environ, {"PATH": f"{bin_dir}:{os.environ['PATH']}"}):
                self.assertEqual(ledger_publish(make_context(root), ["--base", "main"]), 0)
            self.assertEqual(marker.read_text(), "1")

    def test_gh_reads_the_body_from_stdin(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_receipt(root, "aaa")
            bin_dir = root / "bin"
            bin_dir.mkdir()
            argv_marker = root / "gh-argv.txt"
            body_marker = root / "gh-body.txt"
            stub = bin_dir / "gh"
            stub.write_text(
                f'#!/bin/sh\nprintf "%s\\n" "$@" > "{argv_marker}"\ncat > "{body_marker}"\n',
                encoding="utf-8")
            stub.chmod(0o755)
            with mock.patch.dict(os.environ, {"PATH": f"{bin_dir}:{os.environ['PATH']}"}):
                self.assertEqual(ledger_publish(make_context(root), ["--pr", "9"]), 0)
            args = argv_marker.read_text().splitlines()
            self.assertEqual(args[3:], ["--body-file", "-"])
            record_body = next((root / ".exitzero" / "ledger").glob("record-*.md")).read_text()
            self.assertEqual(body_marker.read_text(), record_body)

    def test_many_implicated_paths_batch_git_invocations(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            findings = [{"rule": "r", "path": f"p{i}.py", "message": "x"}
                        for i in range(300)]
            write_receipt(root, "f1", status="failed", findings=findings)
            count = root / "git-calls.txt"
            bin_dir = root / "bin"
            bin_dir.mkdir()
            stub = bin_dir / "git"
            stub.write_text(f'#!/bin/sh\necho call >> "{count}"\n', encoding="utf-8")
            stub.chmod(0o755)
            with mock.patch.dict(os.environ, {"PATH": f"{bin_dir}:{os.environ['PATH']}"}):
                self.assertEqual(ledger_publish(make_context(root), []), 0)
            self.assertGreaterEqual(len(count.read_text().splitlines()), 2)

    def test_suspect_commits_keep_global_recency_across_batches(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            findings = [{"rule": "r", "path": f"p{i}.py", "message": "x"}
                        for i in range(300)]
            write_receipt(root, "f1", status="failed", findings=findings)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            stub = bin_dir / "git"
            # The newest commit only touches a path in the second batch; a
            # concatenated-then-truncated list would drop it, so the merge
            # must sort by committer date across batches.
            stub.write_text("""#!/usr/bin/env python3
import sys
paths = [a for a in sys.argv[1:] if a.startswith(":(literal)")]
if any(a.endswith("p299.py") for a in paths):
    sys.stdout.write("f" * 40 + "\\x002099-01-01T00:00:00+00:00\\x00newest\\n")
else:
    for i in range(20):
        sys.stdout.write("%040d\\x002020-01-01T00:00:00+00:00\\x00old-%d\\n" % (i, i))
""", encoding="utf-8")
            stub.chmod(0o755)
            with mock.patch.dict(os.environ, {"PATH": f"{bin_dir}:{os.environ['PATH']}"}):
                self.assertEqual(ledger_publish(make_context(root), []), 0)
            commits = latest_record(root)["rollback_hints"]["suspect_commits"]
            self.assertEqual(commits[0]["subject"], "newest")
            self.assertEqual(commits[0]["sha"], "f" * 12)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFOs need POSIX mkfifo")
    def test_fifo_receipt_is_corrupt_not_blocking(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".exitzero" / "runs").mkdir(parents=True)
            os.mkfifo(root / ".exitzero" / "runs" / "pipe.json")
            self.assertEqual(ledger_publish(make_context(root), []), 0)
            self.assertIn("pipe.json", latest_record(root)["corrupt_receipts"])

    def test_loaded_receipts_drop_the_inputs_map(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runs = root / ".exitzero" / "runs"
            runs.mkdir(parents=True)
            receipt = {"schema_version": 1, "run_id": "r1", "command": "check",
                       "status": "passed", "started_at": "2026-09-16T00:00:00+00:00",
                       "findings": [], "inputs": {f"f{i}.py": "x" * 64 for i in range(500)}}
            (runs / "r1.json").write_text(json.dumps(receipt), encoding="utf-8")
            receipts, corrupt = _load_receipts(root, None)
            self.assertFalse(corrupt)
            self.assertNotIn("inputs", receipts[0])
            self.assertEqual(receipts[0]["command"], "check")


if __name__ == "__main__":
    unittest.main()
