"""Focused contracts for policy generation through ``exitzero init``."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "bin" / "exitzero"


class InitProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def cli(self, *args):
        return subprocess.run(
            [sys.executable, str(CLI), "--root", str(self.root), *args],
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_python_profile_roundtrips_paths_modules_and_commands(self):
        (self.root / "src").mkdir()
        (self.root / "lib").mkdir()
        (self.root / "tests").mkdir()
        (self.root / "src/main.py").write_text("VALUE = 1\n", encoding="utf-8")
        (self.root / "lib/helper.py").write_text("VALUE = 1\n", encoding="utf-8")
        (self.root / "tests/test_smoke.py").write_text(
            "def test_smoke():\n    value = 1\n    assert value == 1\n", encoding="utf-8"
        )
        (self.root / "review_contract.py").write_text("print('review')\n", encoding="utf-8")
        result = self.cli(
            "init",
            "--profile",
            "python",
            "--source-root",
            "src",
            "--source-root",
            "lib",
            "--allow-module",
            "numpy",
            "--test-command",
            '{python} -c "print(\'test|smoke\')"',
            "--review-command",
            "{python} review_contract.py",
            "--review-command",
            '{python} -c "print(\'review|smoke\')"',
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        policy_path = self.root / "exitzero.toml"
        self.assertTrue(policy_path.is_file())
        from_exitzero = None
        sys.path.insert(0, str(ROOT / "packages" / "core" / "src"))
        try:
            from exitzero.policy import load_policy

            from_exitzero = load_policy(policy_path)
        finally:
            sys.path.pop(0)
        self.assertEqual(from_exitzero["checks"][0]["paths"], ["src/**/*.py", "lib/**/*.py", "tests/**/*.py"])
        imports = from_exitzero["checks"][1]
        self.assertEqual(imports["options"], {"roots": ["src", "lib"], "allow_modules": ["numpy"]})
        commands = [check for check in from_exitzero["checks"] if check["kind"] == "command"]
        self.assertEqual([check["id"] for check in commands], ["test-command", "review-1", "review-2"])
        self.assertEqual(commands[0]["options"]["argv"], ["{python}", "-c", "print('test|smoke')"])
        self.assertTrue(all(check["paths"] == ["**/*.py"] for check in commands))
        result = self.cli("check", "--format", "json")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        receipt = json.loads((self.root / json.loads(result.stdout)["receipt"]).read_text(encoding="utf-8"))
        self.assertIn("review_contract.py", receipt["inputs"])
        self.assertIn("review-1", (self.root / "AGENTS.md").read_text(encoding="utf-8"))

    def test_python_profile_without_source_root_uses_repository_scope(self):
        result = self.cli("init", "--profile", "python")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        policy = (self.root / "exitzero.toml").read_text(encoding="utf-8")
        self.assertIn('paths = ["**/*.py", "tests/**/*.py"]', policy)
        self.assertIn('roots = ["."]', policy)

    def test_init_does_not_execute_commands(self):
        sentinel = self.root / "init-was-executed"
        command = f'{{python}} -c "from pathlib import Path; Path({str(sentinel)!r}).touch()"'
        result = self.cli("init", "--profile", "python", "--test-command", command)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(sentinel.exists())

    def test_invalid_command_fails_before_filesystem_mutation(self):
        result = self.cli("init", "--profile", "python", "--test-command", "pytest tests; touch bad")
        self.assertEqual(result.returncode, 2)
        self.assertFalse((self.root / "exitzero.toml").exists())
        self.assertFalse((self.root / "AGENTS.md").exists())
        self.assertFalse((self.root / ".gitignore").exists())

    def test_quoted_punctuation_is_literal_but_unquoted_operator_is_rejected(self):
        accepted = self.cli("init", "--profile", "python", "--test-command", 'pytest "a|b"')
        self.assertEqual(accepted.returncode, 0, accepted.stdout + accepted.stderr)
        self.assertIn('"a|b"', (self.root / "exitzero.toml").read_text(encoding="utf-8"))

        other = tempfile.TemporaryDirectory()
        self.addCleanup(other.cleanup)
        other_root = Path(other.name)
        rejected = subprocess.run(
            [sys.executable, str(CLI), "--root", str(other_root), "init", "--profile", "python", "--test-command", "pytest a|b"],
            capture_output=True,
            text=True,
            timeout=30,
            shell=False,
        )
        self.assertEqual(rejected.returncode, 2)
        self.assertFalse((other_root / "exitzero.toml").exists())

    def test_empty_command_and_sync_generation_options_fail_without_mutation(self):
        empty = self.cli("init", "--profile", "python", "--test-command", "   ")
        self.assertEqual(empty.returncode, 2)
        self.assertFalse((self.root / "exitzero.toml").exists())

        self.assertEqual(self.cli("init").returncode, 0)
        policy_before = (self.root / "exitzero.toml").read_bytes()
        agents_before = (self.root / "AGENTS.md").read_bytes()
        invalid_sync = self.cli("init", "--sync", "--profile", "python", "--source-root", "src")
        self.assertEqual(invalid_sync.returncode, 2)
        self.assertEqual((self.root / "exitzero.toml").read_bytes(), policy_before)
        self.assertEqual((self.root / "AGENTS.md").read_bytes(), agents_before)

    def test_existing_policy_is_not_overwritten_by_profile_generation(self):
        self.assertEqual(self.cli("init").returncode, 0)
        policy = self.root / "exitzero.toml"
        original = policy.read_bytes()
        result = self.cli("init", "--profile", "python", "--source-root", "src")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(policy.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
