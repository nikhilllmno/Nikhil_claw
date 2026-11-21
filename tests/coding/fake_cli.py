"""A fake ``claude`` and ``codex`` for the tests of ``sama.coding``.

The tests put two small scripts named ``claude`` and ``codex`` on ``PATH``. They run this file.
It writes the event stream of the real CLI (the shapes are from Claude Code 2.1.330 and Codex
0.171.0) and changes one file in the folder of the request.

``$HOME/fake.json`` controls it: ``{"mode": "ok" | "hang" | "error" | "fail_resume"}``. Each call
appends ``{"cli", "argv", "cwd", "prompt", "env_ok"}`` to ``$HOME/calls.jsonl``.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path


def _config() -> dict[str, str]:
    path = Path(os.environ["HOME"]) / "fake.json"
    return json.loads(path.read_text()) if path.is_file() else {"mode": "ok"}


def _record(cli: str, argv: list[str], prompt: str, env_ok: bool) -> None:
    with (Path(os.environ["HOME"]) / "calls.jsonl").open("a") as handle:
        handle.write(
            json.dumps(
                {"cli": cli, "argv": argv, "cwd": os.getcwd(), "prompt": prompt, "env_ok": env_ok}
            )
            + "\n"
        )


def _say(value: object) -> None:
    print(json.dumps(value), flush=True)


def claude(argv: list[str]) -> int:
    prompt = sys.stdin.read()
    token = os.environ.get("ANTHROPIC_API_KEY", "")
    env_ok = (
        bool(token)
        and token == os.environ.get("SAMA_RUN_TOKEN")
        and os.environ.get("ANTHROPIC_BASE_URL", "").endswith("/anthropic")
        and os.environ.get("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC") == "1"
    )
    _record("claude", argv, prompt, env_ok)
    mode = _config().get("mode", "ok")
    resume = argv[argv.index("--resume") + 1] if "--resume" in argv else None
    if resume and mode == "fail_resume":
        _say(
            {
                "type": "result",
                "subtype": "error_during_execution",
                "is_error": True,
                "errors": [f"No conversation found with session ID: {resume}"],
                "session_id": "x",
            }
        )
        return 1
    session = resume or "11111111-2222-3333-4444-555555555555"
    _say({"type": "system", "subtype": "init", "session_id": session, "model": "m", "tools": []})
    if mode == "hang":
        time.sleep(120)
        return 0
    _say(
        {
            "type": "assistant",
            "parent_tool_use_id": None,
            "session_id": session,
            "message": {
                "content": [
                    {"type": "text", "text": "I will change the file."},
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "Write",
                        "input": {"file_path": "change.txt", "content": "x"},
                    },
                ]
            },
        }
    )
    Path("change.txt").write_text(f"claude: {prompt.strip()}\n")
    _say(
        {
            "type": "user",
            "parent_tool_use_id": None,
            "message": {
                "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_1", "content": "File written"}
                ]
            },
        }
    )
    if mode == "error":
        _say(
            {
                "type": "result",
                "subtype": "error_max_turns",
                "is_error": True,
                "session_id": session,
                "usage": {"input_tokens": 1, "output_tokens": 1},
                "total_cost_usd": 0.001,
            }
        )
        return 1
    _say(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "Done: I changed change.txt.",
            "session_id": session,
            "num_turns": 2,
            "total_cost_usd": 0.0123,
            "usage": {
                "input_tokens": 100,
                "cache_creation_input_tokens": 10,
                "cache_read_input_tokens": 20,
                "output_tokens": 30,
            },
        }
    )
    return 0


def codex(argv: list[str]) -> int:
    prompt = sys.stdin.read()
    env_ok = bool(os.environ.get("SAMA_RUN_TOKEN")) and "OPENAI_API_KEY" not in os.environ
    _record("codex", argv, prompt, env_ok)
    mode = _config().get("mode", "ok")
    resume = argv[argv.index("resume") + 1] if "resume" in argv else None
    thread = resume or "0199a213-81c0-7800-8aa1-bbab2a035a53"
    _say({"type": "thread.started", "thread_id": thread})
    _say({"type": "turn.started"})
    if mode == "hang":
        time.sleep(120)
        return 0
    if mode == "error":
        _say({"type": "error", "message": "stream error: 402 budget_exceeded"})
        _say({"type": "turn.failed", "error": {"message": "402 budget_exceeded"}})
        return 1
    command = {"id": "item_1", "type": "command_execution", "command": "bash -lc ls"}
    _say({"type": "item.started", "item": {**command, "status": "in_progress"}})
    _say(
        {
            "type": "item.completed",
            "item": {**command, "aggregated_output": "a\n", "exit_code": 0, "status": "completed"},
        }
    )
    Path("change.txt").write_text(f"codex: {prompt.strip()}\n")
    _say(
        {
            "type": "item.completed",
            "item": {
                "id": "item_2",
                "type": "file_change",
                "changes": [{"path": "change.txt", "kind": "add"}],
                "status": "completed",
            },
        }
    )
    _say(
        {
            "type": "item.completed",
            "item": {"id": "item_3", "type": "agent_message", "text": "Done: I added change.txt."},
        }
    )
    _say(
        {
            "type": "turn.completed",
            "usage": {"input_tokens": 120, "cached_input_tokens": 20, "output_tokens": 30},
        }
    )
    return 0


if __name__ == "__main__":
    name = sys.argv[1]
    sys.exit(claude(sys.argv[2:]) if name == "claude" else codex(sys.argv[2:]))
