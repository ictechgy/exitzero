"""Local stdio MCP gateway: a thin proxy with an explicit tool allowlist.

``exitzero plugin mcp-gateway --config PATH`` spawns one upstream MCP server
as a subprocess and proxies newline-delimited JSON-RPC between the client on
its own stdio and the server.  ``tools/call`` requests are authorized against
allow/deny glob patterns — deny-by-default, deny wins — and ``tools/list``
responses drop tools the client may not call.  Every decision is appended to
an audit log under ``.exitzero/mcp-gateway/``.  The gateway never inspects
tool arguments beyond the name and never touches the network.
"""

from __future__ import annotations

from datetime import datetime, timezone
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import tomllib
from typing import Any
import uuid

from exitzero.api import Context
from exitzero.files import safe_path


API_VERSION = 1


_GW_SCHEMA_VERSION = 1
_GW_FIELDS = frozenset({"schema_version", "description", "upstream", "allow", "deny"})
_GW_UPSTREAM_FIELDS = frozenset({"command", "args", "cwd", "env"})
_GW_PATTERN_FIELDS = frozenset({"tools"})
_GW_SHUTDOWN_TIMEOUT = 5.0


def register(registry: Any) -> None:
    """Register the stdio MCP gateway command."""

    registry.add_command("mcp-gateway", mcp_gateway)


def mcp_gateway(context: Context, argv: list[str]) -> int:
    """Proxy stdio JSON-RPC to one upstream MCP server under a tool allowlist.

    ``--config PATH`` selects a TOML document inside the repository:

    .. code-block:: toml

        schema_version = 1

        [upstream]
        command = "python3"
        args = ["-m", "example_server"]

        [allow]
        tools = ["read_*"]
        [deny]
        tools = ["exec_*"]    # optional; deny always wins

    Only ``tools/call`` is gated; every other message is forwarded verbatim.
    Denials answer the client with a JSON-RPC error and are never forwarded.
    The session ends when the client closes stdin; a clean session exits 0,
    while configuration, spawn, audit or upstream failures exit 2.
    """

    try:
        config_path = _gateway_config_path(context, argv)
        config = _load_gateway_config(context, config_path)
    except ValueError as error:
        print(f"mcp-gateway: {error}", file=sys.stderr)
        return 2
    try:
        return _serve(context, config)
    except _GatewayError as error:
        print(f"mcp-gateway: {error}", file=sys.stderr)
        return 2


class _GatewayError(Exception):
    """An operational gateway failure that must exit 2."""


def _gateway_config_path(context: Context, argv: list[str]) -> Path:
    config: str | None = None
    index = 0
    while index < len(argv):
        argument = argv[index]
        if argument == "--config":
            if index + 1 >= len(argv):
                raise ValueError("--config requires a path")
            config = argv[index + 1]
            index += 2
        elif argument.startswith("--config="):
            config = argument.split("=", 1)[1]
            index += 1
        else:
            raise ValueError(f"unknown argument: {argument}")
    if config is None or not config.strip():
        raise ValueError("usage: mcp-gateway --config PATH")
    try:
        path = safe_path(context.root, config)
    except (OSError, ValueError, TypeError) as error:
        raise ValueError(f"config path is not allowed: {config}") from error
    if not path.is_file():
        raise ValueError(f"config file does not exist: {config}")
    return path


