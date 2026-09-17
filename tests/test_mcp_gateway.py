import json
import os
import select
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "core" / "src"))
sys.path.insert(0, str(ROOT / "packages" / "plugin-mcp-gateway" / "src"))

import exitzero_mcp_gateway as gateway_module  # noqa: E402
from exitzero.api import Context  # noqa: E402
from exitzero_mcp_gateway import _filter_tools_list, _tool_allowed, mcp_gateway  # noqa: E402


POLICY = """version = 1
plugins = ["exitzero_mcp_gateway"]

[[checks]]
id = "noop"
kind = "python.syntax"
paths = ["*.py"]
"""

GATEWAY = """schema_version = 1

[upstream]
command = "{python}"
args = ["{server}"]
{env}

[allow]
tools = ["read_*"]

[deny]
tools = ["*_root"]
"""


def make_context(root: Path) -> Context:
    return Context(root=root, policy={}, policy_path=root / "exitzero.toml")


def write_gateway(root: Path, extra_env: str = "") -> Path:
    (root / "exitzero.toml").write_text(POLICY, encoding="utf-8")
    config = root / "gateway.toml"
    config.write_text(GATEWAY.format(
        python=sys.executable.replace("\\", "/"),
        server=str(ROOT / "tests" / "fake_mcp_server.py").replace("\\", "/"),
        env=extra_env,
    ), encoding="utf-8")
    return config


def start_gateway(root: Path) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, str(ROOT / "bin" / "exitzero"), "--root", str(root),
         "plugin", "mcp-gateway", "--config", "gateway.toml"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True)


def send(process: subprocess.Popen, message: dict) -> dict:
    process.stdin.write(json.dumps(message) + "\n")
    process.stdin.flush()
    return read_response(process)


def read_response(process: subprocess.Popen, timeout: float = 10.0) -> dict:
    ready, _, _ = select.select([process.stdout], [], [], timeout)
    if not ready:
        raise AssertionError("gateway produced no response within the timeout")
    line = process.stdout.readline()
    if not line:
        raise AssertionError("gateway closed stdout unexpectedly")
    return json.loads(line)


def read_audit(root: Path) -> list[dict]:
    logs = list((root / ".exitzero" / "mcp-gateway").glob("audit-*.jsonl"))
    assert len(logs) == 1, f"expected one audit log, found {logs}"
    return [json.loads(line) for line in logs[0].read_text().splitlines()]


class GatewayConfigTests(unittest.TestCase):
    def test_usage_and_config_errors_exit_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context = make_context(root)
            self.assertEqual(mcp_gateway(context, []), 2)
            self.assertEqual(mcp_gateway(context, ["--config"]), 2)
            self.assertEqual(mcp_gateway(context, ["--config", "missing.toml"]), 2)
            self.assertEqual(mcp_gateway(context, ["--unknown"]), 2)
            (root / "bad.toml").write_text('schema_version = "one"\n', encoding="utf-8")
            self.assertEqual(mcp_gateway(context, ["--config", "bad.toml"]), 2)
            (root / "noallow.toml").write_text(
                'schema_version = 1\n[upstream]\ncommand = "x"\n', encoding="utf-8")
            self.assertEqual(mcp_gateway(context, ["--config", "noallow.toml"]), 2)

    def test_audit_path_blocked_exits_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_gateway(root)
            (root / ".exitzero").mkdir()
            (root / ".exitzero" / "mcp-gateway").write_text("blocked\n", encoding="utf-8")
            self.assertEqual(mcp_gateway(make_context(root), ["--config", "gateway.toml"]), 2)

    def test_tool_allowed_deny_wins_and_default_denies(self):
        config = {"allow": ["read_*"], "deny": ["read_root"]}
        self.assertTrue(_tool_allowed(config, "read_file"))
        self.assertFalse(_tool_allowed(config, "read_root"))
        self.assertFalse(_tool_allowed(config, "exec_shell"))

    def test_filter_tools_list_drops_denied(self):
        config = {"allow": ["read_*"], "deny": []}
        message = {"id": 1, "result": {"tools": [{"name": "read_file"},
                                                 {"name": "exec_shell"}, "junk"]}}
        dropped = _filter_tools_list(message, config)
        self.assertEqual(dropped, ["exec_shell"])
        # Non-dict entries are forwarded untouched; only named tools are gated.
        self.assertEqual(message["result"]["tools"], [{"name": "read_file"}, "junk"])


