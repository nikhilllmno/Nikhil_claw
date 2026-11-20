"""A fake stdio MCP server for the connector host tests (Section 10.3.2). Standard library only.

Tools: ``echo`` (read only), ``has_secret`` (tells if ``DEMO_TOKEN`` is set and its length,
not its value), ``grow`` (uses ``mb`` MB of memory), ``crash`` (exits with code 3), and
``leak`` (an error that holds the value of ``DEMO_TOKEN``, as a careless server can send). A log
line on stdout before ``initialize`` checks that the client skips lines that are not JSON.
"""

from __future__ import annotations

import json
import os
import sys
import time

TOOLS = [
    {
        "name": "echo",
        "description": "Echo the text.",
        "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}},
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "has_secret",
        "description": "Tell if the secret is set.",
        "inputSchema": {"type": "object"},
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "grow",
        "description": "Use memory.",
        "inputSchema": {"type": "object", "properties": {"mb": {"type": "integer"}}},
    },
    {"name": "crash", "description": "Exit.", "inputSchema": {"type": "object"}},
]


def reply(message_id: object, result: object) -> None:
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": message_id, "result": result}) + "\n")
    sys.stdout.flush()


def text(value: str) -> dict[str, object]:
    return {"content": [{"type": "text", "text": value}]}


def main() -> None:
    sys.stdout.write("fake server starting\n")
    sys.stdout.flush()
    hold: list[bytearray] = []
    for line in sys.stdin:
        message = json.loads(line)
        method, message_id = message.get("method"), message.get("id")
        if message_id is None:
            continue
        params = message.get("params") or {}
        if method == "initialize":
            reply(
                message_id,
                {
                    "protocolVersion": params["protocolVersion"],
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "fake", "version": "1"},
                },
            )
        elif method == "tools/list":
            reply(message_id, {"tools": TOOLS})
        elif method == "tools/call":
            name, args = params["name"], params.get("arguments") or {}
            if name == "echo":
                reply(message_id, text(f"echo {args.get('text', '')} pid={os.getpid()}"))
            elif name == "has_secret":
                value = os.environ.get("DEMO_TOKEN", "")
                reply(message_id, text(f"set={bool(value)} length={len(value)}"))
            elif name == "grow":
                for _ in range(int(args.get("mb", 200))):
                    hold.append(bytearray(b"x" * 1_048_576))
                time.sleep(10)
                reply(message_id, text("grew"))
            elif name == "crash":
                sys.exit(3)
            elif name == "leak":
                error = {"code": -2, "message": f"bad token {os.environ.get('DEMO_TOKEN', '')}"}
                sys.stdout.write(
                    json.dumps({"jsonrpc": "2.0", "id": message_id, "error": error}) + "\n"
                )
                sys.stdout.flush()
            else:
                sys.stdout.write(
                    json.dumps(
                        {"jsonrpc": "2.0", "id": message_id, "error": {"code": -1, "message": "?"}}
                    )
                    + "\n"
                )
                sys.stdout.flush()


if __name__ == "__main__":
    main()