def _load_gateway_config(context: Context, path: Path) -> dict[str, Any]:
    try:
        document = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as error:
        raise ValueError(f"gateway config cannot be parsed: {error}") from error
    if not isinstance(document, dict) or set(document) - _GW_FIELDS:
        raise ValueError("gateway config has unknown fields")
    version = document.get("schema_version")
    if type(version) is not int or version != _GW_SCHEMA_VERSION:
        raise ValueError(f"gateway schema_version must be {_GW_SCHEMA_VERSION}")
    if "description" in document and not isinstance(document["description"], str):
        raise ValueError("gateway description must be a string")

    upstream = document.get("upstream")
    if not isinstance(upstream, dict) or set(upstream) - _GW_UPSTREAM_FIELDS:
        raise ValueError("gateway upstream must be a table of command/args/cwd/env")
    command = upstream.get("command")
    if not isinstance(command, str) or not command.strip():
        raise ValueError("upstream.command must be a non-empty string")
    args = upstream.get("args", [])
    if not isinstance(args, list) or any(not isinstance(arg, str) for arg in args):
        raise ValueError("upstream.args must be a list of strings")
    cwd = context.root
    if "cwd" in upstream:
        if not isinstance(upstream["cwd"], str) or not upstream["cwd"].strip():
            raise ValueError("upstream.cwd must be a repo-relative path")
        try:
            cwd = safe_path(context.root, upstream["cwd"])
        except (OSError, ValueError, TypeError) as error:
            raise ValueError("upstream.cwd is not allowed") from error
        if not cwd.is_dir():
            raise ValueError("upstream.cwd does not exist")
    env = upstream.get("env", {})
    if not isinstance(env, dict) or any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in env.items()
    ):
        raise ValueError("upstream.env must be a string map")

    allow = _load_patterns(document, "allow", required=True)
    deny = _load_patterns(document, "deny", required=False)
    return {
        "argv": [command, *args],
        "cwd": cwd,
        "env": {**os.environ, **env},
        "allow": allow,
        "deny": deny,
        "config_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def _load_patterns(document: dict[str, Any], table: str, required: bool) -> list[str]:
    section = document.get(table)
    if section is None:
        if required:
            raise ValueError(f"gateway config requires an [{table}] table")
        return []
    if not isinstance(section, dict) or set(section) - _GW_PATTERN_FIELDS:
        raise ValueError(f"gateway [{table}] must be a table of tools")
    tools = section.get("tools")
    if not isinstance(tools, list) or any(
        not isinstance(pattern, str) or not pattern.strip() for pattern in tools
    ):
        raise ValueError(f"gateway [{table}].tools must be a list of non-empty strings")
    return list(tools)


def _tool_allowed(config: dict[str, Any], name: str) -> bool:
    """Deny wins, then allow: a tool with no allow match is denied by default."""

    if any(fnmatch.fnmatchcase(name, pattern) for pattern in config["deny"]):
        return False
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in config["allow"])


def _error_response(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id,
            "error": {"code": code, "message": message}}


def _filter_tools_list(message: dict[str, Any], config: dict[str, Any]) -> list[str]:
    """Remove denied tools from a tools/list result; return the dropped names."""

    result = message.get("result")
    if not isinstance(result, dict) or not isinstance(result.get("tools"), list):
        return []
    dropped: list[str] = []
    kept: list[Any] = []
    for tool in result["tools"]:
        name = tool.get("name") if isinstance(tool, dict) else None
        if isinstance(name, str) and not _tool_allowed(config, name):
            dropped.append(name)
        else:
            kept.append(tool)
    if dropped:
        result["tools"] = kept
    return dropped


class _AuditLog:
    """Append-only JSONL audit evidence under .exitzero/mcp-gateway/."""

    def __init__(self, context: Context, run_id: str):
        try:
            self._path = safe_path(context.root, f".exitzero/mcp-gateway/audit-{run_id}.jsonl")
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._file = self._path.open("a", encoding="utf-8")
        except (OSError, ValueError) as error:
            raise _GatewayError(f"cannot open the audit log: {error}") from error
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._path

    def write(self, event: str, **fields: Any) -> None:
        record = {"ts": datetime.now(timezone.utc).isoformat(), "event": event, **fields}
        with self._lock:
            self._file.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
            self._file.flush()

    def close(self) -> None:
        with self._lock:
            self._file.close()


