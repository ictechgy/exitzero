"""Advisory policy must preserve failures and never suppress execution errors."""
import io
import json
import os
from contextlib import redirect_stdout
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from exitzero.api import Finding, Registry
from exitzero.cli import emit, _sarif
from exitzero.hooks import cursor_response, stop_block_response
from exitzero.policy import parse_policy, sync_agents
from exitzero.runner import run


class EnforcementTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / 'example.py').write_text('value = 1\n')
        self.configure()

    def configure(self, mode='warn', extra='', kind='python.syntax', options=''):
        text = ('version = 1\nplugins = ["exitzero_verify", "exitzero_harness"]\n'
                '[[checks]]\nid = "sample"\n' + f'kind = "{kind}"\nenforcement = "{mode}"\n'
                'paths = ["example.py"]\n' + options + extra)
        (self.root / 'exitzero.toml').write_text(text)
        sync_agents(self.root, parse_policy(text))

    def gate(self, result=None, **kwargs):
        registry = Registry()
        handler = Mock(return_value=[Finding('sample', 'Violation')] if result is None else result)
        registry.add_check('python.syntax', handler)
        with patch('exitzero.runner.discover', return_value=registry):
            receipt = run(self.root, 'exitzero.toml', 'check', **kwargs)
        self.assertEqual(json.loads((self.root / receipt['receipt']).read_text()), receipt)
        return receipt

    def test_warn_keeps_observed_failure_and_block_uses_exit_one(self):
        warned = self.gate()
        self.assertEqual(warned['exit_code'], 0)
        self.assertEqual(warned['checks'][0]['status'], 'failed')
        self.assertEqual(warned['checks'][0]['enforcement'], 'warn')
        self.assertFalse(warned['checks'][0]['blocking'])
        self.assertEqual(warned['findings'][0]['severity'], 'warning')
        self.assertIn('warning only', (self.root / 'AGENTS.md').read_text())
        self.configure('block')
        blocked = self.gate()
        self.assertEqual(blocked['exit_code'], 1)
        self.assertTrue(blocked['checks'][0]['blocking'])
        self.assertEqual(blocked['checks'][0]['status'], 'failed')

    def test_requirement_is_failed_despite_advisory_exit_zero(self):
        self.configure(extra='\n[[requirements]]\nid = "done"\ndescription = "Done"\nchecks = ["sample"]\n')
        failed = self.gate()
        self.assertEqual(failed['exit_code'], 0)
        self.assertEqual(failed['requirements'][0]['status'], 'failed')
        self.assertEqual(failed['findings'][-1]['severity'], 'warning')
        passed = self.gate(result=[])
        self.assertEqual(passed['requirements'][0]['status'], 'checks_passed')

    def test_mixed_requirement_still_blocks(self):
        self.configure(extra='\n[[checks]]\nid = "strict"\nkind = "python.syntax"\npaths = ["example.py"]\n'
                       '\n[[requirements]]\nid = "done"\ndescription = "Done"\nchecks = ["sample", "strict"]\n')
        receipt = self.gate()
        self.assertEqual(receipt['exit_code'], 1)
        self.assertEqual(receipt['requirements'][0]['status'], 'failed')
        self.assertEqual(receipt['findings'][-1]['severity'], 'error')

    def test_advisory_failures_are_not_reused(self):
        self.gate()
        repeated = self.gate(reuse=True)
        self.assertEqual(repeated['checks'][0]['status'], 'failed')
        self.assertNotIn('reused_from', repeated['checks'][0])
        self.gate(result=[])
        cached = self.gate(reuse=True)
        self.assertEqual(cached['checks'][0]['status'], 'reused')
        self.assertEqual(cached['checks'][0]['enforcement'], 'warn')

    def test_diff_preserves_enforcement(self):
        with patch('exitzero.runner._diff_changed_files', return_value=['example.py']):
            receipt = self.gate(diff='base')
        self.assertEqual(receipt['exit_code'], 0)
        self.assertEqual(receipt['checks'][0]['status'], 'failed')
        self.assertEqual(receipt['checks'][0]['enforcement'], 'warn')

    def test_mutating_inputs_and_exceptions_still_block(self):
        registry = Registry()
        def mutate(context, spec):
            (context.root / 'example.py').write_text('value = 2\n')
            return [Finding(spec.id, 'Advisory violation')]
        registry.add_check('python.syntax', mutate)
        with patch('exitzero.runner.discover', return_value=registry):
            changed = run(self.root, 'exitzero.toml', 'check')
        self.assertEqual(changed['exit_code'], 1)
        self.assertIn('core.inputs-changed', {f['rule'] for f in changed['findings']})
        registry.checks['python.syntax'] = Mock(side_effect=ValueError('private detail'))
        with patch('exitzero.runner.discover', return_value=registry):
            error = run(self.root, 'exitzero.toml', 'check')
        self.assertEqual(error['exit_code'], 2)
        self.assertNotIn('private detail', json.dumps(error))
        with patch('exitzero.runner.persist', side_effect=OSError):
            lost = run(self.root, 'exitzero.toml', 'check')
        self.assertEqual(lost['exit_code'], 2)
        self.assertIsNone(lost['receipt'])

    def test_command_start_and_timeout_failures_cannot_be_advisory(self):
        commands = [(['./missing-program'], 1), ([sys.executable, '-c', 'import time; time.sleep(10)'], 0.01)]
        if os.name == 'posix':
            commands.append(([sys.executable, '-c', 'import os, signal; os.kill(os.getpid(), signal.SIGTERM)'], 1))
        for argv, timeout in commands:
            with self.subTest(argv=argv):
                self.configure(kind='command', options='[checks.options]\nargv = ' + json.dumps(argv) + f'\ntimeout = {timeout}\n')
                receipt = run(self.root, 'exitzero.toml', 'check')
                self.assertEqual(receipt['exit_code'], 1)
                check = next(c for c in receipt['checks'] if c['id'] == 'sample')
                self.assertTrue(check['blocking'])
                self.assertEqual(receipt['findings'][0]['category'], 'execution')

    def test_normal_command_failure_can_be_advisory(self):
        self.configure(kind='command', options='[checks.options]\nargv = ["{python}", "-c", "raise SystemExit(1)"]\n')
        receipt = run(self.root, 'exitzero.toml', 'check')
        self.assertEqual(receipt['exit_code'], 0)
        self.assertEqual(receipt['checks'][-1]['status'], 'failed')

    def test_invalid_policy_or_plugin_category_fails_closed(self):
        path = self.root / 'exitzero.toml'
        path.write_text(path.read_text().replace('"warn"', '"ignore"'))
        self.assertEqual(run(self.root, 'exitzero.toml', 'check')['exit_code'], 2)
        self.configure()
        invalid = self.gate([Finding('sample', 'Execution failed', severity='warning', category='execution')])
        self.assertEqual(invalid['exit_code'], 2)

    def test_human_sarif_and_hook_views_preserve_policy_meaning(self):
        from exitzero_mcp_gate import _tool_result
        receipt = self.gate()
        output = io.StringIO()
        with redirect_stdout(output):
            emit(receipt, 'human')
        self.assertIn('Advisory failures (not blocking): sample', output.getvalue())
        self.assertEqual(_sarif(receipt)['runs'][0]['results'][0]['level'], 'warning')
        self.assertEqual(stop_block_response(receipt), {})
        self.assertEqual(cursor_response(receipt, 'beforeShellExecution', {})['permission'], 'allow')
        mcp = _tool_result(receipt)
        self.assertNotIn('all checks passed', mcp['content'][0]['text'])
        self.assertIn('advisory failures', mcp['content'][0]['text'])
        self.assertEqual(mcp['structuredContent']['checks'][0]['status'], 'failed')
        self.assertFalse(mcp['structuredContent']['checks'][0]['blocking'])


if __name__ == '__main__':
    unittest.main()
