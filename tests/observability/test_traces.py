from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from sama.observability.langfuse import LangfuseClient, LangfuseError, Span
from sama.observability.traces import (
    MAX_TEXT_CHARS,
    MAX_TOOL_CHARS,
    RunRecord,
    build_trace,
    span_id,
)
from sama.vault.redact import default_redactor

T0 = datetime(2026, 11, 19, 1, 0, 0, 123456, tzinfo=UTC)


def _run(**values: Any) -> SimpleNamespace:
    row: dict[str, Any] = {
        "id": uuid.UUID("11111111-2222-3333-4444-555555555555"),
        "org_id": uuid.uuid4(),
        "session_id": uuid.uuid4(),
        "triggered_by_user_id": uuid.uuid4(),
        "profile_name": "helper",
        "runtime": "claude_agent_sdk",
        "status": "completed",
        "attempts": 1,
        "failure_code": None,
        "failure_message": None,
        "created_at": T0,
        "started_at": T0,
        "finished_at": T0 + timedelta(seconds=10),
    }
    return SimpleNamespace(**(row | values))


def _model_call(**values: Any) -> SimpleNamespace:
    row: dict[str, Any] = {
        "call_id": uuid.uuid4(),
        "provider": "anthropic",
        "model": "claude-x",
        "endpoint": "messages",
        "status_code": 200,
        "input_tokens": 10,
        "output_tokens": 5,
        "cache_read_tokens": 2,
        "cache_write_tokens": 1,
        "cache_write_long_tokens": 3,
        "usage_estimated": False,
        "cost_usd": Decimal("0.0125"),
        "duration_ms": 1500,
        "created_at": T0 + timedelta(seconds=2),
    }
    return SimpleNamespace(**(row | values))


def _tool(**values: Any) -> SimpleNamespace:
    row: dict[str, Any] = {
        "call_id": "t1",
        "tool_name": "web_search",
        "decision": "allow",
        "status": "finished",
        "arguments": {"query": "secret"},
        "result_preview": "found-it",
        "created_at": T0 + timedelta(seconds=3),
        "finished_at": T0 + timedelta(seconds=4),
    }
    return SimpleNamespace(**(row | values))


def _by_name(spans: list[Span]) -> dict[str, Span]:
    return {span.name: span for span in spans}


def test_the_root_holds_the_run_and_the_children_point_to_it() -> None:
    run = _run()
    record = RunRecord(
        run=run,
        input_text="Hi",
        output_text="Hello",
        model_calls=[_model_call()],
        tool_calls=[_tool()],
        events=[
            SimpleNamespace(
                seq=4,
                type="approval_required",
                payload={"tool_name": "shell", "arguments": {"cmd": "rm"}},
                created_at=T0,
            )
        ],
    )
    spans = build_trace(record, environment="staging")
    root, generation, tool, approval = spans
    assert root.trace_id == run.id.hex
    assert root.span_id == span_id(run.id, "run")
    assert root.parent_id is None
    assert {s.parent_id for s in spans[1:]} == {root.span_id}
    assert root.attributes["langfuse.observation.type"] == "agent"
    assert root.attributes["langfuse.observation.input"] == "Hi"
    assert root.attributes["langfuse.observation.output"] == "Hello"
    assert root.attributes["langfuse.trace.name"] == "helper"
    assert root.attributes["langfuse.environment"] == "staging"
    assert root.attributes["langfuse.trace.tags"] == ["claude_agent_sdk", "completed"]
    for span in spans:
        assert span.attributes["langfuse.session.id"] == str(run.session_id)
        assert span.attributes["langfuse.user.id"] == str(run.triggered_by_user_id)

    assert generation.name == "claude-x"
    assert generation.end - generation.start == timedelta(milliseconds=1500)
    assert json.loads(str(generation.attributes["langfuse.observation.usage_details"])) == {
        "input": 10,
        "output": 5,
        "cache_read_input_tokens": 2,
        "cache_creation_input_tokens": 4,
    }
    assert json.loads(str(generation.attributes["langfuse.observation.cost_details"])) == {
        "total": 0.0125
    }
    assert tool.attributes["langfuse.observation.type"] == "tool"
    assert approval.name == "approval"
    assert approval.attributes["langfuse.observation.metadata.tool_name"] == "shell"


def test_a_tool_span_has_the_arguments_and_the_output() -> None:
    record = RunRecord(
        run=_run(), tool_calls=[_tool(), _tool(call_id="t2")], tool_outputs={"t1": "found-it"}
    )
    _, first, second = build_trace(record)
    assert json.loads(str(first.attributes["langfuse.observation.input"])) == {"query": "secret"}
    assert first.attributes["langfuse.observation.output"] == "found-it"
    assert "langfuse.observation.output" not in second.attributes
    long = RunRecord(
        run=_run(), tool_calls=[_tool()], tool_outputs={"t1": "y" * (MAX_TOOL_CHARS + 50)}
    )
    output = str(build_trace(long)[1].attributes["langfuse.observation.output"])
    assert len(output) < MAX_TOOL_CHARS + 10
    off = build_trace(record, include_content=False)
    text = json.dumps([dict(span.attributes) for span in off], default=str)
    assert "secret" not in text
    assert "found-it" not in text


