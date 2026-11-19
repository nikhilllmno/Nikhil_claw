from __future__ import annotations

import subprocess
import sys
import textwrap
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from sama.runtime import (
    ApprovalRequired,
    Audience,
    ContextFile,
    ContractViolation,
    FailureCode,
    FakeRuntime,
    McpServerConfig,
    RunCompleted,
    RunConfig,
    RunEvent,
    RunFailed,
    RunRequest,
    RunScope,
    RuntimeKind,
    RuntimeRegistry,
    SkillIndexEntry,
    TextDelta,
    ToolCallStarted,
    ToolSchema,
    UnknownRuntimeError,
    Usage,
    is_terminal,
    run_event_adapter,
    validate_event_stream,
)
from sama.runtime.contract import CONTEXT_FILE_MAX_CHARS, AgentRuntime
from tests.support import make_config, make_request, make_scope

# --- RunScope ---------------------------------------------------------------------------------


def test_private_scope_needs_user() -> None:
    with pytest.raises(ValidationError, match="needs a user_id"):
        RunScope(
            org_id="o", workspace_id="w", session_id="s", run_id="r", audience=Audience.PRIVATE
        )


def test_shared_scope_has_no_user() -> None:
    with pytest.raises(ValidationError, match="runs as the workspace"):
        RunScope(
            org_id="o",
            workspace_id="w",
            session_id="s",
            run_id="r",
            audience=Audience.SHARED,
            user_id="u",
        )
    assert make_scope(shared=True).user_id is None


def test_models_are_frozen_and_reject_unknown_fields() -> None:
    scope = make_scope()
    with pytest.raises(ValidationError):
        scope.run_id = "other"  # type: ignore[misc]
    with pytest.raises(ValidationError, match="extra"):
        RunScope.model_validate({**scope.model_dump(), "typo": 1})


# --- RunConfig.fingerprint ----------------------------------------------------------------------


def test_fingerprint_is_stable_for_the_same_input() -> None:
    assert make_config().fingerprint() == make_config().fingerprint()
    assert len(make_config().fingerprint()) == 64


def test_fingerprint_ignores_dict_key_order() -> None:
    def config(schema: dict[str, object]) -> RunConfig:
        tool = ToolSchema(name="t", description="d", input_schema=schema)
        return RunConfig(runtime=RuntimeKind.FAKE, model="m", tool_schemas=(tool,))

    a = config({"type": "object", "required": ["q"]})
    b = config({"required": ["q"], "type": "object"})
    assert a.fingerprint() == b.fingerprint()


def test_fingerprint_ignores_the_run_token() -> None:
    assert make_config(token="a").fingerprint() == make_config(token="b").fingerprint()


def test_fingerprint_changes_when_the_prefix_changes() -> None:
    base = make_config()
    assert make_config(soul="Other soul.").fingerprint() != base.fingerprint()
    assert make_config(RuntimeKind.CLAUDE_AGENT_SDK).fingerprint() != base.fingerprint()
    more_memory = base.model_copy(update={"memory_snapshot": "New fact."})
    assert more_memory.fingerprint() != base.fingerprint()


def test_fingerprint_is_the_same_in_another_process() -> None:
    """The digest must not depend on Python's per-process hash seed."""
    code = textwrap.dedent(
        """
        from tests.support import make_config
        print(make_config().fingerprint())
        """
    )
    results = set()
    for seed in ("1", "2"):
        out = subprocess.run(
            [sys.executable, "-c", code],
            check=True,
            capture_output=True,
            text=True,
            env={"PYTHONHASHSEED": seed, "PYTHONPATH": "."},
        )
        results.add(out.stdout.strip())
    assert results == {make_config().fingerprint()}


# --- RunConfig other rules ----------------------------------------------------------------------


def test_bearer_token_is_hidden() -> None:
    config = make_config(token="super-secret-token")
    assert "super-secret-token" not in repr(config)
    assert "super-secret-token" not in config.model_dump_json()
    token = config.mcp_servers[0].bearer_token
    assert token is not None
    assert token.get_secret_value() == "super-secret-token"


