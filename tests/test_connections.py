"""Focused tests for the bounded Python connectivity check."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "core" / "src"))
sys.path.insert(0, str(ROOT / "packages" / "plugin-verify" / "src"))

from exitzero.api import CheckSpec, Context
from exitzero.services import select_files
from exitzero_verify.connections import check_connections, connection_inputs


class ConnectionTests(unittest.TestCase):
    def context(self, root: Path) -> Context:
        return Context(root, {}, root / "exitzero.toml")

    def spec(self, connection, *, roots=None):
        options = {"connections": [connection]}
        if roots is not None:
            options["roots"] = roots
        return CheckSpec("connections", "python.connections", ("**/*.py",), options)

    def connection(self, **changes):
        value = {
            "source": "app.py",
            "target": "lib.py",
            "symbol": "handler",
            "within": "<module>",
            "usage": "call",
        }
        value.update(changes)
        return value

    def write_pair(self, source="from lib import handler\nhandler()\n", target="def handler(): pass\n"):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        (root / "app.py").write_text(source, encoding="utf-8")
        (root / "lib.py").write_text(target, encoding="utf-8")
        return root

    def test_named_import_and_call_pass(self):
        root = self.write_pair()
        self.assertEqual(check_connections(self.context(root), self.spec(self.connection())), [])

    def test_target_side_effect_is_never_executed(self):
        root = self.write_pair(target="raise RuntimeError('static only')\ndef handler(): pass\n")
        self.assertEqual(check_connections(self.context(root), self.spec(self.connection())), [])

    def test_module_alias_and_renamed_symbol_pass(self):
        root = self.write_pair("import lib as implementation\nimplementation.handler()\n")
        self.assertEqual(check_connections(self.context(root), self.spec(self.connection())), [])
        root = self.write_pair("from lib import handler as run_handler\nrun_handler()\n")
        self.assertEqual(check_connections(self.context(root), self.spec(self.connection())), [])

    def test_argument_usage_requires_named_consumer_call(self):
        root = self.write_pair("from lib import handler\nregistry.add(handler)\n")
        connection = self.connection(usage="argument", consumer="registry.add")
        self.assertEqual(check_connections(self.context(root), self.spec(connection)), [])
        root.joinpath("app.py").write_text("from lib import handler\nother.add(handler)\n", encoding="utf-8")
        self.assertTrue(check_connections(self.context(root), self.spec(connection)))

    def test_relative_import_and_nested_scope_pass(self):
        root = self.write_pair()
        (root / "pkg").mkdir()
        (root / "pkg/__init__.py").write_text("", encoding="utf-8")
        (root / "pkg/lib.py").write_text("class Handler: pass\n", encoding="utf-8")
        (root / "pkg/app.py").write_text(
            "from .lib import Handler as LocalHandler\n"
            "def run():\n    return LocalHandler()\n", encoding="utf-8")
        connection = self.connection(source="pkg/app.py", target="pkg/lib.py", symbol="Handler",
                                     within="run")
        self.assertEqual(check_connections(self.context(root), self.spec(connection)), [])
        (root / "pkg/app.py").write_text(
            "from .lib import Handler\n"
            "class Runner:\n    def run(self):\n        return Handler()\n", encoding="utf-8")
        connection = self.connection(source="pkg/app.py", target="pkg/lib.py", symbol="Handler",
                                     within="Runner.run")
        self.assertEqual(check_connections(self.context(root), self.spec(connection)), [])

    def test_relative_import_without_a_package_is_not_treated_as_absolute(self):
        root = self.write_pair("from .lib import handler\nhandler()\n")
        findings = check_connections(self.context(root), self.spec(self.connection()))
        self.assertEqual(len(findings), 1)
        self.assertIn("does not import", findings[0].message)

    def test_relative_import_from_package_initializer_passes(self):
        root = self.write_pair()
        (root / "pkg").mkdir()
        (root / "pkg/__init__.py").write_text(
            "from .lib import Handler\nHandler()\n", encoding="utf-8")
        (root / "pkg/lib.py").write_text("class Handler: pass\n", encoding="utf-8")
        connection = self.connection(source="pkg/__init__.py", target="pkg/lib.py", symbol="Handler")
        self.assertEqual(check_connections(self.context(root), self.spec(connection)), [])

    def test_removed_registration_and_constant_string_decoy_fail(self):
        root = self.write_pair("from lib import handler\nname = 'handler()'\n")
        findings = check_connections(self.context(root), self.spec(self.connection()))
        self.assertEqual(len(findings), 1)
        self.assertIn("no matching use", findings[0].message)

    def test_duplicate_test_implementation_and_shadowing_fail(self):
        root = self.write_pair("def handler(): pass\nhandler()\n")
        self.assertTrue(check_connections(self.context(root), self.spec(self.connection())))
        root.joinpath("app.py").write_text(
            "from lib import handler\nhandler = lambda: None\nhandler()\n", encoding="utf-8")
        findings = check_connections(self.context(root), self.spec(self.connection()))
        self.assertEqual(len(findings), 1)
        self.assertIn("shadowed", findings[0].message)

    def test_module_and_ancestor_rebindings_taint_nested_uses(self):
        root = self.write_pair(
            "from lib import handler\n"
            "handler = lambda: None\n"
            "def run():\n    handler()\n")
        connection = self.connection(within="run")
        findings = check_connections(self.context(root), self.spec(connection))
        self.assertEqual(len(findings), 1)
        self.assertIn("shadowed", findings[0].message)

        root.joinpath("app.py").write_text(
            "from lib import handler\n"
            "class Container:\n"
            "    handler = lambda: None\n"
            "    def run(self):\n        handler()\n", encoding="utf-8")
        connection = self.connection(within="Container.run")
        findings = check_connections(self.context(root), self.spec(connection))
        self.assertEqual(len(findings), 1)
        self.assertIn("shadowed", findings[0].message)

        root.joinpath("app.py").write_text(
            "from lib import handler\n"
            "def outer():\n"
            "    from other import handler\n"
            "    def run():\n        handler()\n"
            "    return run\n", encoding="utf-8")
        (root / "other.py").write_text("def handler(): pass\n", encoding="utf-8")
        connection = self.connection(within="outer.run")
        findings = check_connections(self.context(root), self.spec(connection))
        self.assertEqual(len(findings), 1)
        self.assertIn("shadowed", findings[0].message)

        root.joinpath("app.py").write_text(
            "from lib import handler\n"
            "def outer():\n"
            "    handler = lambda: None\n"
            "    def run():\n        handler()\n"
            "    return run\n", encoding="utf-8")
        connection = self.connection(within="outer.run")
        findings = check_connections(self.context(root), self.spec(connection))
        self.assertEqual(len(findings), 1)
        self.assertIn("shadowed", findings[0].message)

    def test_attribute_monkeypatch_taints_module_binding(self):
        root = self.write_pair(
            "import lib\n"
            "lib.handler = lambda: None\n"
            "lib.handler()\n")
        findings = check_connections(self.context(root), self.spec(self.connection()))
        self.assertEqual(len(findings), 1)
        self.assertIn("shadowed", findings[0].message)

    def test_target_redefinition_and_duplicate_declaration_fail(self):
        root = self.write_pair(target="def handler(): pass\nhandler = object()\n")
        self.assertIn("does not declare", check_connections(self.context(root), self.spec(self.connection()))[0].message)
        root.joinpath("lib.py").write_text(
            "def handler(): pass\ndef handler(): pass\n", encoding="utf-8")
        self.assertIn("does not declare", check_connections(self.context(root), self.spec(self.connection()))[0].message)
        root.joinpath("lib.py").write_text(
            "if False:\n    def handler(): pass\n", encoding="utf-8")
        self.assertIn("does not declare", check_connections(self.context(root), self.spec(self.connection()))[0].message)

    def test_multiple_clean_aliases_are_each_accepted(self):
        root = self.write_pair(
            "from lib import handler as first\n"
            "from lib import handler as second\n"
            "second()\n")
        self.assertEqual(check_connections(self.context(root), self.spec(self.connection())), [])

    def test_conditional_imports_and_dead_uses_do_not_connect(self):
        root = self.write_pair(
            "if False:\n    from lib import handler\n"
            "handler()\n")
        findings = check_connections(self.context(root), self.spec(self.connection()))
        self.assertEqual(len(findings), 1)
        self.assertIn("conditional", findings[0].message)
        root.joinpath("app.py").write_text(
            "from lib import handler\n"
            "if False:\n    handler()\n", encoding="utf-8")
        findings = check_connections(self.context(root), self.spec(self.connection()))
        self.assertEqual(len(findings), 1)
        self.assertIn("unsupported conditional or repeated code", findings[0].message)
        self.assertEqual(findings[0].line, 3)

    def test_unreachable_else_and_zero_iteration_loop_do_not_connect(self):
        root = self.write_pair(
            "from lib import handler\n"
            "if True:\n"
            "    pass\n"
            "else:\n"
            "    handler()\n")
        findings = check_connections(self.context(root), self.spec(self.connection()))
        self.assertEqual(len(findings), 1)
        self.assertIn("unsupported conditional or repeated code", findings[0].message)
        root.joinpath("app.py").write_text(
            "from lib import handler\n"
            "for item in ():\n"
            "    handler()\n", encoding="utf-8")
        findings = check_connections(self.context(root), self.spec(self.connection()))
        self.assertEqual(len(findings), 1)
        self.assertIn("unsupported conditional or repeated code", findings[0].message)

    def test_try_match_and_with_bodies_do_not_establish_unconditional_use(self):
        cases = (
            "try:\n    pass\nexcept Exception:\n    handler()\n",
            "try:\n    pass\nexcept Exception:\n    pass\nelse:\n    handler()\n",
            "try:\n    pass\nfinally:\n    handler()\n",
            "match value:\n    case _:\n        handler()\n",
            "with context():\n    handler()\n",
        )
        for body in cases:
            with self.subTest(body=body):
                root = self.write_pair("from lib import handler\n" + body)
                findings = check_connections(self.context(root), self.spec(self.connection()))
                self.assertEqual(len(findings), 1)
                self.assertIn("unsupported conditional or repeated code", findings[0].message)
                self.assertIn("command check", findings[0].message)
                self.assertEqual(findings[0].severity, "error")
                self.assertEqual(findings[0].category, "violation")

    def test_short_circuit_comparison_and_assert_message_do_not_connect(self):
        for expression in ("False == True == handler()", "assert True, handler()",
                           "False and handler()", "True or handler()"):
            with self.subTest(expression=expression):
                root = self.write_pair("from lib import handler\n" + expression + "\n")
                findings = check_connections(self.context(root), self.spec(self.connection()))
                self.assertEqual(len(findings), 1)
                self.assertIn("unsupported conditional or repeated code", findings[0].message)
                self.assertEqual(findings[0].line, 2)
        root = self.write_pair("from lib import handler\nassert handler()\n")
        self.assertEqual(check_connections(self.context(root), self.spec(self.connection())), [])
        root = self.write_pair("from lib import handler\nTrue == handler()\n")
        self.assertEqual(check_connections(self.context(root), self.spec(self.connection())), [])

    def test_unsupported_use_reports_first_matching_line(self):
        root = self.write_pair(
            "from lib import handler as run\n"
            "with context():\n"
            "    unrelated()\n"
            "    run()\n"
            "if False:\n"
            "    run()\n")
        finding, = check_connections(self.context(root), self.spec(self.connection()))
        self.assertEqual(finding.path, "app.py")
        self.assertEqual(finding.line, 4)
        self.assertIn("unsupported conditional or repeated code", finding.message)
        self.assertIn("behavior/contract test", finding.message)

    def test_unsupported_registration_requires_exact_binding_and_consumer(self):
        root = self.write_pair(
            "import lib as implementation\n"
            "with context():\n"
            "    registry.add(callback=implementation.handler)\n")
        spec = self.spec(self.connection(usage="argument", consumer="registry.add"))
        finding, = check_connections(self.context(root), spec)
        self.assertIn("unsupported conditional or repeated code", finding.message)
        self.assertEqual(finding.line, 3)
        for use in ("other.add(implementation.handler)", "registry.add(other.handler)",
                    "registry.add('implementation.handler')"):
            with self.subTest(use=use):
                (root / "app.py").write_text("import lib as implementation\nwith context():\n    " + use + "\n")
                finding, = check_connections(self.context(root), spec)
                self.assertIn("no matching use", finding.message)

    def test_unrelated_conditional_and_nested_calls_remain_missing(self):
        for body in ("with context():\n    unrelated()\n", "def nested():\n    handler()\n",
                     "value = lambda: handler()\n", "value = 'handler()'\n"):
            with self.subTest(body=body):
                root = self.write_pair("from lib import handler\n" + body)
                finding, = check_connections(self.context(root), self.spec(self.connection()))
                self.assertIn("no matching use", finding.message)
                self.assertIsNone(finding.line)

    def test_supported_use_still_passes_with_conditional_uses(self):
        for body in (
            "if False:\n    handler()\nhandler()\n",
            "handler()\nwith context():\n    handler()\n",
            "if handler():\n    pass\n", "for item in handler():\n    pass\n",
            "while handler():\n    break\n", "with handler():\n    pass\n",
            "try:\n    handler()\nexcept Exception:\n    pass\n",
        ):
            with self.subTest(body=body):
                root = self.write_pair("from lib import handler\n" + body)
                self.assertEqual(check_connections(self.context(root), self.spec(self.connection())), [])

    def test_async_and_comprehension_uses_keep_failing_with_specific_diagnostic(self):
        for body in (
            "async with context():\n    handler()\n",
            "async for item in values:\n    handler()\n",
            "values = [handler() for item in ()]\n",
            "value = handler() if enabled else None\n",
        ):
            with self.subTest(body=body):
                root = self.write_pair("from lib import handler\nasync def run():\n"
                                       + "".join("    " + line + "\n" for line in body.splitlines()))
                finding, = check_connections(self.context(root), self.spec(self.connection(within="run")))
                self.assertIn("unsupported conditional or repeated code", finding.message)
                self.assertGreaterEqual(finding.line, 3)

    def test_cli_receipt_and_sarif_preserve_block_and_warn_semantics(self):
        root = self.write_pair("from lib import handler\nwith context():\n    handler()\n")
        for mode, exit_code, severity in (("block", 1, "error"), ("warn", 0, "warning")):
            with self.subTest(mode=mode):
                (root / "exitzero.toml").write_text(
                    'version = 1\nplugins = ["exitzero_verify"]\n[[checks]]\n'
                    'id = "link"\nkind = "python.connections"\npaths = ["app.py"]\n'
                    f'enforcement = "{mode}"\n[checks.options]\nroots = ["."]\n'
                    'connections = [{source="app.py", target="lib.py", symbol="handler", '
                    'within="<module>", usage="call"}]\n', encoding="utf-8")
                cli = [sys.executable, str(ROOT / "bin/exitzero"), "--root", str(root)]
                process = subprocess.run([*cli, "check", "--format", "json"],
                                         capture_output=True, text=True, timeout=30)
                self.assertEqual(process.returncode, exit_code, process.stdout + process.stderr)
                receipt = json.loads(process.stdout)
                self.assertEqual(receipt, json.loads((root / receipt["receipt"]).read_text()))
                finding, = receipt["findings"]
                self.assertEqual((finding["rule"], finding["line"], finding["severity"], finding["category"]),
                                 ("link", 3, severity, "violation"))
                self.assertIn("unsupported conditional or repeated code", finding["message"])
                check, = receipt["checks"]
                self.assertEqual(check["status"], "failed")
                self.assertEqual(check["blocking"], mode == "block")
                report = subprocess.run([*cli, "report", "--run-id", receipt["run_id"], "--format", "sarif"],
                                        capture_output=True, text=True, timeout=30)
                self.assertEqual(report.returncode, 0, report.stdout + report.stderr)
                result, = json.loads(report.stdout)["runs"][0]["results"]
                self.assertEqual(result["message"]["text"], finding["message"])
                self.assertEqual(result["ruleId"], "link")
                self.assertEqual(result["level"], severity)
                self.assertEqual(result["locations"][0]["physicalLocation"]["region"]["startLine"], 3)

    def test_duplicate_scope_names_are_ambiguous(self):
        root = self.write_pair(
            "from lib import handler\n"
            "def run():\n    handler()\n"
            "def run():\n    handler()\n")
        findings = check_connections(self.context(root), self.spec(self.connection(within="run")))
        self.assertEqual(len(findings), 1)
        self.assertIn("scope", findings[0].message)

    def test_missing_or_ambiguous_module_is_rejected(self):
        root = self.write_pair()
        connection = self.connection(target="missing.py")
        with self.assertRaises(ValueError):
            connection_inputs(self.context(root), self.spec(connection))
        (root / "src").mkdir()
        (root / "src/lib.py").write_text("def handler(): pass\n", encoding="utf-8")
        (root / "src/app.py").write_text("from lib import handler\nhandler()\n", encoding="utf-8")
        connection = self.connection(source="src/app.py", target="src/lib.py")
        findings = check_connections(self.context(root), self.spec(connection, roots=[".", "src"]))
        self.assertTrue(findings)
        self.assertIn("ambiguous", findings[0].message)

    def test_malformed_schema_and_symlinks_fail_closed(self):
        root = self.write_pair()
        invalid = [
            {},
            {"source": "app.py", "target": "lib.py", "symbol": "handler", "within": "<module>", "usage": "argument"},
            {**self.connection(), "consumer": "registry.add"},
            {**self.connection(), "within": "<module>.bad"},
            {**self.connection(), "source": "../app.py"},
        ]
        for item in invalid:
            with self.subTest(item=item), self.assertRaises(ValueError):
                connection_inputs(self.context(root), self.spec(item))
        (root / "link.py").symlink_to(root / "lib.py")
        with self.assertRaises(ValueError):
            connection_inputs(self.context(root), self.spec(self.connection(source="link.py")))

    def test_input_patterns_fingerprint_new_python_members(self):
        root = self.write_pair()
        spec = self.spec(self.connection())
        patterns = connection_inputs(self.context(root), spec)
        before = {path.relative_to(root).as_posix() for path in select_files(root, patterns)}
        (root / "new.py").write_text("value = 1\n", encoding="utf-8")
        after = {path.relative_to(root).as_posix() for path in select_files(root, patterns)}
        self.assertIn("new.py", after - before)
        self.assertIn("app.py", patterns)
        self.assertIn("lib.py", patterns)


if __name__ == "__main__":
    unittest.main()