def test_the_trace_text_is_redacted() -> None:
    planted = "planted-trace-secret-1"
    token = "sk-" + "t" * 30
    default_redactor().add(planted)
    try:
        record = RunRecord(
            run=_run(status="failed", failure_code="tool_error", failure_message=f"bad {planted}"),
            input_text=f"use {planted}",
            output_text=f"the key is {token}",
            tool_calls=[_tool(arguments={"command": f"curl -H 'k: {planted}'", planted: 1})],
            tool_outputs={"t1": f"echo {planted}"},
        )
        spans = build_trace(record)
    finally:
        default_redactor().forget(planted)
    text = json.dumps([(span.error, dict(span.attributes)) for span in spans], default=str)
    assert planted not in text
    assert token not in text
    assert text.count("[REDACTED]") >= 6


def test_a_failed_run_and_a_failed_call_are_errors() -> None:
    run = _run(status="failed", failure_code="model_error", failure_message="overloaded")
    record = RunRecord(
        run=run,
        model_calls=[_model_call(status_code=529)],
        tool_calls=[_tool(call_id="t1", status="failed"), _tool(call_id="t2", status="denied")],
    )
    root, generation, failed, denied = build_trace(record)
    assert root.error == "model_error: overloaded"
    assert root.attributes["langfuse.observation.level"] == "ERROR"
    assert generation.error == "HTTP 529"
    assert failed.error is not None
    assert denied.error is None
    assert denied.attributes["langfuse.observation.level"] == "WARNING"


def test_long_text_is_cut_and_content_can_be_off() -> None:
    record = RunRecord(run=_run(), input_text="x" * (MAX_TEXT_CHARS + 50), output_text="out")
    root = build_trace(record)[0]
    assert len(str(root.attributes["langfuse.observation.input"])) < MAX_TEXT_CHARS + 10
    root = build_trace(record, include_content=False)[0]
    assert "langfuse.observation.input" not in root.attributes
    assert "langfuse.observation.output" not in root.attributes


def test_the_span_ids_are_stable_and_distinct() -> None:
    record = RunRecord(run=_run(), model_calls=[_model_call(), _model_call()])
    first = [span.span_id for span in build_trace(record)]
    assert first == [span.span_id for span in build_trace(record)]
    assert len(set(first)) == 3
    assert all(len(value) == 16 for value in first)


def _client(handler: Any) -> LangfuseClient:
    return LangfuseClient(
        "https://lf.example.test/",
        "pk-lf-1",
        SecretStr("sk-lf-secret"),
        environment="test",
        transport=httpx.MockTransport(handler),
    )


async def test_the_client_posts_otlp_json_with_basic_auth() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={})

    client = _client(handler)
    record = RunRecord(run=_run(), model_calls=[_model_call()])
    await client.send(build_trace(record))
    await client.aclose()
    [request] = seen
    assert str(request.url) == "https://lf.example.test/api/public/otel/v1/traces"
    assert request.headers["x-langfuse-ingestion-version"] == "4"
    assert request.headers["authorization"].startswith("Basic ")
    body = json.loads(request.content)
    [resource] = body["resourceSpans"]
    assert {"key": "service.name", "value": {"stringValue": "sama"}} in resource["resource"][
        "attributes"
    ]
    root, generation = resource["scopeSpans"][0]["spans"]
    assert root["traceId"] == "11111111222233334444555555555555"
    assert root["startTimeUnixNano"] == "1795050000123456000"
    assert "parentSpanId" not in root
    assert generation["parentSpanId"] == root["spanId"]
    tags = next(a for a in root["attributes"] if a["key"] == "langfuse.trace.tags")
    assert tags["value"] == {
        "arrayValue": {
            "values": [{"stringValue": "claude_agent_sdk"}, {"stringValue": "completed"}]
        }
    }


async def test_nothing_is_sent_for_no_spans() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request")

    await _client(handler).send([])


@pytest.mark.parametrize(
    ("status", "retry"),
    [
        (400, False),
        (413, False),
        (422, False),
        (500, False),
        (502, False),
        (504, False),
        (401, True),
        (403, True),
        (408, True),
        (429, True),
        (503, True),
    ],
)
async def test_only_a_sure_miss_can_be_sent_again(status: int, retry: bool) -> None:
    client = _client(lambda request: httpx.Response(status, text="no"))
    with pytest.raises(LangfuseError) as caught:
        await client.send(build_trace(RunRecord(run=_run())))
    assert caught.value.retry is retry
    assert "sk-lf" not in str(caught.value)


async def test_rejected_spans_are_not_sent_again() -> None:
    client = _client(
        lambda request: httpx.Response(200, json={"partialSuccess": {"rejectedSpans": "2"}})
    )
    with pytest.raises(LangfuseError) as caught:
        await client.send(build_trace(RunRecord(run=_run())))
    assert not caught.value.retry


@pytest.mark.parametrize(
    ("error", "retry"),
    [
        (httpx.ConnectError, True),
        (httpx.ConnectTimeout, True),
        (httpx.ReadTimeout, False),
        (httpx.RemoteProtocolError, False),
    ],
)
async def test_a_transport_error_is_sent_again_only_before_the_request_left(
    error: type[httpx.TransportError], retry: bool
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise error("down", request=request)

    with pytest.raises(LangfuseError) as caught:
        await _client(handler).send(build_trace(RunRecord(run=_run())))
    assert caught.value.retry is retry