def test_duplicate_names_are_rejected() -> None:
    tool = ToolSchema(name="t", description="")
    with pytest.raises(ValidationError, match="duplicate tool name: t"):
        RunConfig(runtime=RuntimeKind.FAKE, model="m", tool_schemas=(tool, tool))
    server = McpServerConfig(name="sama", url="https://x")
    with pytest.raises(ValidationError, match="duplicate MCP server name"):
        RunConfig(runtime=RuntimeKind.FAKE, model="m", mcp_servers=(server, server))


def test_context_file_cap() -> None:
    ContextFile(path="a", content="x" * CONTEXT_FILE_MAX_CHARS)
    with pytest.raises(ValidationError):
        ContextFile(path="a", content="x" * (CONTEXT_FILE_MAX_CHARS + 1))


@pytest.mark.parametrize("path", ["", 'a"b', "a>b", "<a", "a\nb", "x" * 201])
def test_a_context_file_path_cannot_close_its_tag(path: str) -> None:
    with pytest.raises(ValidationError):
        ContextFile(path=path, content="x")


def test_render_prefix_orders_the_tiers() -> None:
    config = make_config().model_copy(
        update={"system": "You are Sama.", "environment": "Session start: today."}
    )
    prefix = config.render_prefix()
    positions = [
        prefix.index("You are Sama."),
        prefix.index("You help."),
        prefix.index('<context_file path="AGENTS.md">'),
        prefix.index("- deploy: Deploy to Fly.io."),
        prefix.index("<memory>\nThe user is in IST.\n</memory>"),
        prefix.index("<environment>\nSession start: today.\n</environment>"),
    ]
    assert positions == sorted(positions)
    assert "web_search" not in prefix  # tools go through the SDK, not the text
    assert "run-token" not in prefix


def test_render_prefix_leaves_out_empty_parts() -> None:
    assert RunConfig(runtime=RuntimeKind.FAKE, model="m").render_prefix() == ""
    only_skills = RunConfig(
        runtime=RuntimeKind.FAKE,
        model="m",
        skill_index=(SkillIndexEntry(name="a", description="b"),),
    )
    assert only_skills.render_prefix() == "<skills>\n- a: b\n</skills>"


# --- Usage --------------------------------------------------------------------------------------


def test_usage_adds_up() -> None:
    a = Usage(input_tokens=1, output_tokens=2, cache_read_tokens=3, cost_usd=Decimal("0.10"))
    b = Usage(input_tokens=10, cache_write_tokens=4, cost_usd=Decimal("0.05"))
    total = a + b
    assert total == Usage(
        input_tokens=11,
        output_tokens=2,
        cache_read_tokens=3,
        cache_write_tokens=4,
        cost_usd=Decimal("0.15"),
    )
    assert total.total_tokens == 20


def test_usage_rejects_negative_values() -> None:
    with pytest.raises(ValidationError):
        Usage(input_tokens=-1)


# --- Events -------------------------------------------------------------------------------------


EVENTS: list[RunEvent] = [
    TextDelta(text="hi", usage=Usage(output_tokens=1)),
    ToolCallStarted(call_id="c1", name="web_search", arguments={"q": "x"}),
    ApprovalRequired(approval_id="a1", call_id="c1", tool_name="send", runtime_state=b"\x00\x01"),
    RunCompleted(output="done", usage=Usage(cost_usd=Decimal("0.0123"))),
    RunFailed(code=FailureCode.TIMEOUT, message="slow", retryable=True),
]


@pytest.mark.parametrize("event", EVENTS, ids=lambda e: type(e).__name__)
def test_events_round_trip_through_json(event: RunEvent) -> None:
    raw = run_event_adapter.dump_json(event)
    assert run_event_adapter.validate_json(raw) == event


