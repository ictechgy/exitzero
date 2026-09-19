"""exitzero completion-gate MCP server.

Hosts without a blocking stop hook — Windsurf-class IDEs, headless runners —
register this server so the agent itself calls ``check_completion`` before
declaring a task done. ``exitzero plugin mcp-gate`` serves newline-delimited
JSON-RPC over stdio, the same framing the exitzero MCP gateway proxies.

The server exposes exactly one tool:

- ``check_completion`` runs the project's real ``check`` gate and returns the
  verdict plus the persisted receipt reference. A gate violation is a
  successful tool result with a negative verdict (the tool worked); only
  operational failures (exit 2) surface as tool errors.

Nothing here replaces hooks: an agent that never calls the tool simply never
gets verified, which is why hook-capable hosts should prefer ``hooks install``.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

from exitzero.api import Context
from exitzero.services import run_gate

API_VERSION = 1

_PROTOCOL_VERSION = "2024-11-05"
_SERVER_INFO = {"name": "exitzero-mcp-gate", "version": "1.0.0"}
_MAX_FRAME = 4 * 1024 * 1024

_TOOL = {
    "name": "check_completion",
    "description": (
        "Verify task completion against the project's exitzero gate. Call this "
        "before claiming work is done: it runs the project's configured checks "
        "and returns the verdict with a persisted receipt reference. A "
        "'failed' verdict means checks found violations; inspect the receipt "
        "and repair instead of declaring completion."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
}


def register(registry: Any) -> None:
    """Register the stdio completion-gate MCP server command."""

    registry.add_command("mcp-gate", mcp_gate)


def mcp_gate(context: Context, argv: list[str]) -> int:
    """Serve the completion gate over newline-delimited stdio JSON-RPC."""

    if argv:
        print("mcp-gate: this command takes no arguments", file=sys.stderr)
        return 2
    policy_name = context.policy_path.relative_to(context.root).as_posix()
    try:
        _serve(context, policy_name)
    except _GateError as error:
        print(f"mcp-gate: {error}", file=sys.stderr)
        return 2
    return 0


class _GateError(Exception):
    """An operational server failure that must exit 2."""


def _read_frames(source: Any) -> Any:
    """Yield newline-delimited frames under a hard per-frame byte cap."""
    read = source.read1 if hasattr(source, "read1") else source.read
    buffer = bytearray()
    while True:
        chunk = read(65536)
        if isinstance(chunk, str):
            chunk = chunk.encode("utf-8")
        if not chunk:
            if buffer:
                yield bytes(buffer)
            return
        buffer += chunk
        while True:
            newline = buffer.find(b"\n")
            if newline < 0:
                break
            if newline > _MAX_FRAME:
                raise _GateError(f"client JSON-RPC frame exceeds {_MAX_FRAME} bytes")
            yield bytes(buffer[:newline])
            del buffer[: newline + 1]
        if len(buffer) > _MAX_FRAME:
            raise _GateError(f"client JSON-RPC frame exceeds {_MAX_FRAME} bytes")


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject duplicate members so a re-parsed line cannot change meaning."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate object member: {key}")
        result[key] = value
    return result


def _result(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id,
            "error": {"code": code, "message": message}}


def _tool_result(receipt: dict[str, Any]) -> dict[str, Any]:
    """Render one gate receipt as an MCP tools/call result.

    ``isError`` is reserved for operational failures (exit 2): a failed gate
    is a successful verdict response the agent should act on, not a tool
    fault.
    """
    exit_code = receipt["exit_code"]
    receipt_ref = receipt.get("receipt")
    location = f"Receipt: {receipt_ref}." if isinstance(receipt_ref, str) else \
        "Receipt unavailable: persistence failed."
    if exit_code == 0:
        text = f"exitzero: completion verified — all checks passed. {location}"
    elif exit_code == 1:
        rules = sorted({f.get("rule", "?") for f in receipt.get("findings", [])
                        if isinstance(f, dict)})
        text = ("exitzero: completion NOT verified — the gate found violations "
                f"({', '.join(rules)}). {location} Inspect the receipt and repair; "
                "do not declare completion.")
    else:
        text = ("exitzero: the gate could not run (operational error). "
                f"{location} Fix policy or plugin settings and retry.")
    return {
        "content": [{"type": "text", "text": text}],
        "isError": exit_code == 2,
        "structuredContent": {
            "verdict": receipt["status"],
            "exit_code": exit_code,
            "receipt": receipt_ref,
            "checks": [
                {"id": check.get("id"), "status": check.get("status")}
                for check in receipt.get("checks", []) if isinstance(check, dict)
            ],
        },
    }


def _handle(message: dict[str, Any], context: Context, policy_name: str) -> dict[str, Any] | None:
    """Answer one JSON-RPC message; notifications return None."""
    request_id = message.get("id")
    method = message.get("method")
    if "id" not in message:
        return None  # notifications are accepted and ignored
    if not isinstance(request_id, (str, int, type(None))) or not isinstance(method, str):
        return _error(request_id if isinstance(request_id, (str, int)) else None,
                      -32600, "invalid request")
    if method == "initialize":
        return _result(request_id, {
            "protocolVersion": _PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": _SERVER_INFO,
        })
    if method == "ping":
        return _result(request_id, {})
    if method == "tools/list":
        return _result(request_id, {"tools": [_TOOL]})
    if method == "tools/call":
        params = message.get("params")
        name = params.get("name") if isinstance(params, dict) else None
        if name != _TOOL["name"]:
            return _error(request_id, -32602, f"unknown tool: {name!r}")
        try:
            receipt = run_gate(context.root, policy_name, "check")
        except Exception as error:  # gate raised before producing a receipt
            return _error(request_id, -32000,
                          f"exitzero: gate invocation failed ({type(error).__name__})")
        return _result(request_id, _tool_result(receipt))
    return _error(request_id, -32601, f"method not found: {method}")


def _serve(context: Context, policy_name: str) -> None:
    """Sequential request loop: the session ends when the client closes stdin."""
    stdout = sys.stdout.buffer if hasattr(sys.stdout, "buffer") else sys.stdout
    for raw in _read_frames(sys.stdin.buffer if hasattr(sys.stdin, "buffer") else sys.stdin):
        try:
            line = raw.decode("utf-8")
            message = json.loads(line, object_pairs_hook=_strict_object)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            response = _error(None, -32700, "parse error")
        else:
            if not isinstance(message, dict):
                response = _error(None, -32600, "request must be a JSON object")
            else:
                try:
                    response = _handle(message, context, policy_name)
                except Exception as error:
                    response = _error(message.get("id"), -32000,
                                      f"exitzero: internal error ({type(error).__name__})")
        if response is not None:
            stdout.write(json.dumps(response, ensure_ascii=False).encode("utf-8") + b"\n")
            stdout.flush()
