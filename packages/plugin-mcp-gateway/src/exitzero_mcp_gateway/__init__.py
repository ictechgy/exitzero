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
from exitzero.services import safe_path


API_VERSION = 1


_GW_SCHEMA_VERSION = 1
_GW_FIELDS = frozenset({"schema_version", "description", "upstream", "allow", "deny"})
_GW_UPSTREAM_FIELDS = frozenset({"command", "args", "cwd", "env"})
_GW_PATTERN_FIELDS = frozenset({"tools"})
_GW_SHUTDOWN_TIMEOUT = 5.0
_GW_JOIN_TIMEOUT = 2.0
_GW_MAX_FRAME = 4 * 1024 * 1024
_GW_MAX_PENDING = 1024


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
        # One snapshot: the audited hash must cover exactly the bytes parsed,
        # so a file swapped between reads cannot desynchronize the two.
        raw = path.read_bytes()
        document = tomllib.loads(raw.decode("utf-8"))
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
        "config_sha256": hashlib.sha256(raw).hexdigest(),
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
            # Unbuffered binary pipes: a raw FileIO has no Python-level buffer
            # lock, so closing it below can never block on a worker's in-flight
            # read the way a buffered text stream can.
            upstream = subprocess.Popen(
                config["argv"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                cwd=config["cwd"], env=config["env"], bufsize=0,
            )
        except OSError as error:
            raise _GatewayError(f"cannot spawn upstream {config['argv']!r}: {error}") from error
        # Cleanup is installed before session_start so an audit failure cannot
        # leave the spawned upstream running.
        try:
            try:
                audit.write("session_start", argv=config["argv"], cwd=str(config["cwd"]),
                            config_sha256=config["config_sha256"], run_id=run_id)
            except OSError as error:
                raise _GatewayError(f"cannot write the audit log: {error}") from error
            return _pump(upstream, config, audit, run_id)
        finally:
            # The pipes are raw FileIO objects: close() is a bare os.close and
            # can never block on a worker's in-flight read/write the way a
            # buffered stream does.  Then reap the child.
            for stream in (upstream.stdout, upstream.stdin):
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass
            if upstream.poll() is None:
                upstream.kill()
                upstream.wait()
    finally:
        audit.close()


_INVALID = object()


def _read_frames(source: Any, side: str) -> Any:
    """Yield newline-delimited frames under a hard per-frame byte cap.

    ``source`` is a raw file descriptor or a binary/text stream.  Reads in
    bounded chunks so an unterminated or oversized frame can never grow the
    buffer past the cap; the cap is measured in UTF-8 bytes, not characters.
    Yields raw bytes; callers decode.
    """
    if isinstance(source, int):
        def read(size: int) -> bytes:
            return os.read(source, size)
    else:
        read = source.read1 if hasattr(source, "read1") else source.read
    buffer = bytearray()
    while True:
        chunk = read(65536)
        if isinstance(chunk, str):  # tolerate text streams (tests)
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
            if newline > _GW_MAX_FRAME:
                raise _GatewayError(
                    f"{side} JSON-RPC frame exceeds {_GW_MAX_FRAME} bytes")
            yield bytes(buffer[:newline])
            del buffer[: newline + 1]
        if len(buffer) > _GW_MAX_FRAME:
            raise _GatewayError(
                f"{side} JSON-RPC frame exceeds {_GW_MAX_FRAME} bytes")


def _pump(upstream: subprocess.Popen, config: dict[str, Any],
          audit: _AuditLog, run_id: str) -> int:
    """Pump both directions; the watch loop notices upstream death on its own."""

    pending: dict[Any, str] = {}
    lock = threading.Lock()
    write_lock = threading.Lock()  # both workers share the client's stdout
    client_done = threading.Event()
    errors: list[BaseException] = []

    def pump_upstream() -> None:
        assert upstream.stdout is not None
        for raw in _read_frames(upstream.stdout, "upstream"):
            try:
                line = raw.decode("utf-8")
            except UnicodeDecodeError:
                audit.write("upstream_nonjson")
                continue
            message = _parse_line(line)
            if message is _INVALID:
                audit.write("upstream_nonjson")
                continue
            # Only a real response (id present, no method, scalar-or-null id)
            # consumes a pending client request; upstream requests reuse ids.
            if (isinstance(message, dict) and "method" not in message
                    and "id" in message
                    and (message["id"] is None
                         or isinstance(message["id"], (str, int)))):
                with lock:
                    method = pending.pop(message["id"], None)
                if method == "tools/list":
                    dropped = _filter_tools_list(message, config)
                    if dropped:
                        audit.write("tools_filtered", dropped=dropped)
                        line = json.dumps(message, ensure_ascii=False)
            _write_client(line, write_lock)

    def pump_client() -> None:
        for raw in _read_frames(_client_input(), "client"):
            try:
                line = raw.decode("utf-8")
            except UnicodeDecodeError:
                audit.write("parse_error")
                _write_client_obj(_error_response(None, -32700, "parse error"),
                                  write_lock)
                continue
            _handle_client_line(line, upstream, config, audit,
                                pending, lock, write_lock)
        client_done.set()

    workers = [
        threading.Thread(target=_guarded, args=(pump_upstream, errors), daemon=True),
        threading.Thread(target=_guarded, args=(pump_client, errors), daemon=True),
    ]
    for worker in workers:
        worker.start()

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
    except (OSError, ValueError):
        pass
    try:
        exit_code = upstream.wait(timeout=_GW_SHUTDOWN_TIMEOUT)
    except subprocess.TimeoutExpired:
        upstream.kill()
        exit_code = upstream.wait()
    # Join the workers before audit.close() so no worker writes or fails into
    # a closing log; a client blocked on stdin stays a daemon and dies at exit.
    for worker in workers:
        worker.join(timeout=_GW_JOIN_TIMEOUT)
    try:
        audit.write("session_end", upstream_exit=exit_code,
                    premature_exit=premature is not None, worker_error=bool(errors))
    except OSError as error:
        raise _GatewayError(f"cannot write the audit log: {error}") from error
    print(f"mcp-gateway: audit log at {audit.path}", file=sys.stderr)
    if errors:
        raise _GatewayError(f"gateway worker failed: {errors[0]!r}")
    if premature is not None:
        raise _GatewayError(f"upstream exited early with code {premature}")
    if exit_code != 0:
        raise _GatewayError(f"upstream exited with code {exit_code}")
    return 0


def _guarded(pump: Any, errors: list[BaseException]) -> None:
    try:
        pump()
    except Exception as error:
        errors.append(error)


def _handle_client_line(line: str, upstream: subprocess.Popen, config: dict[str, Any],
                        audit: _AuditLog, pending: dict[Any, str],
                        lock: threading.Lock, write_lock: threading.Lock) -> None:
    message = _parse_line(line)
    if message is _INVALID or not isinstance(message, dict):
        audit.write("parse_error")
        _write_client_obj(_error_response(None, -32700, "parse error"), write_lock)
        return
    request_id = message.get("id")
    is_request = "method" in message and "id" in message
    if "id" in message and not isinstance(request_id, (str, int, type(None))):
        _write_client_obj(_error_response(None, -32600, "invalid request id"), write_lock)
        return
    if upstream.poll() is not None:
        if is_request:
            _write_client_obj(_error_response(request_id, -32000,
                                              "exitzero: upstream MCP server terminated"),
                              write_lock)
        return
    if is_request:
        with lock:
            # A reused id must not evict an in-flight request (a denied
            # tools/call could otherwise unfilter a pending tools/list), and
            # the map is bounded so unanswered requests cannot exhaust memory.
            if request_id in pending:
                _write_client_obj(_error_response(request_id, -32600,
                                                  "duplicate request id"), write_lock)
                return
            if len(pending) >= _GW_MAX_PENDING:
                _write_client_obj(_error_response(request_id, -32000,
                                                  "too many outstanding requests"), write_lock)
                return
            pending[request_id] = message["method"]
    if message.get("method") == "tools/call":
        params = message.get("params")
        name = params.get("name") if isinstance(params, dict) else None
        if not isinstance(name, str) or not name:
            if is_request:
                with lock:
                    pending.pop(request_id, None)
                _write_client_obj(_error_response(request_id, -32602,
                                                  "tools/call requires a tool name"), write_lock)
            return
        if not _tool_allowed(config, name):
            if is_request:
                with lock:
                    pending.pop(request_id, None)
            audit.write("tool_call", tool=name, decision="deny", request_id=request_id)
            if is_request:
                _write_client_obj(_error_response(request_id, -32000,
                                                  f"exitzero: tool {name!r} is not allowed "
                                                  "by the gateway policy"), write_lock)
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


def _client_input() -> Any:
    """Prefer the raw stdin fd: a daemon worker blocked in ``os.read`` holds
    no buffered-stream lock, so neither cleanup nor interpreter shutdown can
    deadlock closing the stream underneath it."""
    try:
        return sys.stdin.fileno()
    except (AttributeError, OSError):
        return getattr(sys.stdin, "buffer", sys.stdin)


def _write_client(line: str, write_lock: threading.Lock) -> None:
    """Serialize writes to the client so pump and error paths never interleave.

    Prefer ``os.write`` on the raw fd for the same reason as ``_client_input``:
    a worker blocked mid-write must not hold the text wrapper's lock when the
    interpreter closes stdout at shutdown.
    """
    if not line.endswith("\n"):
        line += "\n"
    with write_lock:
        try:
            fd = sys.stdout.fileno()
        except (AttributeError, OSError):
            sys.stdout.write(line)
            sys.stdout.flush()
            return
        view = memoryview(line.encode("utf-8"))
        while len(view):
            view = view[os.write(fd, view):]


def _write_client_obj(message: dict[str, Any], write_lock: threading.Lock) -> None:
    _write_client(json.dumps(message, ensure_ascii=False) + "\n", write_lock)


def _write_upstream(upstream: subprocess.Popen, line: str) -> None:
    """Forward a client frame; a dead pipe is an operational error, not silent loss.

    The pipe is unbuffered, so write() may accept only part of the frame;
    loop until every byte is delivered.
    """
    assert upstream.stdin is not None
    data = (line if line.endswith("\n") else line + "\n").encode("utf-8")
    view = memoryview(data)
    while len(view):
        written = upstream.stdin.write(view)
        if not isinstance(written, int) or written <= 0:
            raise _GatewayError("cannot write to upstream stdin")
        view = view[written:]


__all__ = ["API_VERSION", "mcp_gateway", "register"]
