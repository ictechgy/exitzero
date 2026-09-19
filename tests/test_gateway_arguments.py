"""Configured argument constraints deny before forwarding and do not log values."""
import json
from pathlib import Path
import tempfile
import unittest

from exitzero_mcp_gateway.arguments import allowed_arguments, load_rules
from test_mcp_gateway import write_gateway, start_gateway, send, read_audit


class ArgumentPolicyTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / 'src').mkdir()
        self.rules = [{'tool': 'read_file', 'keys': ['path', 'mode'], 'paths': {'path': ['src']},
                       'enums': {'mode': ['text']}}]

    def allowed(self, values):
        config = {'cwd': self.root, 'argument_rules': load_rules(self.rules, self.root)}
        return allowed_arguments(config, 'read_file', values)

    def test_paths_types_and_unknown_arguments_fail_closed(self):
        self.assertTrue(self.allowed({'path': 'src/a.py', 'mode': 'text'}))
        self.assertTrue(self.allowed({'path': str(self.root / 'src/a.py'), 'mode': 'text'}))
        for path in ('src/../.env', 'src/.env', '../src/a.py', '/etc/passwd', 'src/$HOME', 'src/%2e%2e/a', '~/a'):
            with self.subTest(path=path):
                self.assertFalse(self.allowed({'path': path, 'mode': 'text'}))
        for args in ({}, {'path': 'src/a.py'}, {'path': ['src/a.py'], 'mode': 'text'},
                     {'path': 'src/a.py', 'mode': 'binary'}, {'path': 'src/a.py', 'mode': 'text', 'override': True}, []):
            self.assertFalse(self.allowed(args))
        (self.root / 'src/link').symlink_to(self.root.parent, target_is_directory=True)
        self.assertFalse(self.allowed({'path': 'src/link/a.py', 'mode': 'text'}))

    def test_exact_https_origins_do_not_allow_host_or_port_confusion(self):
        rules = load_rules([{'tool': 'fetch', 'keys': ['url'], 'origins': {'url': ['https://api.example.com']}}], self.root)
        config = {'cwd': self.root, 'argument_rules': rules}
        self.assertTrue(allowed_arguments(config, 'fetch', {'url': 'https://API.example.com:443/path?q=1'}))
        for url in ('http://api.example.com', 'https://api.example.com.evil.invalid',
                    'https://api.example.com@evil.invalid', 'https://user@api.example.com',
                    'https://api.example.com:444/x', 'https://api.example.com:0/x', 'https://api.example.com\\@evil.invalid',
                    ' https://api.example.com', 'https://%61pi.example.com'):
            self.assertFalse(allowed_arguments(config, 'fetch', {'url': url}))

    def test_invalid_rules_rejected_before_startup(self):
        invalid = [None, {}, [{'tool': '*', 'keys': []}],
                   [{'tool': 'x', 'keys': ['path'], 'paths': {'path': ['../outside']}}],
                   [{'tool': 'x', 'keys': [], 'enums': {'extra': ['a']}}],
                   [{'tool': 'x', 'keys': ['url'], 'origins': {'url': ['https://example.com/path']}}]]
        for values in invalid:
            with self.subTest(values=values), self.assertRaises(ValueError):
                load_rules(values, self.root)

    def test_rejected_calls_never_reach_upstream_and_values_are_not_audited(self):
        path = write_gateway(self.root)
        path.write_text(path.read_text() + '\n[[argument_rules]]\ntool = "read_file"\nkeys = ["path", "mode"]\n'
                        '[argument_rules.paths]\npath = ["src"]\n[argument_rules.enums]\nmode = ["text"]\n')
        process = start_gateway(self.root)
        try:
            denied = send(process, {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                                   'params': {'name': 'read_file', 'arguments': {'path': '../private-value-marker', 'mode': 'text'}}})
            self.assertEqual(denied['error']['code'], -32000)
            self.assertFalse((self.root / 'called-tools.txt').exists())
            accepted = send(process, {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call',
                                     'params': {'name': 'read_file', 'arguments': {'path': 'src/a.py', 'mode': 'text'}}})
            self.assertIn('result', accepted)
        finally:
            process.stdin.close()
            self.assertEqual(process.wait(timeout=10), 0)
        self.assertEqual((self.root / 'called-tools.txt').read_text(), 'read_file\n')
        audit = read_audit(self.root)
        self.assertIn('argument-policy', json.dumps(audit))
        self.assertNotIn('private-value-marker', json.dumps(audit))
        self.assertNotIn('src/a.py', json.dumps(audit))


if __name__ == '__main__':
    unittest.main()
