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
from exitzero_ledger import ledger_publish  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
