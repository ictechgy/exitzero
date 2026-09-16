"""Minimal newline-delimited JSON-RPC MCP stand-in for gateway tests.

Reads one JSON message per line on stdin. Requests answer with a fixed result;
notifications are ignored. Each executed tools/call appends the tool name to
``called-tools.txt`` in the working directory so tests can prove which calls
actually reached the server. ``FAKE_DIE_AFTER`` env var exits the process
with code 7 after that many messages, simulating a mid-session crash.
"""
import json
import os
import sys


def main() -> int:
    die_after = os.environ.get("FAKE_DIE_AFTER")
    seen = 0
    for line in sys.stdin:
        seen += 1
        if die_after is not None and seen > int(die_after):
            return 7
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(message, dict) or "id" not in message:
            continue
        method = message.get("method")
        if method == "initialize":
            result = {"protocolVersion": "2024-11-05", "capabilities": {}}
        elif method == "tools/list":
            result = {"tools": [{"name": "read_file"}, {"name": "exec_shell"}]}
        elif method == "tools/call":
            name = (message.get("params") or {}).get("name", "?")
            with open("called-tools.txt", "a", encoding="utf-8") as record:
                record.write(name + "\n")
            result = {"content": [{"type": "text", "text": f"ran {name}"}]}
        else:
            result = {}
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": message["id"],
                                     "result": result}) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
