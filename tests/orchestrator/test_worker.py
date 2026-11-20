"""Tests of the run worker with the fake runtime (docs/architecture.md, Section 4.3)."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.db.models import Approval, Message, Run, RunEvent, ToolPolicy
from sama.gate.service import GateService
from sama.orchestrator import worker as worker_module
from sama.orchestrator.queue import RunQueue
from sama.orchestrator.worker import MAX_FAILURE_CHARS, Worker
from sama.runtime.contract import (
    ApprovalDecision,
    ApprovalRequired,
    FailureCode,
    ResumeRequest,
    RunCompleted,
    RunFailed,
    RunRequest,
    RuntimeRegistry,
    TextDelta,
    Usage,
)
from sama.runtime.contract import RunEvent as Event
from sama.runtime.fake import FakeRuntime, FakeScript
from sama.vault.redact import default_redactor
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


async def test_a_run_logs_its_step_times(
    harness: Harness, tenant: Tenant, caplog: pytest.LogCaptureFixture
) -> None:
    run_id = await harness.send(tenant, "s1", "hi")
    with caplog.at_level(logging.INFO, logger="sama.orchestrator"):
        assert await harness.worker.run_once()
    assert "claim_s=" in caplog.text
    steps = [r.getMessage() for r in caplog.records if r.getMessage().startswith("worker steps")]
    assert len(steps) == 1
    names = [pair.split("=")[0] for pair in steps[0].split()[3:]]
    assert steps[0].split()[2] == f"run={run_id}"
    assert names == ["config_s", "dispatch_s", "first_event_s", "stream_s", "finish_s"]


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
    expected = ApprovalDecision.APPROVE if approve else ApprovalDecision.REJECT
    assert resume.decision is expected
    row = await harness.run(run_id)
    assert (row.status, row.pending_approval_id) == ("completed", None)
    assert [seq for seq, _ in await harness.events(run_id)] == [0, 1, 2]
    assert harness.ended == [(str(tenant.org), str(run_id))]
    assert await harness.worker.run_once()
    assert harness.runtime.requests[2].input == "next"  # type: ignore[union-attr]


async def test_a_deny_rule_after_the_approval_turns_the_resume_into_a_reject(
    engine: AsyncEngine, harness: Harness, tenant: Tenant
) -> None:
    """Live authority (Section 5.1): the worker checks the approved call again."""
    harness.runtime.add_script(approval_script())
    run_id = await harness.send(tenant, "s1", "clean up")
    assert await harness.worker.run_once()
    gate = GateService(engine)
    await gate.decide_approval(
        org_id=tenant.org, run_id=run_id, call_id="call-9", user_id=tenant.ids["mia"], approve=True
    )
    async with engine.begin() as connection:
        await connection.execute(
            insert(ToolPolicy).values(org_id=tenant.org, tool="Bash", action="deny")
        )
    await harness.worker.maintain()
    assert await harness.worker.run_once()
    resume = harness.runtime.requests[-1]
    assert isinstance(resume, ResumeRequest)
    assert resume.decision is ApprovalDecision.REJECT
    assert resume.reason is not None
    assert "a policy rule denies Bash" in resume.reason
    assert (await harness.run(run_id)).status == "completed"


async def test_a_failed_reconfirm_rejects_the_resume(
    engine: AsyncEngine, harness: Harness, tenant: Tenant
) -> None:
    harness.runtime.add_script(approval_script())
    run_id = await harness.send(tenant, "s1", "clean up")
    assert await harness.worker.run_once()
    await GateService(engine).decide_approval(
        org_id=tenant.org, run_id=run_id, call_id="call-9", user_id=tenant.ids["mia"], approve=True
    )

    async def broken(**_: Any) -> Any:
        raise RuntimeError("the database is down")

    harness.worker._gate.reconfirm = broken  # type: ignore[method-assign]
    await harness.worker.maintain()
    assert await harness.worker.run_once()
    resume = harness.runtime.requests[-1]
    assert isinstance(resume, ResumeRequest)
    assert resume.decision is ApprovalDecision.REJECT
    assert resume.reason is not None
    assert "the tool gate failed" in resume.reason


async def test_a_slow_reconfirm_rejects_the_resume(
    engine: AsyncEngine, harness: Harness, tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness.runtime.add_script(approval_script())
    run_id = await harness.send(tenant, "s1", "clean up")
    assert await harness.worker.run_once()
    await GateService(engine).decide_approval(
        org_id=tenant.org, run_id=run_id, call_id="call-9", user_id=tenant.ids["mia"], approve=True
    )

    async def slow(**_: Any) -> Any:
        await asyncio.sleep(5)

    monkeypatch.setattr(worker_module, "RECONFIRM_TIMEOUT_S", 0.05)
    harness.worker._gate.reconfirm = slow  # type: ignore[method-assign]
    await harness.worker.maintain()
    assert await harness.worker.run_once()
    resume = harness.runtime.requests[-1]
    assert isinstance(resume, ResumeRequest)
    assert resume.decision is ApprovalDecision.REJECT


async def test_a_lease_lost_during_reconfirm_starts_no_resume(
    engine: AsyncEngine, harness: Harness, tenant: Tenant
) -> None:
    harness.runtime.add_script(approval_script())
    run_id = await harness.send(tenant, "s1", "clean up")
    assert await harness.worker.run_once()
    gate = GateService(engine)
    await gate.decide_approval(
        org_id=tenant.org, run_id=run_id, call_id="call-9", user_id=tenant.ids["mia"], approve=True
    )

    async def taken(**kwargs: Any) -> Any:
        async with engine.begin() as connection:
            await connection.execute(update(Run).where(Run.id == run_id).values(lease_owner="w2"))
        return await gate.reconfirm(**kwargs)

    harness.worker._gate.reconfirm = taken  # type: ignore[method-assign]
    await harness.worker.maintain()
    assert await harness.worker.run_once()
    assert len(harness.runtime.requests) == 1, "the worker started a run it does not own"


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


async def test_a_secret_on_the_cut_of_a_failure_message_is_redacted(
    harness: Harness, tenant: Tenant
) -> None:
    secret = "known-secret-on-the-cut"
    message = "x" * (MAX_FAILURE_CHARS - 10) + secret
    harness.runtime.add_script(
        FakeScript(events=(RunFailed(code=FailureCode.MODEL_ERROR, message=message),))
    )
    default_redactor().add(secret)
    try:
        run_id = await harness.send(tenant, "s1", "x")
        assert await harness.worker.run_once()
    finally:
        default_redactor().forget(secret)
    row = await harness.run(run_id)
    assert row.failure_message.endswith("[REDACTED]")
    assert secret[:10] not in row.failure_message


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
    assert await harness.queue.dispatch(run, "dead-worker")
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


class StopWatch(FakeRuntime):
    """Records how many events the worker read, to show that it stops at once."""

    def __init__(self) -> None:
        super().__init__()
        self.read = 0

    async def run(self, request: RunRequest) -> AsyncIterator[Event]:
        async for event in super().run(request):
            self.read += 1
            yield event


async def test_a_rejected_write_stops_the_run_at_once(engine: AsyncEngine, tenant: Tenant) -> None:
    harness = Harness(engine)
    runtime = StopWatch()
    registry = RuntimeRegistry()
    registry.register(runtime)
    harness.worker = Worker(harness.queue, registry, owner="w1", flush_s=0.0)
    words = [TextDelta(text="w ", usage=Usage(output_tokens=n)) for n in range(1, 10)]
    runtime.add_script(FakeScript(events=(*words, RunCompleted(output="x", usage=words[-1].usage))))
    run_id = await harness.send(tenant, "s1", "x")
    run = await harness.queue.claim("w1")
    assert run is not None

    real_add = harness.queue.add_events

    async def add_then_steal(*args: Any) -> bool:
        stored = await real_add(*args)
        async with engine.begin() as connection:
            await connection.execute(update(Run).where(Run.id == run_id).values(lease_owner="w2"))
        return stored

    harness.queue.add_events = add_then_steal  # type: ignore[method-assign,assignment]
    await harness.worker.execute(run)
    assert runtime.read <= 2, "the worker read on after the lease was lost"
    assert (await harness.run(run_id)).status == "running"


async def test_the_default_worker_times_out_approvals(
    engine: AsyncEngine, harness: Harness, tenant: Tenant
) -> None:
    harness.runtime.add_script(approval_script())
    run_id = await harness.send(tenant, "s1", "clean up")
    assert await harness.worker.run_once()
    async with engine.begin() as connection:
        await connection.execute(
            update(Approval)
            .where(Approval.run_id == run_id)
            .values(expires_at=Approval.created_at - timedelta(seconds=1))
        )
    await harness.worker.maintain()
    assert await harness.worker.run_once()
    row = await harness.run(run_id)
    assert row.status == "completed"
    resume = harness.runtime.requests[-1]
    assert isinstance(resume, ResumeRequest)
    assert resume.decision is ApprovalDecision.REJECT