def test_terminal_events() -> None:
    assert [is_terminal(e) for e in EVENTS] == [False, False, True, True, True]


def test_validate_event_stream_accepts_a_good_stream() -> None:
    done = RunCompleted(output="hi", usage=Usage(output_tokens=1))
    assert validate_event_stream([EVENTS[0], done]) is done


@pytest.mark.parametrize(
    ("events", "message"),
    [
        ([], "no events"),
        ([TextDelta(text="x")], "exactly one terminal"),
        ([RunCompleted(output="a"), RunCompleted(output="b")], "exactly one terminal"),
        ([RunCompleted(output="a"), TextDelta(text="x")], "exactly one terminal"),
        (
            [TextDelta(text="x", usage=Usage(output_tokens=5)), RunCompleted(output="x")],
            "usage went down",
        ),
    ],
)
def test_validate_event_stream_rejects_bad_streams(events: list[RunEvent], message: str) -> None:
    with pytest.raises(ContractViolation, match=message):
        validate_event_stream(events)


# --- Registry -----------------------------------------------------------------------------------


def test_registry() -> None:
    registry = RuntimeRegistry()
    fake = FakeRuntime()
    assert isinstance(fake, AgentRuntime)
    registry.register(fake)
    assert registry.get(RuntimeKind.FAKE) is fake
    assert registry.kinds() == [RuntimeKind.FAKE]
    with pytest.raises(ValueError, match="already registered"):
        registry.register(FakeRuntime())
    with pytest.raises(UnknownRuntimeError):
        registry.get(RuntimeKind.CLAUDE_AGENT_SDK)


# --- immutability of JSON fields and bytes in JSON ----------------------------------------------


def test_tool_schema_cannot_change_after_validation() -> None:
    source: dict[str, Any] = {"type": "object", "properties": {"q": {"type": "string"}}}
    tool = ToolSchema(name="t", description="d", input_schema=source)
    config = RunConfig(runtime=RuntimeKind.FAKE, model="m", tool_schemas=(tool,))
    before = config.fingerprint()
    # A change to the input after validation does not reach the model.
    source["properties"]["q"]["type"] = "integer"
    # A change through the model fails, also in nested values.
    with pytest.raises(TypeError):
        config.tool_schemas[0].input_schema["type"] = "array"  # type: ignore[index]
    with pytest.raises(TypeError):
        config.tool_schemas[0].input_schema["properties"]["q"]["type"] = "integer"
    assert config.fingerprint() == before
    assert config.tool_schemas[0].model_dump()["input_schema"] == {
        "type": "object",
        "properties": {"q": {"type": "string"}},
    }


def test_tool_call_arguments_cannot_change() -> None:
    event = ToolCallStarted(call_id="c1", name="n", arguments={"items": [{"a": 1}]})
    with pytest.raises(TypeError):
        event.arguments["items"][0]["a"] = 2
    assert run_event_adapter.validate_json(run_event_adapter.dump_json(event)) == event


def test_any_runtime_state_bytes_round_trip_through_json() -> None:
    state = bytes(range(256))
    event = RunCompleted(output="x", runtime_state=state)
    parsed = run_event_adapter.validate_json(run_event_adapter.dump_json(event))
    assert isinstance(parsed, RunCompleted)
    assert parsed.runtime_state == state
    request = make_request(runtime_state=b"\xff\x00")
    parsed_request = RunRequest.model_validate_json(request.model_dump_json())
    assert parsed_request.runtime_state == b"\xff\x00"


@pytest.mark.parametrize(
    "value",
    [
        ToolSchema(name="t", description="d").input_schema,
        ToolCallStarted(call_id="c1", name="n").arguments,
        ApprovalRequired(approval_id="a", call_id="c", tool_name="t").arguments,
    ],
    ids=["input_schema", "tool_call_arguments", "approval_arguments"],
)
def test_default_json_fields_are_read_only(value: Any) -> None:
    with pytest.raises(TypeError):
        value["x"] = 1
