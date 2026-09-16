import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "core" / "src"))
sys.path.insert(0, str(ROOT / "packages" / "plugin-harness" / "src"))

from exitzero.api import Context, Registry  # noqa: E402
from exitzero.policy import render_agents  # noqa: E402
from exitzero_harness import (  # noqa: E402
    API_VERSION,
    lint_config,
    register,
)


BEGIN = "<!-- exitzero:begin -->"
END = "<!-- exitzero:end -->"


def make_context(root: Path, policy=None):
    return Context(
        root=root,
        policy=policy or {},
        policy_path=root / "exitzero.toml",
    )


class HarnessPluginTests(unittest.TestCase):
    def test_registers_linter_and_eval_stub(self):
        registry = Registry()

        register(registry)

        self.assertEqual(API_VERSION, 1)
        self.assertIn("harness.config", registry.linters)
        self.assertEqual(registry.commands["harness-eval"](None, []), 2)

    def test_generated_agents_section_must_match_exactly(self):
        policy = {"checks": [{"id": "syntax", "kind": "python.syntax"}]}
        generated = render_agents(policy)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "AGENTS.md").write_text(
                f"team notes\n{generated}\nlocal notes\n", encoding="utf-8"
            )
            context = make_context(root, policy)
            self.assertEqual(lint_config(context), [])

            (root / "AGENTS.md").write_text(
                f"team notes\n{BEGIN}\nwrong\n{END}\nlocal notes\n", encoding="utf-8"
            )
            findings = lint_config(context)
            self.assertTrue(any("differs" in finding.message for finding in findings))

    def test_missing_multiple_and_broken_markers_are_actionable(self):
        generated = f"{BEGIN}\nmanaged\n{END}"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context = make_context(root)
            with patch("exitzero_harness.render_agents", return_value=generated):
                missing = lint_config(context)
            self.assertTrue(any("missing" in finding.message for finding in missing))

            (root / "AGENTS.md").write_text(
                f"{BEGIN}\none\n{END}\n{BEGIN}\ntwo\n{END}\n", encoding="utf-8"
            )
            with patch("exitzero_harness.render_agents", return_value=generated):
                multiple = lint_config(context)
            self.assertTrue(any("multiple" in finding.message for finding in multiple))

            (root / "AGENTS.md").write_text(
                f"{END}\nbroken\n{BEGIN}\n", encoding="utf-8"
            )
            with patch("exitzero_harness.render_agents", return_value=generated):
                broken = lint_config(context)
            self.assertTrue(any("broken" in finding.message for finding in broken))

    def test_config_files_validate_json_without_executing_values(self):
        policy = {"harness": {"config_files": [".cursor/hooks.json", "mcp.json"]}}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "AGENTS.md").write_text("notes\n", encoding="utf-8")
            (root / ".cursor").mkdir()
            (root / ".cursor/hooks.json").write_text(
                json.dumps({"version": 1, "hooks": {"beforeShellExecution": [{"command": "echo hi"}]}}),
                encoding="utf-8",
            )
            (root / "mcp.json").write_text(
                json.dumps({"mcpServers": {"local": {"command": "echo", "args": ["ok"]}}}),
                encoding="utf-8",
            )
            context = make_context(root, policy)
            with patch("exitzero_harness.render_agents", return_value=""):
                findings = lint_config(context)
            self.assertFalse(any("config file" in finding.message for finding in findings))

            (root / "mcp.json").write_text("{broken", encoding="utf-8")
            with patch("exitzero_harness.render_agents", return_value=""):
                findings = lint_config(context)
            self.assertTrue(any("invalid JSON" in finding.message for finding in findings))

    def test_rejects_secret_paths_and_invalid_config_shapes(self):
        policy = {
            "harness": {
                "config_files": [".env", "auth.json", "bad.json"],
            }
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "bad.json").write_text("[]", encoding="utf-8")
            context = make_context(root, policy)
            with patch("exitzero_harness.render_agents", return_value=""):
                findings = lint_config(context)
            messages = [finding.message for finding in findings]
            self.assertTrue(any("not allowed" in message or "Credential-like" in message for message in messages))
            self.assertTrue(any("must be an object" in message for message in messages))

    def test_rules_reject_unknown_fields_and_conflicting_values(self):
        policy = {
            "harness": {
                "rules": [
                    {"id": "ordering", "value": "imports-first"},
                    {"id": "ordering", "value": "alphabetical"},
                    {"id": "ordering", "value": "alphabetical"},
                    {"id": "bad", "value": True, "extra": "reject"},
                ]
            }
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context = make_context(root, policy)
            with patch("exitzero_harness.render_agents", return_value=""):
                findings = lint_config(context)
            messages = [finding.message for finding in findings]
            self.assertTrue(any("conflicting values" in message for message in messages))
            self.assertTrue(any("duplicate" in message for message in messages))
            self.assertTrue(any("unknown field" in message for message in messages))
            self.assertTrue(any("value must be a string" in message for message in messages))

    def test_cursor_hooks_accept_documented_and_future_slots_but_require_commands(self):
        policy = {"harness": {"config_files": ["hooks.json"]}}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "hooks.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "hooks": {
                            "afterFileEdit": [{"command": "exitzero"}],
                            "futureSlot": [{"command": "future"}],
                        },
                    }
                ),
                encoding="utf-8",
            )
            context = make_context(root, policy)
            with patch("exitzero_harness.render_agents", return_value=""):
                findings = lint_config(context)
            self.assertEqual([f for f in findings if f.path == "hooks.json"], [])

            (root / "hooks.json").write_text(
                json.dumps({"version": True, "hooks": {"stop": [{"command": " "}, {"args": []}]}}),
                encoding="utf-8",
            )
            with patch("exitzero_harness.render_agents", return_value=""):
                findings = lint_config(context)
            messages = [finding.message for finding in findings]
            self.assertTrue(any("version must be 1" in message for message in messages))
            self.assertTrue(any("non-empty command" in message for message in messages))

    def test_mcp_servers_require_exactly_one_endpoint_and_typed_options(self):
        policy = {"harness": {"config_files": ["mcp.json"]}}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "mcp.json").write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            "both": {"command": "run", "url": "https://example.test"},
                            "neither": {},
                            "badTypes": {"command": 4, "args": ["ok", 2], "env": {"TOKEN": 1}},
                        }
                    }
                ),
                encoding="utf-8",
            )
            context = make_context(root, policy)
            with patch("exitzero_harness.render_agents", return_value=""):
                findings = lint_config(context)
            messages = [finding.message for finding in findings]
            self.assertTrue(any("exactly one" in message for message in messages))
            self.assertTrue(any("args must be a list" in message for message in messages))
            self.assertTrue(any("env must be a string map" in message for message in messages))
            self.assertTrue(any("command must be a non-empty string" in message for message in messages))

    def test_cursor_prompt_hooks_are_linted_without_execution(self):
        policy = {"checks": [{"id": "syntax", "kind": "python.syntax"}],
                  "harness": {"config_files": ["hooks.json"]}}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "AGENTS.md").write_text(render_agents(policy) + "\n")
            config = root / "hooks.json"
            config.write_text(json.dumps({"version": 1, "hooks": {"preToolUse": [
                {"type": "prompt", "prompt": "Check tool inputs without executing this text.", "timeout": 10},
                {"type": "command", "command": "do-not-execute-this"},
            ]}}))
            self.assertEqual(lint_config(make_context(root, policy)), [])
            for entry in ({"type": "prompt", "prompt": " "}, {"type": "prompt", "prompt": 3},
                          {"type": "prompt", "command": "echo wrong field"}, {"type": "unknown", "command": "echo"}):
                with self.subTest(entry=entry):
                    config.write_text(json.dumps({"version": 1, "hooks": {"stop": [entry]}}))
                    findings = lint_config(make_context(root, policy))
                    self.assertTrue(any(f.path == "hooks.json" for f in findings))

    def test_agents_read_uses_core_safe_path_for_symlinks(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as outside:
            root = Path(tmp)
            (Path(outside) / "AGENTS.md").write_text("external", encoding="utf-8")
            (root / "AGENTS.md").symlink_to(Path(outside) / "AGENTS.md")
            with self.assertRaises(ValueError):
                lint_config(make_context(root))


if __name__ == "__main__":
    unittest.main()
