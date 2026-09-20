import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

import exitzero_verify
from exitzero.api import CheckSpec, Context
from exitzero_verify import check_command, check_imports, check_syntax, check_test_quality, register


class VerifyPluginTests(unittest.TestCase):
    def context(self, root: Path) -> Context:
        return Context(root=root, policy={}, policy_path=root / "exitzero.toml")

    def spec(self, kind: str, paths=("**/*.py",), options=None) -> CheckSpec:
        return CheckSpec("test", kind, tuple(paths), options or {})

    def test_registers_all_v1_checks(self):
        from exitzero.api import Registry

        registry = Registry()
        register(registry)
        self.assertEqual(
            set(registry.checks),
            {"python.syntax", "python.imports", "python.test-quality",
             "python.test-integrity", "node.test-integrity", "command"},
        )
        self.assertEqual(registry.check_inputs, {"python.imports": exitzero_verify.import_inputs,
                                                "node.test-integrity": exitzero_verify.node_integrity_inputs})

    def test_syntax_reports_invalid_python(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "bad.py").write_text("def broken(:\n", encoding="utf-8")
            findings = check_syntax(self.context(root), self.spec("python.syntax", ("bad.py",)))
            self.assertEqual(len(findings), 1)
            self.assertEqual(findings[0].path, "bad.py")

    def test_syntax_passes_valid_python(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "ok.py").write_text("value = 1\n", encoding="utf-8")
            self.assertEqual(check_syntax(self.context(root), self.spec("python.syntax", ("ok.py",))), [])

    def test_syntax_requires_explicit_paths_and_rejects_options(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "ok.py").write_text("value = 1\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                check_syntax(self.context(root), CheckSpec("test", "python.syntax"))
            with self.assertRaises(ValueError):
                check_syntax(self.context(root), self.spec("python.syntax", ("ok.py",), {"strict": True}))

    def test_imports_resolves_local_symbols_and_reports_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "lib.py").write_text("value = 1\nclass Known: pass\n", encoding="utf-8")
            (root / "main.py").write_text(
                "from lib import Known\nfrom lib import Missing\nimport lib\nlib.nope\n", encoding="utf-8"
            )
            findings = check_imports(self.context(root), self.spec("python.imports", ("main.py",)))
            messages = "\n".join(f.message for f in findings)
            self.assertIn("Missing", messages)
            self.assertIn("lib.nope", messages)
            self.assertEqual(sum("Known" in f.message for f in findings), 0)

    def test_imports_handles_classes_in_selected_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "lib.py").write_text("value = 1\n", encoding="utf-8")
            (root / "main.py").write_text("import lib\nclass Holder: pass\nlib.nope\n", encoding="utf-8")
            findings = check_imports(self.context(root), self.spec("python.imports", ("main.py",)))
            self.assertEqual(len(findings), 1)
            self.assertIn("lib.nope", findings[0].message)

    def test_imports_reports_syntax_errors_and_resolves_package_relative_import(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pkg").mkdir()
            (root / "pkg/__init__.py").write_text("from .api import Known\n", encoding="utf-8")
            (root / "pkg/api.py").write_text("class Known: pass\n", encoding="utf-8")
            clean = self.spec("python.imports", ("pkg/__init__.py",))
            self.assertEqual(check_imports(self.context(root), clean), [])
            (root / "broken.py").write_text("def broken(:\n", encoding="utf-8")
            broken = self.spec("python.imports", ("broken.py",))
            findings = check_imports(self.context(root), broken)
            self.assertEqual(len(findings), 1)
            self.assertIn("syntax", findings[0].message.lower())

    def test_imports_allow_modules_and_stdlib_without_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "side_effect.py").write_text("raise RuntimeError('must not execute')\n", encoding="utf-8")
            (root / "main.py").write_text("import json\nimport side_effect\nimport permitted\n", encoding="utf-8")
            spec = self.spec("python.imports", ("main.py",), {"allow_modules": ["permitted"]})
            findings = check_imports(self.context(root), spec)
            self.assertEqual([f.message for f in findings], [])

    def test_imports_accepts_non_identifier_root_and_skips_shadowed_module(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "my-src"
            source.mkdir()
            (source / "lib.py").write_text("value = 1\n", encoding="utf-8")
            (source / "main.py").write_text("import lib\nlib = object()\nlib.nope\n", encoding="utf-8")
            spec = self.spec("python.imports", ("my-src/main.py",), {"roots": ["my-src"]})
            self.assertEqual(check_imports(self.context(root), spec), [])

    def test_import_input_provider_shares_root_and_option_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src").mkdir()
            context = self.context(root)
            provider = exitzero_verify.import_inputs
            self.assertEqual(provider(context, self.spec("python.imports")), ["**/*.py"])
            self.assertEqual(provider(context, self.spec("python.imports", options={"roots": ["src", "."]})),
                             ["src/**/*.py", "**/*.py"])
            for options in ({"roots": "src"}, {"roots": [1]}, {"roots": [""]},
                            {"roots": ["missing"]}, {"allow_modules": "json"},
                            {"allow_modules": ["not a module"]}, {"unknown": True}):
                with self.subTest(options=options):
                    spec = self.spec("python.imports", options=options)
                    with self.assertRaises(ValueError):
                        provider(context, spec)
                    with self.assertRaises(ValueError):
                        check_imports(context, spec)

    def test_imports_validates_options(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for options in ({"roots": "src"}, {"allow_modules": "json"}, {"roots": ["../outside"]}):
                with self.assertRaises(ValueError):
                    check_imports(self.context(root), self.spec("python.imports", options=options))

    def test_imports_skips_importerror_guarded_optional_dependencies(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "main.py").write_text(
                "try:\n"
                "    import optional_dep\n"
                "    from other_dep import helper\n"
                "except ImportError:\n"
                "    optional_dep = None\n"
                "try:\n"
                "    import bare_dep\n"
                "except:\n"
                "    pass\n"
                "try:\n"
                "    import tuple_dep\n"
                "except (ImportError, OSError):\n"
                "    pass\n"
                "try:\n"
                "    import required_dep\n"
                "except OSError:\n"
                "    pass\n"
                "import unguarded_dep\n",
                encoding="utf-8",
            )
            findings = check_imports(self.context(root), self.spec("python.imports", ("main.py",)))
            messages = "\n".join(f.message for f in findings)
            self.assertNotIn("optional_dep", messages)
            self.assertNotIn("other_dep", messages)
            self.assertNotIn("bare_dep", messages)
            self.assertNotIn("tuple_dep", messages)
            self.assertIn("required_dep", messages)
            self.assertIn("unguarded_dep", messages)

    def test_imports_guard_does_not_cross_function_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "main.py").write_text(
                "try:\n"
                "    def later():\n"
                "        import missing_dependency\n"
                "except ImportError:\n"
                "    pass\n"
                "try:\n"
                "    class Holder:\n"
                "        import class_scoped_dep\n"
                "except ImportError:\n"
                "    pass\n",
                encoding="utf-8",
            )
            findings = check_imports(self.context(root), self.spec("python.imports", ("main.py",)))
            messages = "\n".join(f.message for f in findings)
            self.assertIn("missing_dependency", messages)
            self.assertNotIn("class_scoped_dep", messages)

    def test_imports_guard_keeps_local_attribute_checks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "localmod.py").write_text("value = 1\n", encoding="utf-8")
            (root / "main.py").write_text(
                "try:\n"
                "    import localmod\n"
                "except ImportError:\n"
                "    pass\n"
                "localmod.nonexistent\n",
                encoding="utf-8",
            )
            findings = check_imports(self.context(root), self.spec("python.imports", ("main.py",)))
            self.assertTrue(any("localmod.nonexistent" in f.message for f in findings))

    def test_imports_module_not_found_guard_keeps_symbol_findings(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "lib.py").write_text("value = 1\n", encoding="utf-8")
            (root / "main.py").write_text(
                "try:\n"
                "    from lib import Missing\n"
                "except ModuleNotFoundError:\n"
                "    pass\n"
                "try:\n"
                "    from lib import AlsoMissing\n"
                "except ImportError:\n"
                "    pass\n",
                encoding="utf-8",
            )
            findings = check_imports(self.context(root), self.spec("python.imports", ("main.py",)))
            messages = "\n".join(f.message for f in findings)
            self.assertIn("'Missing'", messages)
            self.assertNotIn("AlsoMissing", messages)

    def test_imports_try_star_and_qualified_handlers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "main.py").write_text(
                "try:\n"
                "    import starred_dep\n"
                "except* ImportError:\n"
                "    pass\n"
                "try:\n"
                "    import qualified_dep\n"
                "except builtins.ImportError:\n"
                "    pass\n",
                encoding="utf-8",
            )
            self.assertEqual(
                check_imports(self.context(root), self.spec("python.imports", ("main.py",))), [])

    def test_imports_overlapping_roots_keep_first_name_precedence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src/pkg").mkdir(parents=True)
            (root / "src/pkg/__init__.py").write_text("", encoding="utf-8")
            (root / "src/pkg/api.py").write_text("from .. import helper\n", encoding="utf-8")
            (root / "src/helper.py").write_text("value = 1\n", encoding="utf-8")
            spec = self.spec("python.imports", ("src/pkg/api.py",), {"roots": ["src", "."]})
            findings = check_imports(self.context(root), spec)
            self.assertTrue(any("Relative import" in f.message for f in findings))

    def test_imports_parse_each_module_once_per_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "lib.py").write_text("def real():\n    pass\n", encoding="utf-8")
            (root / "a.py").write_text("import lib\nlib.missing\n", encoding="utf-8")
            (root / "b.py").write_text("import lib\nlib.alsomissing\n", encoding="utf-8")
            calls = []
            real = exitzero_verify._module_symbols
            with mock.patch.object(exitzero_verify, "_module_symbols",
                                   side_effect=lambda path: (calls.append(path), real(path))[1]):
                findings = check_imports(self.context(root), self.spec("python.imports"))
            self.assertEqual(len(calls), 1)
            self.assertEqual(len(findings), 2)

    def test_test_quality_reports_empty_and_vacuous_tests(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "test_bad.py").write_text(
                textwrap.dedent(
                    """
                    def test_empty():
                        pass
                    def test_true():
                        assert True
                    def test_constant():
                        assert 1 == 1
                    """
                ),
                encoding="utf-8",
            )
            findings = check_test_quality(self.context(root), self.spec("python.test-quality", ("test_bad.py",)))
            self.assertGreaterEqual(len(findings), 3)

    def test_test_quality_reports_no_tests(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "test_none.py").write_text("VALUE = 1\n", encoding="utf-8")
            findings = check_test_quality(self.context(root), self.spec("python.test-quality", ("test_none.py",)))
            self.assertTrue(any("no test" in f.message.lower() for f in findings))

    def test_test_quality_reports_syntax_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "test_invalid.py").write_text("def test_broken(:\n", encoding="utf-8")
            findings = check_test_quality(self.context(root), self.spec("python.test-quality", ("test_invalid.py",)))
            self.assertEqual(len(findings), 1)
            self.assertEqual(findings[0].rule, "test")
            self.assertIn("syntax", findings[0].message.lower())

    def test_test_quality_passes_meaningful_test(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "test_good.py").write_text(
                "def test_value():\n    result = 1 + 1\n    assert result == 2\n", encoding="utf-8"
            )
            self.assertEqual(check_test_quality(self.context(root), self.spec("python.test-quality", ("test_good.py",))), [])

    def test_command_uses_argv_and_python_expansion(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec = self.spec("command", options={"argv": ["{python}", "-c", "import sys; sys.exit(0)"]})
            self.assertEqual(check_command(self.context(root), spec), [])

    def test_command_reports_nonzero_and_validates_options(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bad = self.spec("command", options={"argv": ["{python}", "-c", "raise SystemExit(3)"]})
            findings = check_command(self.context(root), bad)
            self.assertEqual(len(findings), 1)
            self.assertIn("failed", findings[0].message.lower())
            for options in ({"argv": "python"}, {"argv": []}, {"argv": ["sh", "-c", "echo x"], "timeout": 0}):
                with self.assertRaises(ValueError):
                    check_command(self.context(root), self.spec("command", options=options))

    def test_command_timeout_is_generic(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec = self.spec("command", options={"argv": [sys.executable, "-c", "import time; time.sleep(2)"], "timeout": 0.05})
            findings = check_command(self.context(root), spec)
            self.assertEqual(len(findings), 1)
            self.assertIn("timed out", findings[0].message.lower())
            self.assertNotIn("sleep", findings[0].message)


if __name__ == "__main__":
    unittest.main()
