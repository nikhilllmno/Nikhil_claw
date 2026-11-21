"""The CLI commands, the request parser, and the event parsers of ``sama.coding``."""

from __future__ import annotations

import json
from decimal import Decimal

from sama.coding.agents import (
    CodingAgent,
    base_runtime,
    cli_command,
    coding_model,
    runtime_of,
)
from sama.coding.events import (
    ClaudeStream,
    CliResult,
    CliSession,
    CliText,
    CliToolEnd,
    CliToolStart,
    CliUsage,
    CodexStream,
)
from sama.coding.runtime import parse_request
from sama.runtime.contract import RuntimeKind

TOKEN = "run-token-value"


def test_claude_code_gets_the_proxy_and_the_run_token_only() -> None:
    command = cli_command(
        CodingAgent.CLAUDE_CODE,
        prompt="fix it",
        model="claude-sonnet-4-6",
        model_url="https://sama.test/",
        token=TOKEN,
        resume="abc",
    )
    assert command.argv[:2] == ("claude", "-p")
    assert command.argv[2:4] == ("--output-format", "stream-json")
    assert command.argv[-2:] == ("--resume", "abc")
    assert command.env["ANTHROPIC_BASE_URL"] == "https://sama.test/anthropic"
    assert command.env["ANTHROPIC_API_KEY"] == TOKEN
    assert command.env["SAMA_RUN_TOKEN"] == TOKEN
    assert command.stdin == "fix it"
    assert TOKEN not in repr(command)
    assert not any(TOKEN in part for part in command.argv)


def test_codex_reads_the_token_from_the_environment() -> None:
    command = cli_command(
        CodingAgent.CODEX,
        prompt="fix it",
        model="gpt-6.1-terra",
        model_url="https://sama.test",
        token=TOKEN,
        resume="t-1",
    )
    argv = command.argv
    assert argv[:3] == ("codex", "exec", "resume")
    assert argv[-2:] == ("t-1", "-")
    fresh = cli_command(
        CodingAgent.CODEX, prompt="x", model="gpt-6.1-terra", model_url="https://s", token=TOKEN
    )
    assert fresh.argv[:3] == ("codex", "exec", "--json")
    assert "analytics.enabled=false" in fresh.argv
    assert "features.plugins=false" in fresh.argv
    provider = next(part for part in argv if part.startswith("model_providers.sama="))
    assert 'base_url = "https://sama.test/openai/v1"' in provider
    assert 'env_key = "SAMA_RUN_TOKEN"' in provider
    assert command.env == {"SAMA_RUN_TOKEN": TOKEN}
    assert not any(TOKEN in part for part in argv)


def test_a_model_of_the_other_provider_falls_back_to_the_default() -> None:
    assert coding_model(CodingAgent.CLAUDE_CODE, "claude-haiku-4-5") == "claude-haiku-4-5"
    assert coding_model(CodingAgent.CODEX, "claude-haiku-4-5") == "gpt-6.1-terra"
    assert coding_model(CodingAgent.CODEX, None) == "gpt-6.1-terra"
    assert runtime_of(CodingAgent.CODEX) is RuntimeKind.CODEX
    assert base_runtime("claude_code") is RuntimeKind.CLAUDE_AGENT_SDK
    assert base_runtime("codex") is RuntimeKind.OPENAI_AGENTS_SDK
    assert base_runtime("fake") is RuntimeKind.FAKE


def test_parse_request() -> None:
    plain = parse_request("fix the README typo")
    assert (plain.repo, plain.prompt, plain.open_pr) == (None, "fix the README typo", False)
    full = parse_request("octo/demo add a test\nfor parse --pr")
    assert (full.repo, full.prompt, full.open_pr) == ("octo/demo", "add a test\nfor parse", True)
    url = parse_request("--pr https://github.com/octo/demo.git fix")
    assert (url.repo, url.prompt, url.open_pr) == ("https://github.com/octo/demo.git", "fix", True)
    # A path is not a repository.
    assert parse_request("src/app.py has a bug").repo is None
    assert parse_request("github.com/vercel/next.js fix").repo == "github.com/vercel/next.js"
    assert parse_request("and/or fix it").repo == "and/or"


def _lines(*values: object) -> list[str]:
    return [json.dumps(value) for value in values]


