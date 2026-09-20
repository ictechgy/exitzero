"""Real Git/CLI regression cases for the bounded Node test-integrity check."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from exitzero.api import CheckSpec, Context
from exitzero_verify import node_integrity

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "bin/exitzero"
ONE = "test('one', t => { t.is(1, 1); });\n"
TWO = "test('two', t => { t.is(2, 2); });\n"


class NodeIntegrityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                        GIT_AUTHOR_NAME="Test", GIT_AUTHOR_EMAIL="test@example.invalid",
                        GIT_COMMITTER_NAME="Test", GIT_COMMITTER_EMAIL="test@example.invalid")
        self.git("init", "--quiet")
        self.git("config", "core.hooksPath", os.devnull)
        self.policy()

    def policy(self, options="", *, reuse=False, enforcement="block", root=None):
        target = root or self.root
        (target / "exitzero.toml").write_text(
            'version = 1\nplugins = ["exitzero_verify"]\n[[checks]]\n'
            'id = "integrity"\nkind = "node.test-integrity"\n'
            'paths = ["tests/**/*.js", "tests/**/*.ts"]\n'
            f'reuse = {str(reuse).lower()}\nenforcement = "{enforcement}"\n'
            '[checks.options]\n' + options, encoding="utf-8")

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.root), *args],
                                       env=self.env, stderr=subprocess.PIPE, timeout=15).decode().strip()

    def write(self, source=ONE + TWO, path="tests/app.test.js", *, root=None):
        target = (root or self.root) / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source, encoding="utf-8")
        return target

    def commit(self):
        self.git("add", "-A")
        self.git("-c", "commit.gpgsign=false", "commit", "--quiet", "-m", "baseline")
        return self.git("rev-parse", "HEAD")

    def check(self, expected=0, *args, command="check", root=None):
        target = root or self.root
        result = subprocess.run([sys.executable, str(CLI), "--root", str(target), command,
                                 *args, "--format", "json"], env=self.env,
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        receipt = json.loads(result.stdout)
        self.assertEqual(receipt["exit_code"], expected)
        self.assertEqual(json.loads((target / receipt["receipt"]).read_text()), receipt)
        return receipt

    def test_removes_named_case_even_when_another_case_is_added(self):
        self.write()
        self.commit()
        self.write(ONE + "test('three', t => { t.pass(); });\n")
        receipt = self.check(1)
        self.assertIn("removed: 1", receipt["findings"][0]["message"])
        self.assertNotIn("three", json.dumps(receipt["findings"]))

    def test_duplicate_titles_are_counted_and_deleted_files_survive_diff_scope(self):
        path = self.write(ONE + ONE)
        self.commit()
        self.write(ONE)
        self.check(1)
        path.unlink()
        receipt = self.check(1, "--diff", "HEAD")
        self.assertTrue(any("file deleted" in f["message"] for f in receipt["findings"]))
        self.assertEqual(receipt["diff"], "HEAD")

    def test_new_skip_focus_todo_and_node_options_fail(self):
        self.write(ONE)
        self.commit()
        variants = ("test.skip", "test.only", "test.todo", "test.serial.skip", "test['skip']",
                    "test.skipIf(false)", "test.runIf(true)", "test.failing")
        for call in variants:
            with self.subTest(call=call):
                self.write(ONE.replace("test(", call + "("))
                receipt = self.check(1)
                self.assertTrue(any("suppression" in f["message"] for f in receipt["findings"]))
        for field in ("skip: true", "only: true", "todo: 'later'", "skip: condition"):
            with self.subTest(field=field):
                self.write("test('one', {" + field + "}, () => {});\n")
                self.check(1)
        self.write("test('one', {skip: false, options: {skip: true}}, () => {});\n")
        self.check()

    def test_moving_skip_to_another_test_is_not_hidden_by_equal_total(self):
        self.write(ONE.replace("test(", "test.skip(") + TWO)
        self.commit()
        self.check()
        self.write(ONE + TWO.replace("test(", "test.skip("))
        self.check(1)

    def test_new_untracked_suppression_and_alias_reference_are_checked(self):
        self.write()
        self.commit()
        added = self.write("test.skip('new', () => {});\n", "tests/new.test.ts")
        receipt = self.check(1)
        self.assertEqual(receipt["findings"][0]["path"], "tests/new.test.ts")
        added.unlink()
        self.write(ONE + TWO + "const conditionalTest = enabled ? test.skip : test;\n")
        self.check(1)

    def test_callback_skip_todo_and_computed_options_fail(self):
        self.write(ONE)
        self.commit()
        for source in ("test('one', context => { context.skip(); });",
                       "test('one', async (ctx) => { ctx['todo']('later'); });",
                       "test('one', function () { this.skip(); });",
                       "test('one', {['skip']: true}, () => {});",
                       "test('one', {...options}, () => {});"):
            with self.subTest(source=source):
                self.write(source)
                self.check(1)

    def test_keyword_property_does_not_hide_a_skip_call_as_regex(self):
        self.write(ONE)
        self.commit()
        self.write(ONE + "const result = obj.return / test.skip('new', () => {}) / 2;\n")
        receipt = self.check(1)
        self.assertTrue(any("suppression" in f["message"] for f in receipt["findings"]))

    def test_ambiguous_slash_cannot_hide_executable_skip_calls(self):
        self.write(ONE)
        self.commit()
        # Valid JS: 'of' is a contextual keyword that can also name a variable.
        self.write(ONE + "let of = 1; const result = of / test.skip('hidden', () => {}) / 2;\n")
        receipt = self.check(1)
        self.assertTrue(any("Ambiguous slash" in f["message"] for f in receipt["findings"]))
        self.write(ONE + "const value\u200c = 1; const ratio = value\u200c / test.skip('hidden', () => {}) / 2;\n")
        receipt = self.check(1)
        self.assertTrue(any("Unsupported JavaScript token" in f["message"] for f in receipt["findings"]))

    def test_in_scope_file_rename_split_and_body_edits_pass(self):
        original = self.write()
        self.commit()
        original.rename(original.with_name("renamed.test.js"))
        self.check()
        original.with_name("renamed.test.js").unlink()
        self.write(ONE.replace("1, 1", "true, true"), "tests/first.test.ts")
        self.write(TWO, "tests/second.test.js")
        self.check()

    def test_moving_tests_out_of_scope_fails(self):
        original = self.write()
        self.commit()
        moved = self.root / "src/app.js"
        moved.parent.mkdir()
        original.rename(moved)
        self.check(1)

    def test_comments_strings_regex_and_template_examples_are_not_tests(self):
        self.write(ONE)
        self.commit()
        self.write(ONE + r'''
// test.skip('example', () => {});
/* it.only('example', () => {}); */
const text = "test.todo('example')";
const regex = /test.skip\('example'\)[/]/g;
const template = `test.skip('example') ${`nested ${"it.only('example')"}`}`;
if (ready) /test.skip('example')/.test(text);
const ratio = 8 / 2 / 2;
''')
        self.check()

    def test_quotes_escapes_each_and_typescript_annotations(self):
        self.write("test.each([[1]])('one', (x: number) => {});\n", "tests/math.test.ts")
        self.commit()
        self.write('test.each([[1], [2]])(`\\u006fne`, (x: number): void => {});\n', "tests/math.test.ts")
        self.check()
        self.write("test.skip.each([[1]])('one', (x: number) => {});\n", "tests/math.test.ts")
        self.check(1)

    def test_test_hooks_are_not_counted_as_test_cases(self):
        self.write(ONE + "test.beforeEach(t => {});\ntest.after.always(t => {});\n")
        self.commit()
        self.write(ONE + "test.beforeEach.skip(t => {});\n")
        self.check()

    def test_configured_alias_and_nested_project_root(self):
        project = self.root / "project"
        project.mkdir()
        self.policy('functions = ["verify"]\n', root=project)
        self.write("verify('case', () => {});\n", root=project)
        self.write("not valid JS", "tests/unrelated.js")
        self.commit()
        self.check(root=project)
        self.write("verify.skip('case', () => {});\n", root=project)
        self.check(1, root=project)

    def test_unsupported_or_unreadable_source_never_silently_passes(self):
        self.write(ONE)
        self.commit()
        for source in ("test(prefix + 'one', () => {});", "test[`sk${mode}`]('one', () => {});",
                       "/* unterminated", "test('one', () => <View />);", "const x = {} / divisor;"):
            with self.subTest(source=source):
                self.write(source)
                receipt = self.check(1)
                self.assertTrue(any("cannot be analyzed" in f["message"] for f in receipt["findings"]))

    def test_missing_baseline_bad_options_and_reuse_fail_closed(self):
        self.write()
        self.check(2)
        self.commit()
        for setting in ('base = "missing"', 'base = "--help"', 'functions = []',
                        'functions = ["test", "test"]', 'unexpected = true'):
            with self.subTest(setting=setting):
                self.policy(setting)
                self.check(2)
        self.policy(reuse=True)
        self.check(2, command="lint-config")

    def test_moved_baseline_cannot_reuse_an_old_pass(self):
        self.policy('base = "reference"\n')
        self.write(ONE)
        first = self.commit()
        self.git("update-ref", "refs/heads/reference", first)
        self.check()
        self.write(ONE + TWO)
        second = self.commit()
        self.write(ONE)
        self.check()
        self.git("update-ref", "refs/heads/reference", second)
        receipt = self.check(1, "--reuse")
        self.assertEqual(receipt["checks"][0]["status"], "failed")

    def test_advisory_policy_keeps_observed_failure_and_empty_diff_passes(self):
        self.policy(enforcement="warn")
        self.write()
        self.commit()
        self.check(0, "--diff", "HEAD")
        self.write(ONE)
        receipt = self.check()
        self.assertEqual(receipt["checks"][0]["status"], "failed")
        self.assertFalse(receipt["checks"][0]["blocking"])

    def test_baseline_ref_movement_during_execution_fails(self):
        self.write()
        self.commit()
        real_revision = node_integrity.revision(self.root, "HEAD")
        context = Context(self.root, {}, self.root / "exitzero.toml")
        spec = CheckSpec("integrity", "node.test-integrity", ("tests/**/*.js",), {}, reuse=False)
        with mock.patch.object(node_integrity, "revision", side_effect=[real_revision, "0" * 40]):
            with self.assertRaisesRegex(ValueError, "baseline changed"):
                node_integrity.check_integrity(context, spec)


if __name__ == "__main__":
    unittest.main()
