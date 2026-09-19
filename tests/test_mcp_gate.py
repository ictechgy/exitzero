import json
import select
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "core" / "src"))
sys.path.insert(0, str(ROOT / "packages" / "plugin-verify" / "src"))
sys.path.insert(0, str(ROOT / "packages" / "plugin-mcp-gate" / "src"))

from exitzero_mcp_gate import _tool_result  # noqa: E402


POLICY = """version = 1
plugins = ["exitzero_verify", "exitzero_mcp_gate"]

[[checks]]
id = "syntax"
kind = "python.syntax"
paths = ["src/**/*.py"]
"""


def make_root(tmp: str) -> Path:
    root = Path(tmp)
    (root / "exitzero.toml").write_text(POLICY, encoding="utf-8")
    (root / "src").mkdir()
    return root


def start_server(root: Path) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, str(ROOT / "bin" / "exitzero"), "--root", str(root),
         "plugin", "mcp-gate"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True)


def send(process: subprocess.Popen, message: dict) -> dict:
    process.stdin.write(json.dumps(message) + "\n")
    process.stdin.flush()
    ready, _, _ = select.select([process.stdout], [], [], 10.0)
    if not ready:
        raise AssertionError("mcp-gate produced no response within the timeout")
    return json.loads(process.stdout.readline())


class McpGateSessionTests(unittest.TestCase):
    def test_full_session_pass_and_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            (root / "src" / "broken.py").write_text("def broken(:\n", encoding="utf-8")
            process = start_server(root)
            try:
                hello = send(process, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                       "params": {"protocolVersion": "2024-11-05",
                                                  "capabilities": {},
                                                  "clientInfo": {"name": "t", "version": "0"}}})
                self.assertEqual(hello["result"]["serverInfo"]["name"], "exitzero-mcp-gate")
                self.assertIn("tools", hello["result"]["capabilities"])
                process.stdin.write('{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
                process.stdin.flush()
                listed = send(process, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
                self.assertEqual([t["name"] for t in listed["result"]["tools"]],
                                 ["check_completion"])
                failed = send(process, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                        "params": {"name": "check_completion", "arguments": {}}})
                result = failed["result"]
                self.assertFalse(result["isError"])
                self.assertEqual(result["structuredContent"]["verdict"], "failed")
                self.assertEqual(result["structuredContent"]["exit_code"], 1)
                self.assertIn("NOT verified", result["content"][0]["text"])
                receipt = root / result["structuredContent"]["receipt"]
                self.assertTrue(receipt.is_file())
                self.assertEqual(json.loads(receipt.read_text())["exit_code"], 1)
                (root / "src" / "broken.py").write_text("def broken(): return 2\n",
                                                       encoding="utf-8")
                passed = send(process, {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                                        "params": {"name": "check_completion", "arguments": {}}})
                self.assertEqual(passed["result"]["structuredContent"]["verdict"], "passed")
                self.assertIn("verified", passed["result"]["content"][0]["text"])
            finally:
                process.stdin.close()
                self.assertEqual(process.wait(timeout=10.0), 0)

    def test_protocol_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            process = start_server(root)
            try:
                unknown_tool = send(process, {"jsonrpc": "2.0", "id": 1,
                                              "method": "tools/call",
                                              "params": {"name": "nope"}})
                self.assertEqual(unknown_tool["error"]["code"], -32602)
                unknown_method = send(process, {"jsonrpc": "2.0", "id": 2,
                                                "method": "bogus/method"})
                self.assertEqual(unknown_method["error"]["code"], -32601)
                process.stdin.write("not-json\n")
                process.stdin.flush()
                ready, _, _ = select.select([process.stdout], [], [], 10.0)
                self.assertTrue(ready)
                self.assertEqual(json.loads(process.stdout.readline())["error"]["code"], -32700)
                ping = send(process, {"jsonrpc": "2.0", "id": 3, "method": "ping"})
                self.assertEqual(ping["result"], {})
            finally:
                process.stdin.close()
                self.assertEqual(process.wait(timeout=10.0), 0)

    def test_bad_arguments_and_usage(self):
        process = subprocess.run(
            [sys.executable, str(ROOT / "bin" / "exitzero"), "plugin", "mcp-gate", "extra"],
            cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(process.returncode, 2)


class ToolResultTests(unittest.TestCase):
    def test_operational_error_is_tool_error(self):
        receipt = {"exit_code": 2, "status": "error", "receipt": None,
                   "findings": [], "checks": []}
        result = _tool_result(receipt)
        self.assertTrue(result["isError"])
        self.assertIn("could not run", result["content"][0]["text"])

    def test_missing_receipt_reference_noted(self):
        receipt = {"exit_code": 1, "status": "failed", "receipt": None,
                   "findings": [{"rule": "syntax"}], "checks": [{"id": "s", "status": "failed"}]}
        result = _tool_result(receipt)
        self.assertFalse(result["isError"])
        self.assertIn("persistence failed", result["content"][0]["text"])
        self.assertEqual(result["structuredContent"]["checks"],
                         [{"id": "s", "status": "failed"}])


if __name__ == "__main__":
    unittest.main()
