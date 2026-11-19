from __future__ import annotations

import asyncio
import json
import uuid
from datetime import timedelta
from typing import Any

import httpx
from pydantic import SecretStr
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.db.models import Run
from sama.observability.langfuse import LangfuseClient
from sama.observability.traces import TraceExporter
from tests.observability.conftest import add_details, add_run
from tests.orchestrator.conftest import Tenant


class Langfuse:
    """A fake Langfuse that records the traces and answers with ``status``."""

    def __init__(self) -> None:
        self.status = 200
        self.traces: list[dict[str, Any]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.status == 200:
            self.traces.append(json.loads(request.content))
        return httpx.Response(self.status, json={})

    def client(self) -> LangfuseClient:
        return LangfuseClient(
            "https://lf.example.test",
            "pk",
            SecretStr("sk"),
            transport=httpx.MockTransport(self.handler),
        )

    def trace_ids(self) -> list[str]:
        return [
            body["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["traceId"] for body in self.traces
        ]


async def _exported(engine: AsyncEngine, run_id: uuid.UUID) -> bool:
    async with engine.connect() as connection:
        value = await connection.scalar(select(Run.trace_exported_at).where(Run.id == run_id))
    return value is not None


async def test_an_ended_run_is_sent_one_time_with_its_details(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    langfuse = Langfuse()
    exporter = TraceExporter(engine, langfuse.client(), environment="test")
    run_id = await add_run(engine, tenant)
    async with engine.connect() as connection:
        now = await connection.scalar(text("SELECT now()"))
    await add_details(engine, tenant, run_id, now)

    assert await exporter.export_pending() == 1
    assert await exporter.export_pending() == 0
    assert langfuse.trace_ids() == [run_id.hex]
    spans = langfuse.traces[0]["resourceSpans"][0]["scopeSpans"][0]["spans"]
    assert [span["name"] for span in spans] == ["run", "claude-x", "web_search", "compaction"]
    root_attributes = {a["key"]: a["value"] for a in spans[0]["attributes"]}
    assert root_attributes["langfuse.observation.input"] == {"stringValue": "Hi there"}
    assert root_attributes["langfuse.observation.output"] == {"stringValue": "Hello!"}
    assert root_attributes["langfuse.trace.name"] == {"stringValue": "p"}
    assert "secret plans" not in json.dumps(langfuse.traces)
    assert await _exported(engine, run_id)


async def test_open_runs_and_old_runs_wait_or_are_skipped(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    langfuse = Langfuse()
    exporter = TraceExporter(engine, langfuse.client(), max_age=timedelta(hours=1))
    await add_run(engine, tenant, status="running")
    await add_run(engine, tenant, status="paused", session_id=tenant.ids["s2"])
    await add_run(engine, tenant, status="queued", session_id=tenant.ids["s3"])
    old = await add_run(engine, tenant, ended_ago=timedelta(hours=2))
    assert await exporter.export_pending() == 0
    assert langfuse.traces == []
    assert not await _exported(engine, old)


async def test_failed_and_cancelled_runs_are_sent(engine: AsyncEngine, tenant: Tenant) -> None:
    langfuse = Langfuse()
    exporter = TraceExporter(engine, langfuse.client())
    failed = await add_run(engine, tenant, status="failed", failure_code="model_error")
    cancelled = await add_run(engine, tenant, status="cancelled", failure_code="cancelled")
    assert await exporter.export_pending() == 2
    assert set(langfuse.trace_ids()) == {failed.hex, cancelled.hex}
    status = {
        body["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["traceId"]: body["resourceSpans"][0][
            "scopeSpans"
        ][0]["spans"][0]["status"]
        for body in langfuse.traces
    }
    assert status[failed.hex] == {"code": 2, "message": "model_error: it broke"}


async def test_a_down_langfuse_keeps_the_run_for_the_next_pass(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    langfuse = Langfuse()
    exporter = TraceExporter(engine, langfuse.client())
    run_id = await add_run(engine, tenant)
    langfuse.status = 503
    assert await exporter.export_pending() == 0
    assert not await _exported(engine, run_id)
    langfuse.status = 200
    assert await exporter.export_pending() == 1
    assert langfuse.trace_ids() == [run_id.hex]


async def test_a_rejected_trace_is_marked_and_not_sent_again(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    langfuse = Langfuse()
    exporter = TraceExporter(engine, langfuse.client())
    rejected = await add_run(engine, tenant, ended_ago=timedelta(seconds=5))
    good = await add_run(engine, tenant)
    langfuse.status = 400
    assert await exporter.export_pending() == 0
    assert await _exported(engine, rejected)
    assert await _exported(engine, good)
    assert langfuse.traces == []


async def test_two_exporters_send_each_run_one_time(engine: AsyncEngine, tenant: Tenant) -> None:
    langfuse = Langfuse()
    runs = [await add_run(engine, tenant) for _ in range(6)]
    first = TraceExporter(engine, langfuse.client(), batch=10)
    second = TraceExporter(engine, langfuse.client(), batch=10)
    sent = await asyncio.gather(first.export_pending(), second.export_pending())
    assert sum(sent) == 6
    assert sorted(langfuse.trace_ids()) == sorted(run.hex for run in runs)


async def test_serve_stops_on_the_event(engine: AsyncEngine, tenant: Tenant) -> None:
    langfuse = Langfuse()
    run_id = await add_run(engine, tenant)
    stop = asyncio.Event()
    task = asyncio.create_task(
        TraceExporter(engine, langfuse.client()).serve(stop, interval_s=0.05)
    )
    for _ in range(100):
        if langfuse.traces:
            break
        await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, timeout=5)
    assert langfuse.trace_ids() == [run_id.hex]