def test_the_claude_stream() -> None:
    stream = ClaudeStream("claude-sonnet-4-6")
    events = []
    for line in [
        "not json",
        *_lines(
            {"type": "system", "subtype": "init", "session_id": "s-1"},
            {
                "type": "assistant",
                "parent_tool_use_id": None,
                "message": {
                    "content": [
                        {"type": "text", "text": "Looking."},
                        {
                            "type": "tool_use",
                            "id": "t1",
                            "name": "Bash",
                            "input": {"command": "ls -la"},
                        },
                    ]
                },
            },
            {
                "type": "assistant",
                "parent_tool_use_id": "t1",
                "message": {"content": [{"type": "text", "text": "inside a sub-agent"}]},
            },
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "t1",
                            "is_error": True,
                            "content": [{"type": "text", "text": "boom"}],
                        }
                    ]
                },
            },
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": "All done",
                "session_id": "s-1",
                "total_cost_usd": 0.5,
                "usage": {
                    "input_tokens": 1,
                    "output_tokens": 2,
                    "cache_read_input_tokens": 3,
                    "cache_creation_input_tokens": 4,
                },
            },
        ),
    ]:
        events += stream.feed(line)
    assert events[0] == CliSession("s-1")
    assert events[1] == CliText("Looking.")
    assert events[2] == CliToolStart("t1", "Bash", "ls -la")
    assert events[3] == CliToolEnd("t1", True, "boom")
    usage = next(e for e in events if isinstance(e, CliUsage)).usage
    assert (usage.input_tokens, usage.output_tokens) == (1, 2)
    assert (usage.cache_read_tokens, usage.cache_write_tokens) == (3, 4)
    # The cost comes from the price table, not from ``total_cost_usd`` (a whole session).
    assert usage.cost_usd != Decimal("0.5")
    assert events[-1] == CliResult("All done", False, "")


def test_a_claude_error_result() -> None:
    (event,) = [
        e
        for e in ClaudeStream().feed(
            json.dumps({"type": "result", "subtype": "error_max_turns", "is_error": True})
        )
        if isinstance(e, CliResult)
    ]
    assert event.is_error
    assert "error_max_turns" in event.message


def test_the_codex_stream() -> None:
    stream = CodexStream("gpt-6.1-terra")
    events = []
    for line in _lines(
        {"type": "thread.started", "thread_id": "th-1"},
        {"type": "turn.started"},
        {
            "type": "item.started",
            "item": {"id": "i1", "type": "command_execution", "command": "bash -lc ls"},
        },
        {
            "type": "item.completed",
            "item": {
                "id": "i1",
                "type": "command_execution",
                "command": "bash -lc ls",
                "aggregated_output": "x",
                "exit_code": 2,
                "status": "failed",
            },
        },
        {
            "type": "item.completed",
            "item": {
                "id": "i2",
                "type": "file_change",
                "changes": [{"path": "a.py", "kind": "add"}],
            },
        },
        {"type": "item.completed", "item": {"id": "i3", "type": "reasoning", "text": "hmm"}},
        {"type": "item.completed", "item": {"id": "i4", "type": "agent_message", "text": "Done"}},
        {
            "type": "turn.completed",
            "usage": {"input_tokens": 1000, "cached_input_tokens": 400, "output_tokens": 50},
        },
    ):
        events += stream.feed(line)
    assert events[0] == CliSession("th-1")
    assert events[1] == CliToolStart("i1", "Bash", "bash -lc ls")
    assert events[2] == CliToolEnd("i1", True, "x")
    assert events[3] == CliToolStart("i2", "Edit", "add a.py")
    assert events[4] == CliToolEnd("i2", False, "")
    assert events[5] == CliText("Done")
    usage = next(e for e in events if isinstance(e, CliUsage)).usage
    assert (usage.input_tokens, usage.cache_read_tokens, usage.output_tokens) == (600, 400, 50)
    assert usage.cost_usd > 0
    assert events[-1] == CliResult("Done", False)


def test_a_codex_failed_turn() -> None:
    stream = CodexStream("gpt-6.1-terra")
    events = stream.feed(json.dumps({"type": "error", "message": "reconnecting"}))
    assert events == []
    assert stream.last_error == "reconnecting"
    (result,) = stream.feed(json.dumps({"type": "turn.failed", "error": {"message": "402"}}))
    assert result == CliResult("", True, "402")


def test_a_resumed_codex_thread_counts_only_the_new_turn() -> None:
    first = CodexStream("gpt-6.1-terra")
    line = json.dumps(
        {"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 10}}
    )
    first.feed(line)
    assert first.thread_usage is not None
    second = CodexStream("gpt-6.1-terra", baseline=first.thread_usage)
    events = second.feed(
        json.dumps({"type": "turn.completed", "usage": {"input_tokens": 250, "output_tokens": 25}})
    )
    usage = next(e for e in events if isinstance(e, CliUsage)).usage
    assert (usage.input_tokens, usage.output_tokens) == (150, 15)
