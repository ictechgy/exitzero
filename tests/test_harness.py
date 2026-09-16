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

    def test_toml_config_files_validate_mcp_servers(self):
        policy = {"harness": {"config_files": ["mcp.toml", "misc.toml", "broken.toml", "absent.txt"]}}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "mcp.toml").write_text(
                '[mcp_servers.local]\ncommand = "server"\nargs = ["--stdio"]\n\n'
                '[mcp_servers.remote]\nurl = "https://mcp.example.test"\n',
                encoding="utf-8",
            )
            (root / "misc.toml").write_text("[tool.example]\noption = true\n", encoding="utf-8")
            (root / "broken.toml").write_text("[unclosed\n", encoding="utf-8")
            context = make_context(root, policy)
            with patch("exitzero_harness.render_agents", return_value=""):
                findings = lint_config(context)
            messages = [finding.message for finding in findings]
            self.assertFalse(any("mcp.toml" in message for message in messages))
            self.assertTrue(any("must contain mcp_servers" in message for message in messages))
            self.assertTrue(any("invalid TOML" in message for message in messages))
            self.assertTrue(any(".json or .toml" in message for message in messages))

            (root / "mcp.toml").write_text(
                '[mcp_servers.both]\ncommand = "server"\nurl = "https://mcp.example.test"\n\n'
                '[mcp_servers.badargs]\ncommand = "server"\nargs = ["ok", 2]\n',
                encoding="utf-8",
            )
            with patch("exitzero_harness.render_agents", return_value=""):
                findings = lint_config(context)
            messages = [finding.message for finding in findings]
            self.assertTrue(any("exactly one" in message for message in messages))
            self.assertTrue(any("args must be a list" in message for message in messages))

    def test_claude_settings_hooks_linted_without_version(self):
        policy = {"harness": {"config_files": [".claude/settings.json"]}}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".claude").mkdir()
            (root / "scripts").mkdir()
            (root / "scripts/gate.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            settings = root / ".claude/settings.json"
            settings.write_text(json.dumps({"hooks": {"PreToolUse": [
                {"matcher": "Bash", "hooks": [{"type": "command", "command": "./scripts/gate.sh"}]},
            ], "Stop": [{"hooks": [{"command": "exitzero hooks run --event stop"}]}]}}),
                encoding="utf-8")
            context = make_context(root, policy)
            with patch("exitzero_harness.render_agents", return_value=""):
                self.assertEqual(
                    [f for f in lint_config(context) if f.path == ".claude/settings.json"], [])

    def test_claude_settings_hook_defects_are_findings(self):
        policy = {"harness": {"config_files": ["settings.json"]}}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = root / "settings.json"
            context = make_context(root, policy)

            settings.write_text(json.dumps({"hooks": {
                "SessionStart": [{"matcher": "startup"}],
                "PreToolUse": [{"hooks": [{"type": "prompt", "command": "echo"}]}],
                "PostToolUse": [{"hooks": [{"command": "./missing.sh"}]}],
                "Stop": [],
            }}), encoding="utf-8")
            with patch("exitzero_harness.render_agents", return_value=""):
                findings = lint_config(context)
            messages = [finding.message for finding in findings]
            self.assertTrue(any("non-empty hooks list" in message for message in messages))
            self.assertTrue(any("type must be command" in message for message in messages))
            self.assertTrue(any("does not exist" in message for message in messages))
            self.assertTrue(any("has no entries" in message for message in messages))
            self.assertFalse(any("version must be 1" in message for message in messages))

    def test_cursor_entries_without_version_are_flagged(self):
        policy = {"harness": {"config_files": ["hooks.json"]}}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hooks = root / "hooks.json"
            context = make_context(root, policy)

            hooks.write_text(json.dumps({"hooks": {"stop": [{"command": "echo hi"}]}}),
                             encoding="utf-8")
            with patch("exitzero_harness.render_agents", return_value=""):
                findings = lint_config(context)
            self.assertTrue(any("require version 1" in f.message for f in findings))

            hooks.write_text(json.dumps({"version": 1, "hooks": {"stop": [{"command": "./missing.sh"}]}}),
                             encoding="utf-8")
            with patch("exitzero_harness.render_agents", return_value=""):
                findings = lint_config(context)
            self.assertTrue(any("does not exist" in f.message for f in findings))

    def test_cursor_entries_may_carry_matcher_fields(self):
        policy = {"harness": {"config_files": ["hooks.json"]}}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hooks = root / "hooks.json"
            hooks.write_text(json.dumps({"version": 1, "hooks": {"stop": [
                {"matcher": "git.*", "command": "echo ok"},
                {"matcher": ".*", "type": "prompt", "prompt": "review the diff"},
            ]}}), encoding="utf-8")
            context = make_context(root, policy)
            with patch("exitzero_harness.render_agents", return_value=""):
                self.assertEqual(
                    [f for f in lint_config(context) if f.path == "hooks.json"], [])

    def test_shell_syntax_never_produces_missing_path_findings(self):
        policy = {"harness": {"config_files": ["hooks.json"]}}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "scripts").mkdir()
            (root / "scripts/gate.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            hooks = root / "hooks.json"
            hooks.write_text(json.dumps({"version": 1, "hooks": {"stop": [
                {"command": "./scripts/gate.sh; true"},
                {"command": "./scripts/gate.sh>/dev/null"},
                {"command": "${HOME}/scripts/gate.sh"},
                {"command": "./scripts/gate.sh"},
                {"command": "./scripts/absent.sh"},
            ]}}), encoding="utf-8")
            context = make_context(root, policy)
            with patch("exitzero_harness.render_agents", return_value=""):
                findings = [f for f in lint_config(context) if f.path == "hooks.json"]
            self.assertEqual([f.message for f in findings],
                             ["hook command path does not exist: ./scripts/absent.sh"])

    def test_agents_read_uses_core_safe_path_for_symlinks(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as outside:
            root = Path(tmp)
            (Path(outside) / "AGENTS.md").write_text("external", encoding="utf-8")
            (root / "AGENTS.md").symlink_to(Path(outside) / "AGENTS.md")
            with self.assertRaises(ValueError):
                lint_config(make_context(root))


if __name__ == "__main__":
    unittest.main()
