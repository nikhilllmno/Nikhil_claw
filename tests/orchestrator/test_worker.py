"""Tests of the run worker with the fake runtime (docs/architecture.md, Section 4.3)."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.db.models import Message, Run, RunEvent
from sama.gate.service import GateService
from sama.orchestrator.queue import RunQueue
from sama.orchestrator.worker import Worker
from sama.runtime.contract import (
    ApprovalRequired,
    ResumeRequest,
    RunCompleted,
    RunRequest,
    RuntimeRegistry,
    TextDelta,
    Usage,
)
from sama.runtime.fake import FakeRuntime, FakeScript
from tests.orchestrator.conftest import Tenant, add_tenant


class Harness:
    def __init__(self, engine: AsyncEngine, **worker: Any) -> None:
        self.engine = engine
        self.queue = RunQueue(engine, lease=worker.pop("lease", timedelta(seconds=30)))
        self.runtime = FakeRuntime()
        registry = RuntimeRegistry()
        registry.register(self.runtime)
        self.ended: list[tuple[str, str]] = []
        worker.setdefault("flush_s", 0.0)
        self.worker = Worker(
            self.queue,
            registry,
            owner="w1",
            on_run_end=[lambda org, run: self.ended.append((org, run))],
            **worker,
        )

    async def send(self, tenant: Tenant, session: str, text: str) -> uuid.UUID:
        return await self.queue.enqueue(
            org_id=tenant.org, session_id=tenant.ids[session], text=text, user_id=tenant.ids["mia"]
        )

    async def run(self, run_id: uuid.UUID) -> Any:
        async with self.engine.connect() as connection:
            return (await connection.execute(select(Run).where(Run.id == run_id))).one()

    async def status_is(self, run_id: uuid.UUID, status: str) -> bool:
        return bool((await self.run(run_id)).status == status)

    async def ended_count(self, count: int) -> bool:
        return len(self.ended) >= count

    async def events(self, run_id: uuid.UUID) -> list[tuple[int, str]]:
        async with self.engine.connect() as connection:
            rows = await connection.execute(
                select(RunEvent.seq, RunEvent.type)
                .where(RunEvent.run_id == run_id)
                .order_by(RunEvent.seq)
            )
            return [(row.seq, row.type) for row in rows]


async def until(check: Callable[[], Awaitable[bool]], timeout_s: float = 20.0) -> None:
    for _ in range(int(timeout_s / 0.02)):
        if await check():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("the condition did not become true")


@pytest.fixture
def harness(engine: AsyncEngine) -> Harness:
    return Harness(engine)


def approval_script() -> FakeScript:
    paused = ApprovalRequired(
        approval_id="call-9",
        call_id="call-9",
        tool_name="Bash",
        arguments={"command": "rm -rf build"},
        reason="deletes files",
        runtime_state=b"at-the-pause",
        usage=Usage(input_tokens=2),
    )
    return FakeScript(
        events=(TextDelta(text="let me ", usage=Usage(input_tokens=1)), paused),
        on_approve=(RunCompleted(output="deleted", usage=Usage(input_tokens=5)),),
        on_reject=(RunCompleted(output="kept", usage=Usage(input_tokens=5)),),
    )


async def test_a_run_streams_events_and_stores_the_reply(harness: Harness, tenant: Tenant) -> None:
    run_id = await harness.send(tenant, "s1", "hello there")
    assert await harness.worker.run_once()
    assert not await harness.worker.run_once()
    row = await harness.run(run_id)
    assert (row.status, row.usage["output_tokens"]) == ("completed", 3)
    events = await harness.events(run_id)
    assert [seq for seq, _ in events] == list(range(4))
    assert [kind for _, kind in events] == ["text_delta"] * 3 + ["run_completed"]
    async with harness.engine.connect() as connection:
        reply = (
            await connection.execute(
                select(Message.content).where(Message.run_id == run_id, Message.role == "assistant")
            )
        ).scalar_one()
    assert reply == {"text": "echo: hello there"}
    assert harness.ended == [(str(tenant.org), str(run_id))]
    request = harness.runtime.requests[0]
    assert isinstance(request, RunRequest)
    assert (request.input, request.scope.audience, request.scope.user_id) == (
        "hello there",
        "shared",
        None,
    )


async def test_the_next_run_continues_the_session(harness: Harness, tenant: Tenant) -> None:
    await harness.send(tenant, "mine", "one")
    await harness.send(tenant, "mine", "two")
    assert await harness.worker.run_once()
    assert await harness.worker.run_once()
    second = harness.runtime.requests[1]
    assert second.runtime_state is not None
    assert json.loads(second.runtime_state) == {"turns": 1}
    assert second.scope.user_id == str(tenant.ids["mia"])


@pytest.mark.parametrize(("approve", "output"), [(True, "deleted"), (False, "kept")])
async def test_an_approval_pauses_and_resumes_the_run(
    engine: AsyncEngine, harness: Harness, tenant: Tenant, approve: bool, output: str
) -> None:
    harness.runtime.add_script(approval_script())
    run_id = await harness.send(tenant, "s1", "clean up")
    await harness.send(tenant, "s1", "next")
    assert await harness.worker.run_once()
    row = await harness.run(run_id)
    assert (row.status, row.pending_approval_id) == ("paused", "call-9")
    assert harness.ended == [], "a pause does not end the run"
    assert not await harness.worker.run_once(), "the session waits for the approval"

    await GateService(engine).decide_approval(
        org_id=tenant.org,
        run_id=run_id,
        call_id="call-9",
        user_id=tenant.ids["mia"],
        approve=approve,
    )
    await harness.worker.maintain()
    assert await harness.worker.run_once()
    resume = harness.runtime.requests[1]
    assert isinstance(resume, ResumeRequest)
    assert (resume.approval_id, resume.runtime_state) == ("call-9", b"at-the-pause")
    row = await harness.run(run_id)
    assert (row.status, row.pending_approval_id) == ("completed", None)
    assert [seq for seq, _ in await harness.events(run_id)] == [0, 1, 2]
    assert harness.ended == [(str(tenant.org), str(run_id))]
    assert await harness.worker.run_once()
    assert harness.runtime.requests[2].input == "next"  # type: ignore[union-attr]


async def test_a_user_cancels_a_running_run(engine: AsyncEngine, tenant: Tenant) -> None:
    harness = Harness(engine, heartbeat_s=0.05)
    words = [TextDelta(text=f"w{n} ", usage=Usage(output_tokens=n)) for n in range(1, 40)]
    harness.runtime.add_script(
        FakeScript(events=(*words, RunCompleted(output="x", usage=words[-1].usage)), delay_s=0.05)
    )
    run_id = await harness.send(tenant, "s1", "long")
    task = asyncio.create_task(harness.worker.run_once())
    await until(lambda: harness.status_is(run_id, "running"))
    assert await harness.queue.cancel(org_id=tenant.org, run_id=run_id) == "running"
    async with asyncio.timeout(10):
        await task
    row = await harness.run(run_id)
    assert (row.status, row.failure_code) == ("cancelled", "cancelled")
    assert harness.ended == [(str(tenant.org), str(run_id))]


async def test_a_run_longer_than_its_wall_time_fails(engine: AsyncEngine) -> None:
    tenant = await add_tenant(engine, budget={"max_wall_time_s": 0.2})
    harness = Harness(engine)
    words = [TextDelta(text="w ", usage=Usage(output_tokens=n)) for n in range(1, 40)]
    harness.runtime.add_script(
        FakeScript(events=(*words, RunCompleted(output="x", usage=words[-1].usage)), delay_s=0.05)
    )
    run_id = await harness.send(tenant, "s1", "long")
    async with asyncio.timeout(10):
        assert await harness.worker.run_once()
    row = await harness.run(run_id)
    assert (row.status, row.failure_code) == ("failed", "timeout")
    assert "wall-time" in row.failure_message


async def test_a_bad_budget_fails_the_run(engine: AsyncEngine) -> None:
    tenant = await add_tenant(engine, budget={"max_turns": 0})
    harness = Harness(engine)
    run_id = await harness.send(tenant, "s1", "x")
    assert await harness.worker.run_once()
    row = await harness.run(run_id)
    assert (row.status, row.failure_code) == ("failed", "invalid_request")
    assert harness.runtime.requests == []


async def test_an_unknown_runtime_fails_the_run(harness: Harness, tenant: Tenant) -> None:
    run_id = await harness.send(tenant, "s1", "x")
    async with harness.engine.begin() as connection:
        await connection.execute(
            update(Run).where(Run.id == run_id).values(runtime="openai_agents_sdk")
        )
    assert await harness.worker.run_once()
    row = await harness.run(run_id)
    assert (row.status, row.failure_code) == ("failed", "invalid_request")
    assert harness.ended == [(str(tenant.org), str(run_id))]


async def test_a_worker_that_lost_its_lease_writes_no_result(
    harness: Harness, tenant: Tenant
) -> None:
    run_id = await harness.send(tenant, "s1", "hello")
    run = await harness.queue.claim("w1")
    assert run is not None
    async with harness.engine.begin() as connection:
        await connection.execute(update(Run).where(Run.id == run_id).values(lease_owner="w2"))
    await harness.worker.execute(run)
    row = await harness.run(run_id)
    assert (row.status, row.lease_owner) == ("running", "w2")
    assert await harness.events(run_id) == []
    assert harness.ended == []


async def test_maintain_reaps_runs_and_calls_the_hooks(harness: Harness, tenant: Tenant) -> None:
    run_id = await harness.send(tenant, "s1", "x")
    run = await harness.queue.claim("dead-worker")
    assert run is not None
    await harness.queue.add_events(run, "dead-worker", [(0, "text_delta", {"text": "a"})])
    async with harness.engine.begin() as connection:
        await connection.execute(
            update(Run)
            .where(Run.id == run_id)
            .values(lease_expires_at=Run.lease_expires_at - timedelta(minutes=5))
        )
    await harness.worker.maintain()
    assert (await harness.run(run_id)).status == "failed"
    assert harness.ended == [(str(tenant.org), str(run_id))]


async def test_serve_runs_sessions_in_parallel_until_stopped(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    harness = Harness(engine, poll_s=0.02, maintenance_s=0.05)
    ids = [await harness.send(tenant, s, "hi") for s in ("s1", "s2", "s1", "s3")]
    stop = asyncio.Event()
    server = asyncio.create_task(harness.worker.serve(stop, concurrency=2))
    await until(lambda: harness.ended_count(len(ids)))
    stop.set()
    async with asyncio.timeout(10):
        await server
    assert {(await harness.run(run_id)).status for run_id in ids} == {"completed"}


async def test_serve_needs_a_slot(harness: Harness) -> None:
    with pytest.raises(ValueError, match="concurrency"):
        await harness.worker.serve(asyncio.Event(), concurrency=0)