def _serve(context: Context, config: dict[str, Any]) -> int:
    """Run the proxy session until the client disconnects or upstream dies."""

    run_id = uuid.uuid4().hex
    audit = _AuditLog(context, run_id)
    try:
        try:
            upstream = subprocess.Popen(
                config["argv"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                cwd=config["cwd"], env=config["env"], text=True, bufsize=1,
            )
        except OSError as error:
            raise _GatewayError(f"cannot spawn upstream {config['argv']!r}: {error}") from error
        audit.write("session_start", argv=config["argv"], cwd=str(config["cwd"]),
                    config_sha256=config["config_sha256"], run_id=run_id)
        try:
            return _pump(upstream, config, audit, run_id)
        finally:
            for stream in (upstream.stdin, upstream.stdout):
                try:
                    stream.close()
                except OSError:
                    pass
            if upstream.poll() is None:
                upstream.kill()
                upstream.wait()
    finally:
        audit.close()


_INVALID = object()


def _pump(upstream: subprocess.Popen, config: dict[str, Any],
          audit: _AuditLog, run_id: str) -> int:
    """Pump both directions; the watch loop notices upstream death on its own."""

    pending: dict[Any, str] = {}
    lock = threading.Lock()
    client_done = threading.Event()
    errors: list[BaseException] = []

    def pump_upstream() -> None:
        assert upstream.stdout is not None
        for line in upstream.stdout:
            message = _parse_line(line)
            if message is _INVALID:
                audit.write("upstream_nonjson")
                continue
            # Only a real response (id present, no method, scalar id) consumes
            # a pending client request; upstream requests reuse ids freely.
            if (isinstance(message, dict) and "method" not in message
                    and isinstance(message.get("id"), (str, int))):
                with lock:
                    method = pending.pop(message["id"], None)
                if method == "tools/list":
                    dropped = _filter_tools_list(message, config)
                    if dropped:
                        audit.write("tools_filtered", dropped=dropped)
                        line = json.dumps(message, ensure_ascii=False) + "\n"
            _write_client(line)

    def pump_client() -> None:
        for line in sys.stdin:
            _handle_client_line(line, upstream, config, audit, pending, lock)
        client_done.set()

    threading.Thread(target=_guarded, args=(pump_upstream, errors), daemon=True).start()
    threading.Thread(target=_guarded, args=(pump_client, errors), daemon=True).start()

    premature: int | None = None
    while not client_done.wait(0.05):
        exit_code = upstream.poll()
        if exit_code is not None:
            premature = exit_code
            break
        if errors:
            break
    # A client-EOF win can still hide an upstream that died first; check once
    # more before we initiate the graceful stdin close.
    if premature is None:
        premature = upstream.poll()
    try:
        upstream.stdin.close()
    except OSError:
        pass
    try:
        exit_code = upstream.wait(timeout=_GW_SHUTDOWN_TIMEOUT)
    except subprocess.TimeoutExpired:
        upstream.kill()
        exit_code = upstream.wait()
    audit.write("session_end", upstream_exit=exit_code,
                premature_exit=premature is not None, worker_error=bool(errors))
    print(f"mcp-gateway: audit log at {audit.path}", file=sys.stderr)
    if errors:
        raise _GatewayError(f"gateway worker failed: {errors[0]!r}")
    if premature is not None:
        raise _GatewayError(f"upstream exited early with code {premature}")
    return 0


def _guarded(pump: Any, errors: list[BaseException]) -> None:
    try:
        pump()
    except Exception as error:
        errors.append(error)


def _handle_client_line(line: str, upstream: subprocess.Popen, config: dict[str, Any],
                        audit: _AuditLog, pending: dict[Any, str],
                        lock: threading.Lock) -> None:
    message = _parse_line(line)
    if message is _INVALID or not isinstance(message, dict):
        audit.write("parse_error")
        _write_client_obj(_error_response(None, -32700, "parse error"))
        return
    request_id = message.get("id")
    is_request = "method" in message and "id" in message
    if "id" in message and not isinstance(request_id, (str, int, type(None))):
        _write_client_obj(_error_response(None, -32600, "invalid request id"))
        return
    if upstream.poll() is not None:
        if is_request:
            _write_client_obj(_error_response(request_id, -32000,
                                              "exitzero: upstream MCP server terminated"))
        return
    if is_request:
        with lock:
            pending[request_id] = message["method"]
    if message.get("method") == "tools/call":
        params = message.get("params")
        name = params.get("name") if isinstance(params, dict) else None
        if not isinstance(name, str) or not name:
            if is_request:
                with lock:
                    pending.pop(request_id, None)
                _write_client_obj(_error_response(request_id, -32602,
                                                  "tools/call requires a tool name"))
            return
        if not _tool_allowed(config, name):
            if is_request:
                with lock:
                    pending.pop(request_id, None)
            audit.write("tool_call", tool=name, decision="deny", request_id=request_id)
            if is_request:
                _write_client_obj(_error_response(request_id, -32000,
                                                  f"exitzero: tool {name!r} is not allowed "
                                                  "by the gateway policy"))
            return
        audit.write("tool_call", tool=name, decision="allow", request_id=request_id)
    _write_upstream(upstream, line)


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject duplicate members; forwarding a line that parses differently
    upstream would bypass the allowlist check."""

    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate object member: {key}")
        result[key] = value
    return result


def _parse_line(line: str) -> Any:
    try:
        return json.loads(line, object_pairs_hook=_strict_object)
    except (json.JSONDecodeError, ValueError):
        return _INVALID


def _write_client(line: str) -> None:
    sys.stdout.write(line if line.endswith("\n") else line + "\n")
    sys.stdout.flush()


def _write_client_obj(message: dict[str, Any]) -> None:
    _write_client(json.dumps(message, ensure_ascii=False) + "\n")


def _write_upstream(upstream: subprocess.Popen, line: str) -> None:
    assert upstream.stdin is not None
    try:
        upstream.stdin.write(line if line.endswith("\n") else line + "\n")
        upstream.stdin.flush()
    except OSError:
        pass


__all__ = ["API_VERSION", "mcp_gateway", "register"]
