"""Focused tests for the bounded Python connectivity check."""

from pathlib import Path
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
        self.assertIn("no matching use", findings[0].message)

    def test_unreachable_else_and_zero_iteration_loop_do_not_connect(self):
        root = self.write_pair(
            "from lib import handler\n"
            "if True:\n"
            "    pass\n"
            "else:\n"
            "    handler()\n")
        findings = check_connections(self.context(root), self.spec(self.connection()))
        self.assertEqual(len(findings), 1)
        self.assertIn("no matching use", findings[0].message)
        root.joinpath("app.py").write_text(
            "from lib import handler\n"
            "for item in ():\n"
            "    handler()\n", encoding="utf-8")
        findings = check_connections(self.context(root), self.spec(self.connection()))
        self.assertEqual(len(findings), 1)
        self.assertIn("no matching use", findings[0].message)

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
                self.assertIn("no matching use", findings[0].message)

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