class GatewaySessionTests(unittest.TestCase):
    def test_proxy_allows_denies_filters_and_audits(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_gateway(root)
            process = start_gateway(root)
            try:
                listed = send(process, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
                self.assertEqual([t["name"] for t in listed["result"]["tools"]],
                                 ["read_file"])

                allowed = send(process, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                       "params": {"name": "read_file", "arguments": {}}})
                self.assertIn("result", allowed)

                denied = send(process, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                        "params": {"name": "exec_shell", "arguments": {}}})
                self.assertEqual(denied["error"]["code"], -32000)
            finally:
                process.stdin.close()
                self.assertEqual(process.wait(timeout=10), 0)
                process.stdout.close()

            # The denied call must never have reached the upstream server.
            executed = (root / "called-tools.txt").read_text().split()
            self.assertEqual(executed, ["read_file"])

            events = read_audit(root)
            kinds = [event["event"] for event in events]
            self.assertIn("session_start", kinds)
            self.assertIn("tools_filtered", kinds)
            self.assertIn("session_end", kinds)
            calls = {(e["tool"], e["decision"]) for e in events
                     if e["event"] == "tool_call"}
            self.assertEqual(calls, {("read_file", "allow"), ("exec_shell", "deny")})

    def test_duplicate_members_are_rejected_not_forwarded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_gateway(root)
            process = start_gateway(root)
            try:
                # The parsed view says read_file while a first-wins upstream
                # decoder would execute exec_shell — the line must not forward.
                process.stdin.write(
                    '{"jsonrpc":"2.0","id":1,"method":"tools/call",'
                    '"params":{"name":"exec_shell","name":"read_file"}}\n')
                process.stdin.flush()
                response = read_response(process)
                self.assertEqual(response["error"]["code"], -32700)
            finally:
                process.stdin.close()
                self.assertEqual(process.wait(timeout=10), 0)
                process.stdout.close()
            self.assertFalse((root / "called-tools.txt").exists())

    def test_invalid_id_and_notification_shapes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_gateway(root)
            process = start_gateway(root)
            try:
                # A list-valued id is not a valid JSON-RPC request id.
                bad = send(process, {"jsonrpc": "2.0", "id": [], "method": "ping"})
                self.assertEqual(bad["error"]["code"], -32600)

                # A denied tools/call *notification* gets no response; the next
                # line read must belong to the following request's id.
                process.stdin.write(
                    '{"jsonrpc":"2.0","method":"tools/call",'
                    '"params":{"name":"exec_shell"}}\n')
                listed = send(process, {"jsonrpc": "2.0", "id": 9, "method": "tools/list"})
                self.assertEqual(listed["id"], 9)
            finally:
                process.stdin.close()
                self.assertEqual(process.wait(timeout=10), 0)
                process.stdout.close()

    def test_upstream_crash_exits_2_without_client_eof(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_gateway(root, extra_env='\n[upstream.env]\nFAKE_DIE_AFTER = "1"\n')
            process = start_gateway(root)
            try:
                send(process, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
                # Second message reaches a dead upstream; the watch loop must
                # end the session even though client stdin stays open.
                process.stdin.write('{"jsonrpc":"2.0","id":2,"method":"ping"}\n')
                process.stdin.flush()
                self.assertEqual(process.wait(timeout=10), 2)
            finally:
                for stream in (process.stdin, process.stdout):
                    try:
                        stream.close()
                    except OSError:
                        pass
                if process.poll() is None:
                    process.kill()
                    process.wait()

            events = read_audit(root)
            end = next(e for e in events if e["event"] == "session_end")
            self.assertTrue(end["premature_exit"])

    def test_upstream_nonzero_exit_after_client_eof_is_operational(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_gateway(root, extra_env='\n[upstream.env]\nFAKE_EXIT_CODE = "23"\n')
            process = start_gateway(root)
            try:
                send(process, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
            finally:
                process.stdin.close()
                self.assertEqual(process.wait(timeout=10), 2)
                process.stdout.close()

    def test_oversized_client_frame_ends_the_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_gateway(root)
            process = start_gateway(root)
            try:
                try:
                    process.stdin.write(" " * (5 * 1024 * 1024) + "\n")
                    process.stdin.flush()
                except BrokenPipeError:
                    pass  # the gateway may already have exited on the frame cap
                self.assertEqual(process.wait(timeout=10), 2)
            finally:
                for stream in (process.stdin, process.stdout):
                    try:
                        stream.close()
                    except OSError:
                        pass
                if process.poll() is None:
                    process.kill()
                    process.wait()

    def test_frame_cap_counts_utf8_bytes_not_characters(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_gateway(root)
            process = start_gateway(root)
            try:
                # 1.1M emoji is ~1.1M characters but ~4.4 MB of UTF-8 — the
                # 4 MiB cap is a byte cap, so this frame must end the session.
                frame = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping",
                                    "params": {"blob": "😀" * 1_100_000}},
                                   ensure_ascii=False)
                try:
                    process.stdin.write(frame + "\n")
                    process.stdin.flush()
                except BrokenPipeError:
                    pass  # the gateway may already have exited on the cap
                self.assertEqual(process.wait(timeout=10), 2)
            finally:
                for stream in (process.stdin, process.stdout):
                    try:
                        stream.close()
                    except OSError:
                        pass
                if process.poll() is None:
                    process.kill()
                    process.wait()

    def test_oversized_upstream_frame_ends_the_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_gateway(root, extra_env='\n[upstream.env]\nFAKE_BIG_LINE = "5242880"\n')
            process = start_gateway(root)
            try:
                process.stdin.write('{"jsonrpc":"2.0","id":1,"method":"ping"}\n')
                process.stdin.flush()
                self.assertEqual(process.wait(timeout=10), 2)
            finally:
                for stream in (process.stdin, process.stdout):
                    try:
                        stream.close()
                    except OSError:
                        pass
                if process.poll() is None:
                    process.kill()
                    process.wait()

    def test_duplicate_request_id_is_rejected_not_replaced(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_gateway(root)
            process = start_gateway(root)
            try:
                # The first request stays pending; the duplicate id must get an
                # error rather than evicting the in-flight entry.
                process.stdin.write('{"jsonrpc":"2.0","id":1,"method":"tools/list"}\n')
                process.stdin.write('{"jsonrpc":"2.0","id":1,"method":"tools/list"}\n')
                process.stdin.flush()
                responses = [read_response(process), read_response(process)]
            finally:
                process.stdin.close()
                self.assertEqual(process.wait(timeout=10), 0)
                process.stdout.close()
            errors = [r for r in responses if "error" in r]
            self.assertEqual(len(errors), 1)
            self.assertEqual(errors[0]["error"]["code"], -32600)
            self.assertTrue(any("result" in r for r in responses))

    def test_null_id_request_matches_its_response(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_gateway(root)
            process = start_gateway(root)
            try:
                response = send(process, {"jsonrpc": "2.0", "id": None,
                                          "method": "tools/list"})
                self.assertIsNone(response["id"])
                self.assertEqual([t["name"] for t in response["result"]["tools"]],
                                 ["read_file"])
            finally:
                process.stdin.close()
                self.assertEqual(process.wait(timeout=10), 0)
                process.stdout.close()

    def test_audit_write_failure_reaps_the_upstream_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_gateway(root)
            spawned = []
            real_popen = gateway_module.subprocess.Popen

            def capture(*args, **kwargs):
                process = real_popen(*args, **kwargs)
                spawned.append(process)
                return process

            with mock.patch.object(gateway_module._AuditLog, "write",
                                   side_effect=OSError("disk full")), \
                 mock.patch.object(gateway_module.subprocess, "Popen",
                                   side_effect=capture):
                code = mcp_gateway(make_context(root), ["--config", "gateway.toml"])
            self.assertEqual(code, 2)
            self.assertTrue(spawned, "upstream was never spawned")
            self.assertIsNotNone(spawned[0].poll(),
                                 "audit failure must not leave upstream running")

    def test_pending_requests_are_capped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_gateway(root)
            audit = gateway_module._AuditLog(make_context(root), "cap-test")
            upstream = mock.Mock()
            upstream.poll.return_value = None
            upstream.stdin = mock.Mock()
            pending = {index: "ping" for index in range(gateway_module._GW_MAX_PENDING)}
            sent = []
            lock = threading.Lock()
            try:
                with mock.patch.object(gateway_module, "_write_client_obj",
                                       side_effect=lambda obj, wl: sent.append(obj)):
                    gateway_module._handle_client_line(
                        '{"jsonrpc":"2.0","id":"over","method":"ping"}',
                        upstream, {"allow": ["*"], "deny": []}, audit,
                        pending, lock, lock)
            finally:
                audit.close()
            self.assertEqual(sent[0]["error"]["code"], -32000)
            self.assertEqual(len(pending), gateway_module._GW_MAX_PENDING)


if __name__ == "__main__":
    unittest.main()
