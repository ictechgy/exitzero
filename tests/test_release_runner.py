import subprocess
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import release_check  # noqa: E402


class ReleaseRunnerTests(unittest.TestCase):
    def test_workspace_archive_error_cannot_leave_pass_status(self):
        summary = {"status": "passed", "errors": []}
        release_check._apply_workspace_errors(summary, {"errors": ["receipt archive failed (OSError)"]})
        self.assertEqual(summary["status"], "failed")
        self.assertEqual(summary["errors"], ["receipt archive failed (OSError)"])

    def test_timeout_bytes_are_retained_in_command_evidence(self):
        with tempfile.TemporaryDirectory(prefix="aidd release test ") as temporary:
            root = Path(temporary)
            artifact = root / "artifact"
            artifact.mkdir()
            run = release_check.ReleaseRun(artifact, root / "wheel.whl", root, root / "venv")
            timeout = subprocess.TimeoutExpired(["offline-command"], 1, output=b"stdout-bytes", stderr=b"stderr-bytes")
            with patch.object(release_check.subprocess, "run", side_effect=timeout):
                with self.assertRaises(release_check.ReleaseFailure):
                    run.command("timeout", ["offline-command"], root, {})
            self.assertEqual(run.commands[0]["stdout"], "stdout-bytes")
            self.assertEqual(run.commands[0]["stderr"], "stderr-bytes")
            self.assertTrue(run.commands[0]["timed_out"])


if __name__ == "__main__":
    unittest.main()
