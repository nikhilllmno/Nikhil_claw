"""Tests of the Postgres run queue (docs/architecture.md, Section 4.3)."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import insert, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.db.models import Approval, AuditEntry, Message, Run, RunEvent, Session, ToolCall
from sama.gate.service import GateService
from sama.orchestrator.directory import PostgresSandboxDirectory
from sama.orchestrator.queue import (
    EXPIRED_REASON,
    ClaimedRun,
    Lease,
    QueueError,
    RunQueue,
    RunRef,
    narrow_budget,
)
from sama.runtime.contract import ApprovalDecision, FailureCode, Usage
from sama.vault.redact import default_redactor
from tests.orchestrator.conftest import Tenant, add_tenant

OWNER = "worker-a"


async def enqueue(queue: RunQueue, tenant: Tenant, session: str, text_: str = "hi") -> uuid.UUID:
    return await queue.enqueue(
        org_id=tenant.org, session_id=tenant.ids[session], text=text_, user_id=tenant.ids["mia"]
    )


async def run_row(engine: AsyncEngine, run_id: uuid.UUID) -> Any:
    async with engine.connect() as connection:
        return (await connection.execute(select(Run).where(Run.id == run_id))).one()


async def claim(queue: RunQueue, owner: str = OWNER) -> ClaimedRun:
    claimed = await queue.claim(owner)
    assert claimed is not None
    return claimed


async def expire_lease(engine: AsyncEngine, run_id: uuid.UUID) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            update(Run)
            .where(Run.id == run_id)
            .values(lease_expires_at=text("now() - interval '1 second'"))
        )


async def pause(queue: RunQueue, run: ClaimedRun, approval_id: str = "call-1") -> None:
    assert (
        await queue.pause(
            run,
            OWNER,
            approval_id=approval_id,
            tool_name="Bash",
            arguments={"command": "make deploy"},
            reason="deploys",
            runtime_state=b"paused-state",
            usage=Usage(input_tokens=3),
        )
        == "paused"
    )


# -- enqueue ----------------------------------------------------------------------------------


async def test_enqueue_stores_the_message_and_a_queued_run(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    queue = RunQueue(engine)
    run_id = await enqueue(queue, tenant, "s1", "hello")
    row = await run_row(engine, run_id)
    assert (row.status, row.runtime, row.attempts) == ("queued", "fake", 0)
    assert row.triggered_by_user_id == tenant.ids["mia"]
    async with engine.connect() as connection:
        message = (await connection.execute(select(Message).where(Message.run_id == run_id))).one()
    assert (message.role, message.content) == ("user", {"text": "hello"})


async def test_enqueue_needs_an_active_session_of_the_org(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    queue = RunQueue(engine)
    other = await add_tenant(engine)
    with pytest.raises(QueueError, match="does not exist"):
        await queue.enqueue(org_id=other.org, session_id=tenant.ids["s1"], text="x")
    async with engine.begin() as connection:
        await connection.execute(
            update(Session).where(Session.id == tenant.ids["s2"]).values(status="archived")
        )
    with pytest.raises(QueueError, match="not active"):
        await enqueue(queue, tenant, "s2")


async def test_enqueue_copies_the_profile_budget(engine: AsyncEngine) -> None:
    tenant = await add_tenant(engine, budget={"max_turns": 4})
    run_id = await enqueue(RunQueue(engine), tenant, "s1")
    assert (await run_row(engine, run_id)).budget == {"max_turns": 4}


async def test_enqueue_in_can_only_make_the_budget_smaller(engine: AsyncEngine) -> None:
    tenant = await add_tenant(engine, budget={"max_turns": 4, "max_cost_usd": "2.50"})
    queue = RunQueue(engine)
    async with engine.begin() as connection:
        run_id = await queue.enqueue_in(
            connection,
            org_id=tenant.org,
            session_id=tenant.ids["s1"],
            text="hi",
            budget={"max_turns": 30, "max_cost_usd": "1.00", "max_wall_time_s": 60},
        )
    assert (await run_row(engine, run_id)).budget == {
        "max_turns": 4,
        "max_cost_usd": "1.00",
        "max_wall_time_s": 60,
    }


async def test_enqueue_in_rolls_back_with_the_transaction_of_the_caller(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    queue = RunQueue(engine)
    async with engine.connect() as connection:
        transaction = await connection.begin()
        run_id = await queue.enqueue_in(
            connection, org_id=tenant.org, session_id=tenant.ids["s1"], text="hi"
        )
        await transaction.rollback()
    async with engine.connect() as connection:
        found = await connection.execute(select(Run.id).where(Run.id == run_id))
        assert found.first() is None


def test_narrow_budget_takes_the_lower_value_of_each_key() -> None:
    assert narrow_budget(None, None) == {}
    assert narrow_budget({"max_turns": 5}, {"max_turns": 9, "max_cost_usd": 1}) == {
        "max_turns": 5,
        "max_cost_usd": "1",
    }
    assert narrow_budget({"max_cost_usd": "0.5"}, {"max_cost_usd": "0.75", "max_turns": None}) == {
        "max_cost_usd": "0.5"
    }
    with pytest.raises(ValueError, match="not a budget key"):
        narrow_budget({}, {"max_gpus": 1})


# -- claim ------------------------------------------------------------------------------------


async def test_claim_takes_the_run_with_its_input(engine: AsyncEngine, tenant: Tenant) -> None:
    queue = RunQueue(engine)
    run_id = await enqueue(queue, tenant, "mine", "what is up")
    run = await claim(queue)
    assert run.run_id == run_id
    assert (run.input, run.attempt, run.next_seq, run.resume) == ("what is up", 1, 0, None)
    assert (run.audience, run.owner_user_id, run.runtime) == ("private", tenant.ids["mia"], "fake")
    assert (run.model, run.soul) == ("fake-model", "You are a test agent.")
    row = await run_row(engine, run_id)
    assert (row.status, row.lease_owner) == ("running", OWNER)
    assert row.lease_expires_at > datetime.now(UTC)
    assert row.started_at is not None
    assert await queue.claim(OWNER) is None


async def test_one_session_runs_one_run_at_a_time_in_order(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    queue = RunQueue(engine)
    first = await enqueue(queue, tenant, "s1", "one")
    second = await enqueue(queue, tenant, "s1", "two")
    third = await enqueue(queue, tenant, "s1", "three")
    run = await claim(queue)
    assert run.run_id == first
    assert await queue.claim(OWNER) is None
    for expected in (second, third):
        assert await queue.complete(run, OWNER, output="ok", runtime_state=None, usage=Usage())
        run = await claim(queue)
        assert run.run_id == expected
    assert run.input == "three"


async def test_sessions_run_in_parallel(engine: AsyncEngine, tenant: Tenant) -> None:
    queue = RunQueue(engine)
    ids = {await enqueue(queue, tenant, session) for session in ("s1", "s2", "s3")}
    claimed = {(await claim(queue)).run_id for _ in range(3)}
    assert claimed == ids


async def test_concurrent_claims_take_each_run_once(engine: AsyncEngine, tenant: Tenant) -> None:
    queue = RunQueue(engine)
    ids = set()
    for session in ("s1", "s2", "s3", "mine"):
        ids.add(await enqueue(queue, tenant, session))
        await enqueue(queue, tenant, session)  # waits behind the first run
    results = await asyncio.gather(*(queue.claim(f"w{n}") for n in range(12)))
    claimed = [run.run_id for run in results if run is not None]
    assert sorted(claimed) == sorted(ids)


async def test_the_database_allows_one_active_main_run_per_session(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    queue = RunQueue(engine)
    await enqueue(queue, tenant, "s1")
    second = await enqueue(queue, tenant, "s1")
    run = await claim(queue)
    await pause(queue, run)
    with pytest.raises(IntegrityError, match="ix_runs_one_main_per_session"):
        async with engine.begin() as connection:
            await connection.execute(
                update(Run)
                .where(Run.id == second)
                .values(status="running", lease_owner="x", lease_expires_at=text("now()"))
            )


async def test_a_running_run_needs_a_lease(engine: AsyncEngine, tenant: Tenant) -> None:
    run_id = await enqueue(RunQueue(engine), tenant, "s1")
    with pytest.raises(IntegrityError, match="ck_runs_lease"):
        async with engine.begin() as connection:
            await connection.execute(update(Run).where(Run.id == run_id).values(status="running"))


async def test_a_new_run_gets_the_state_of_the_last_completed_run(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    queue = RunQueue(engine)
    await enqueue(queue, tenant, "s1")
    run = await claim(queue)
    assert run.runtime_state is None
    assert await queue.complete(
        run, OWNER, output="a", runtime_state=b"\x00state-1", usage=Usage(output_tokens=2)
    )
    await enqueue(queue, tenant, "s1")
    run = await claim(queue)
    assert run.runtime_state == b"\x00state-1"
    assert await queue.fail(run, OWNER, code=FailureCode.MODEL_ERROR, message="boom", usage=Usage())
    await enqueue(queue, tenant, "s1")
    assert (await claim(queue)).runtime_state == b"\x00state-1"


# -- ending a run -----------------------------------------------------------------------------


async def test_complete_stores_the_reply_and_the_usage(engine: AsyncEngine, tenant: Tenant) -> None:
    queue = RunQueue(engine)
    run_id = await enqueue(queue, tenant, "s1")
    run = await claim(queue)
    assert await queue.complete(
        run, OWNER, output="the answer", runtime_state=b"s", usage=Usage(input_tokens=7)
    )
    row = await run_row(engine, run_id)
    assert (row.status, row.lease_owner, row.lease_expires_at) == ("completed", None, None)
    assert row.usage["input_tokens"] == 7
    assert row.finished_at is not None
    async with engine.connect() as connection:
        roles = (
            await connection.execute(
                select(Message.role, Message.content)
                .where(Message.run_id == run_id)
                .order_by(Message.seq)
            )
        ).all()
    assert [tuple(r) for r in roles] == [
        ("user", {"text": "hi"}),
        ("assistant", {"text": "the answer"}),
    ]


async def test_fail_with_cancelled_ends_as_cancelled(engine: AsyncEngine, tenant: Tenant) -> None:
    queue = RunQueue(engine)
    first = await enqueue(queue, tenant, "s1")
    second = await enqueue(queue, tenant, "s2")
    for _ in range(2):
        run = await claim(queue)
        code = FailureCode.CANCELLED if run.run_id == first else FailureCode.TOOL_ERROR
        assert await queue.fail(run, OWNER, code=code, message="m", usage=Usage())
    assert (await run_row(engine, first)).status == "cancelled"
    row = await run_row(engine, second)
    assert (row.status, row.failure_code, row.failure_message) == ("failed", "tool_error", "m")


async def test_only_the_lease_owner_ends_a_run(engine: AsyncEngine, tenant: Tenant) -> None:
    queue = RunQueue(engine)
    run_id = await enqueue(queue, tenant, "s1")
    run = await claim(queue)
    assert not await queue.complete(run, "intruder", output="x", runtime_state=None, usage=Usage())
    assert not await queue.add_events(run, "intruder", [(0, "text_delta", {})])
    assert await queue.heartbeat(run, "intruder") is Lease.LOST
    assert (await run_row(engine, run_id)).status == "running"


async def test_add_events_stores_rows_in_order(engine: AsyncEngine, tenant: Tenant) -> None:
    queue = RunQueue(engine)
    await enqueue(queue, tenant, "s1")
    run = await claim(queue)
    assert await queue.add_events(
        run, OWNER, [(0, "text_delta", {"text": "a"}), (1, "text_delta", {"text": "b"})]
    )
    async with engine.connect() as connection:
        rows = (
            await connection.execute(
                select(RunEvent.seq, RunEvent.payload)
                .where(RunEvent.run_id == run.run_id)
                .order_by(RunEvent.seq)
            )
        ).all()
    assert [tuple(r) for r in rows] == [(0, {"text": "a"}), (1, {"text": "b"})]


async def test_a_subagent_event_is_redacted_whole_and_then_cut(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    """The runner sends the whole text. A secret that crosses the cut mark is redacted before
    the cut, so no prefix of it stays in the stored text."""
    known = "opaque-signing-0123456789abcdef"
    text = "x" * 1_990 + known + "y" * 3_000
    default_redactor().add(known)
    try:
        queue = RunQueue(engine)
        await enqueue(queue, tenant, "s1")
        run = await claim(queue)
        payload = {"type": "subagent_event", "subagent": "researcher", "text": text}
        assert await queue.add_events(run, OWNER, [(0, "subagent_event", payload)])
        async with engine.connect() as connection:
            stored = (
                await connection.execute(
                    select(RunEvent.payload).where(RunEvent.run_id == run.run_id)
                )
            ).scalar_one()
    finally:
        default_redactor().forget(known)
    assert known not in stored["text"]
    assert "opaque-sign" not in stored["text"]
    assert stored["text"].startswith("x" * 1_990 + "[REDACTED]")
    assert len(stored["text"]) == 2_000


async def test_the_other_events_are_not_cut(engine: AsyncEngine, tenant: Tenant) -> None:
    queue = RunQueue(engine)
    await enqueue(queue, tenant, "s1")
    run = await claim(queue)
    assert await queue.add_events(run, OWNER, [(0, "text_delta", {"text": "a" * 5_000})])
    async with engine.connect() as connection:
        stored = (
            await connection.execute(select(RunEvent.payload).where(RunEvent.run_id == run.run_id))
        ).scalar_one()
    assert len(stored["text"]) == 5_000


async def test_heartbeat_extends_the_lease_and_reports_a_cancel(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    queue = RunQueue(engine, lease=timedelta(minutes=5))
    run_id = await enqueue(queue, tenant, "s1")
    run = await claim(queue)
    before = (await run_row(engine, run_id)).lease_expires_at
    await asyncio.sleep(0.01)
    assert await queue.heartbeat(run, OWNER) is Lease.HELD
    assert (await run_row(engine, run_id)).lease_expires_at > before
    assert await queue.cancel(org_id=tenant.org, run_id=run_id) == "running"
    assert await queue.heartbeat(run, OWNER) is Lease.CANCEL


# -- cancel -----------------------------------------------------------------------------------


async def test_cancel_ends_a_queued_or_paused_run_at_once(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    queue = RunQueue(engine)
    paused = await enqueue(queue, tenant, "s1")
    await pause(queue, await claim(queue))
    queued = await enqueue(queue, tenant, "s1")
    for run_id in (queued, paused):
        assert await queue.cancel(org_id=tenant.org, run_id=run_id) == "cancelled"
        row = await run_row(engine, run_id)
        assert (row.status, row.failure_code) == ("cancelled", "cancelled")
        assert row.finished_at is not None
    assert await queue.cancel(org_id=tenant.org, run_id=paused) == "cancelled"


async def test_cancel_does_not_change_an_ended_run(engine: AsyncEngine, tenant: Tenant) -> None:
    queue = RunQueue(engine)
    run_id = await enqueue(queue, tenant, "s1")
    await queue.complete(await claim(queue), OWNER, output="x", runtime_state=None, usage=Usage())
    assert await queue.cancel(org_id=tenant.org, run_id=run_id) == "completed"


async def test_cancel_is_scoped_by_org(engine: AsyncEngine, tenant: Tenant) -> None:
    queue = RunQueue(engine)
    run_id = await enqueue(queue, tenant, "s1")
    other = await add_tenant(engine)
    with pytest.raises(QueueError, match="does not exist"):
        await queue.cancel(org_id=other.org, run_id=run_id)
    assert (await run_row(engine, run_id)).status == "queued"


# -- approvals --------------------------------------------------------------------------------


async def approval_row(engine: AsyncEngine, run_id: uuid.UUID, call_id: str = "call-1") -> Any:
    async with engine.connect() as connection:
        return (
            await connection.execute(
                select(Approval).where(Approval.run_id == run_id, Approval.call_id == call_id)
            )
        ).one()


async def test_pause_creates_the_approval_if_the_gate_did_not(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    queue = RunQueue(engine)
    run_id = await enqueue(queue, tenant, "s1")
    await pause(queue, await claim(queue))
    row = await run_row(engine, run_id)
    assert (row.status, row.pending_approval_id, row.lease_owner) == ("paused", "call-1", None)
    assert row.finished_at is None
    approval = await approval_row(engine, run_id)
    assert (approval.status, approval.tool_name, approval.reason) == ("pending", "Bash", "deploys")
    async with engine.connect() as connection:
        actions = (
            await connection.execute(select(AuditEntry.action).where(AuditEntry.run_id == run_id))
        ).scalars()
        assert list(actions) == ["approval.requested"]


async def test_a_cancel_before_the_pause_ends_the_run(engine: AsyncEngine, tenant: Tenant) -> None:
    queue = RunQueue(engine)
    run_id = await enqueue(queue, tenant, "s1")
    run = await claim(queue)
    assert await queue.cancel(org_id=tenant.org, run_id=run_id) == "running"
    status = await queue.pause(
        run,
        OWNER,
        approval_id="call-1",
        tool_name="Bash",
        arguments={},
        reason="",
        runtime_state=b"s",
        usage=Usage(),
    )
    assert status == "cancelled"
    row = await run_row(engine, run_id)
    assert (row.status, row.pending_approval_id, row.lease_owner) == ("cancelled", None, None)
    assert await GateService(engine).expire() >= 0
    assert await queue.release_decided() == []
    assert await queue.claim(OWNER) is None


async def test_pause_keeps_the_approval_of_the_gate(engine: AsyncEngine, tenant: Tenant) -> None:
    queue = RunQueue(engine)
    run_id = await enqueue(queue, tenant, "s1")
    run = await claim(queue)
    async with engine.begin() as connection:
        await connection.execute(
            insert(Approval).values(
                org_id=tenant.org,
                session_id=tenant.ids["s1"],
                run_id=run_id,
                call_id="call-1",
                tool_name="Bash",
                arguments={"command": "make deploy"},
                arguments_digest="d" * 64,
                reason="from the gate",
                expires_at=text("now() + interval '1 hour'"),
            )
        )
    await pause(queue, run)
    assert (await approval_row(engine, run_id)).reason == "from the gate"


@pytest.mark.parametrize(("approve", "decision"), [(True, "approve"), (False, "reject")])
async def test_a_decided_approval_queues_the_run_for_a_resume(
    engine: AsyncEngine, tenant: Tenant, approve: bool, decision: str
) -> None:
    queue = RunQueue(engine)
    gate = GateService(engine)
    run_id = await enqueue(queue, tenant, "s1")
    await enqueue(queue, tenant, "s1", "later")
    await pause(queue, await claim(queue))
    assert await queue.claim(OWNER) is None, "a paused run keeps its session"
    assert await queue.release_decided() == []
    await gate.decide_approval(
        org_id=tenant.org,
        run_id=run_id,
        call_id="call-1",
        user_id=tenant.ids["mia"],
        approve=approve,
        reason="ok by me",
    )
    assert await queue.release_decided(org_id=tenant.org, run_id=run_id) == [run_id]
    run = await claim(queue)
    assert run.run_id == run_id, "the resumed run goes before the newer run"
    assert run.resume is not None
    assert (run.resume.approval_id, run.resume.decision, run.resume.reason) == (
        "call-1",
        ApprovalDecision(decision),
        "ok by me",
    )
    assert (run.runtime_state, run.attempt) == (b"paused-state", 2)
    assert await queue.complete(run, OWNER, output="done", runtime_state=None, usage=Usage())
    row = await run_row(engine, run_id)
    assert (row.resume_decision, row.resume_reason, row.pending_approval_id) == (None, None, None)
    assert (await claim(queue)).input == "later"


async def test_an_expired_approval_resumes_with_reject(engine: AsyncEngine, tenant: Tenant) -> None:
    queue = RunQueue(engine)
    later = datetime.now(UTC) + timedelta(days=2)
    gate = GateService(engine, clock=lambda: later)
    run_id = await enqueue(queue, tenant, "s1")
    await pause(queue, await claim(queue))
    assert await gate.expire() >= 1
    assert run_id in await queue.release_decided()
    run = await claim(queue)
    assert run.resume is not None
    assert (run.resume.decision, run.resume.reason) == (ApprovalDecision.REJECT, EXPIRED_REASON)


async def test_release_decided_is_scoped(engine: AsyncEngine, tenant: Tenant) -> None:
    queue = RunQueue(engine)
    gate = GateService(engine)
    run_id = await enqueue(queue, tenant, "s1")
    await pause(queue, await claim(queue))
    await gate.decide_approval(
        org_id=tenant.org,
        run_id=run_id,
        call_id="call-1",
        user_id=tenant.ids["mia"],
        approve=True,
    )
    other = await add_tenant(engine)
    assert await queue.release_decided(org_id=other.org, run_id=run_id) == []
    assert await queue.release_decided(org_id=tenant.org, run_id=uuid.uuid4()) == []
    assert await queue.release_decided(org_id=tenant.org) == [run_id]


# -- reap -------------------------------------------------------------------------------------


async def test_reap_queues_a_run_that_left_no_trace(engine: AsyncEngine, tenant: Tenant) -> None:
    queue = RunQueue(engine)
    run_id = await enqueue(queue, tenant, "s1")
    await claim(queue)
    assert await queue.reap() == ([], [])
    await expire_lease(engine, run_id)
    assert await queue.reap() == ([RunRef(tenant.org, run_id)], [])
    row = await run_row(engine, run_id)
    assert (row.status, row.lease_owner, row.attempts) == ("queued", None, 1)
    run = await claim(queue, "worker-b")
    assert (run.run_id, run.attempt) == (run_id, 2)
    assert not await queue.complete(run, OWNER, output="late", runtime_state=None, usage=Usage()), (
        "the old worker lost the run"
    )


async def test_reap_fails_a_dispatched_run(engine: AsyncEngine, tenant: Tenant) -> None:
    queue = RunQueue(engine)
    run_id = await enqueue(queue, tenant, "s1")
    run = await claim(queue)
    assert await queue.dispatch(run, OWNER)
    await expire_lease(engine, run_id)
    assert await queue.reap() == ([], [RunRef(tenant.org, run_id)])
    row = await run_row(engine, run_id)
    assert (row.status, row.failure_code) == ("failed", "internal")
    assert "tool can have run" in row.failure_message


async def test_an_old_attempt_cannot_write_after_a_new_claim(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    queue = RunQueue(engine)
    run_id = await enqueue(queue, tenant, "s1")
    old = await claim(queue)
    await expire_lease(engine, run_id)
    assert await queue.heartbeat(old, OWNER) is Lease.LOST, "an expired lease is lost"
    assert not await queue.dispatch(old, OWNER)
    await queue.reap()
    new = await claim(queue)  # the same owner, as two slots of one worker
    assert new.attempt == old.attempt + 1
    assert await queue.heartbeat(old, OWNER) is Lease.LOST
    assert not await queue.add_events(old, OWNER, [(0, "text_delta", {})])
    assert not await queue.complete(old, OWNER, output="x", runtime_state=None, usage=Usage())
    assert (
        await queue.pause(
            old,
            OWNER,
            approval_id="c",
            tool_name="Bash",
            arguments={},
            reason="",
            runtime_state=None,
            usage=Usage(),
        )
        is None
    )
    assert await queue.complete(new, OWNER, output="y", runtime_state=None, usage=Usage())
    assert (await run_row(engine, run_id)).status == "completed"


async def test_reap_stops_after_max_attempts(engine: AsyncEngine, tenant: Tenant) -> None:
    queue = RunQueue(engine, max_attempts=2)
    run_id = await enqueue(queue, tenant, "s1")
    for expected in ("queued", "failed"):
        await claim(queue)
        await expire_lease(engine, run_id)
        await queue.reap()
        assert (await run_row(engine, run_id)).status == expected


async def test_reap_cancels_a_run_with_a_cancel_request(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    queue = RunQueue(engine)
    run_id = await enqueue(queue, tenant, "s1")
    await claim(queue)
    await queue.cancel(org_id=tenant.org, run_id=run_id)
    await expire_lease(engine, run_id)
    assert await queue.reap() == ([], [RunRef(tenant.org, run_id)])
    assert (await run_row(engine, run_id)).status == "cancelled"


def test_the_queue_settings_must_be_valid(engine: AsyncEngine) -> None:
    with pytest.raises(ValueError, match="lease"):
        RunQueue(engine, lease=timedelta(0))
    with pytest.raises(ValueError, match="max_attempts"):
        RunQueue(engine, max_attempts=0)


# -- sandbox directory ------------------------------------------------------------------------


async def test_the_sandbox_directory_stores_one_id_per_session(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    directory = PostgresSandboxDirectory(engine)
    org, session = str(tenant.org), str(tenant.ids["s1"])
    assert await directory.get(org, session) is None
    await directory.put(org, session, "sbx-1")
    assert await directory.get(org, session) == "sbx-1"
    other = await add_tenant(engine)
    assert await directory.get(str(other.org), session) is None
    await directory.put(str(other.org), session, "sbx-evil")
    assert await directory.get(org, session) == "sbx-1"
    assert await directory.get("not-a-uuid", session) is None
    await directory.put("not-a-uuid", session, "x")


async def _tool_row(
    engine: AsyncEngine, run: ClaimedRun, call_id: str, status: str, decision: str = "allow"
) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            insert(ToolCall).values(
                org_id=run.org_id,
                session_id=run.session_id,
                run_id=run.run_id,
                call_id=call_id,
                tool_name="browser_open",
                arguments={},
                decision=decision,
                status=status,
            )
        )


async def _tool_rows(engine: AsyncEngine, run: ClaimedRun) -> dict[str, Any]:
    async with engine.connect() as connection:
        rows = (
            await connection.execute(
                select(
                    ToolCall.call_id,
                    ToolCall.status,
                    ToolCall.finished_at,
                    ToolCall.result_preview,
                ).where(ToolCall.run_id == run.run_id)
            )
        ).all()
    return {r.call_id: r for r in rows}


def _done(call_id: str, *, error: bool = False, preview: str = "ok") -> dict[str, Any]:
    return {
        "type": "tool_call_finished",
        "call_id": call_id,
        "is_error": error,
        "output_preview": preview,
    }


async def test_a_finished_event_ends_the_tool_call_row(engine: AsyncEngine, tenant: Tenant) -> None:
    """Item E of M8.3: a call that the gate allowed without an approval was never ended."""
    queue = RunQueue(engine)
    await enqueue(queue, tenant, "s1")
    run = await claim(queue)
    await _tool_row(engine, run, "ok-call", "started")
    await _tool_row(engine, run, "bad-call", "started")
    await _tool_row(engine, run, "no-call", "denied", decision="deny")
    await _tool_row(engine, run, "ticket-call", "finished")
    assert await queue.add_events(
        run,
        OWNER,
        [
            (0, "tool_call_started", {"call_id": "ok-call", "name": "browser_open"}),
            (1, "tool_call_finished", _done("ok-call", preview="the page")),
            (2, "tool_call_finished", _done("bad-call", error=True, preview="boom")),
            (3, "tool_call_finished", _done("no-call", error=True, preview="denied")),
            (4, "tool_call_finished", _done("ticket-call", error=True, preview="late")),
            (5, "tool_call_finished", _done("unknown-call")),
        ],
    )
    rows = await _tool_rows(engine, run)
    assert (rows["ok-call"].status, rows["ok-call"].result_preview) == ("finished", "the page")
    assert rows["ok-call"].finished_at is not None
    assert (rows["bad-call"].status, rows["bad-call"].result_preview) == ("failed", "boom")
    assert rows["bad-call"].finished_at is not None
    # A denied row stays as the gate wrote it.
    assert (rows["no-call"].status, rows["no-call"].finished_at) == ("denied", None)
    assert rows["no-call"].result_preview is None
    # A used ticket stays ``finished`` and gets the time and the preview.
    assert (rows["ticket-call"].status, rows["ticket-call"].result_preview) == ("finished", "late")
    assert "unknown-call" not in rows


async def test_the_finished_preview_is_redacted(engine: AsyncEngine, tenant: Tenant) -> None:
    known = "opaque-preview-secret-0123456789"
    default_redactor().add(known)
    try:
        queue = RunQueue(engine)
        await enqueue(queue, tenant, "s1")
        run = await claim(queue)
        await _tool_row(engine, run, "c1", "started")
        assert await queue.add_events(
            run, OWNER, [(0, "tool_call_finished", _done("c1", preview=f"key {known}"))]
        )
        rows = await _tool_rows(engine, run)
    finally:
        default_redactor().forget(known)
    assert rows["c1"].result_preview == "key [REDACTED]"


async def test_a_finished_event_in_a_compaction_batch_ends_the_row(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    queue = RunQueue(engine)
    await enqueue(queue, tenant, "s1")
    run = await claim(queue)
    await _tool_row(engine, run, "c1", "started")
    compacted = {"type": "compacted", "tokens_before": 10, "tokens_after": 5, "trigger": "auto"}
    assert await queue.add_events(
        run, OWNER, [(0, "tool_call_finished", _done("c1")), (1, "compacted", compacted)]
    )
    assert (await _tool_rows(engine, run))["c1"].status == "finished"


async def test_a_lost_lease_ends_no_tool_call(engine: AsyncEngine, tenant: Tenant) -> None:
    queue = RunQueue(engine)
    await enqueue(queue, tenant, "s1")
    run = await claim(queue)
    await _tool_row(engine, run, "c1", "started")
    assert not await queue.add_events(run, "intruder", [(0, "tool_call_finished", _done("c1"))])
    assert (await _tool_rows(engine, run))["c1"].status == "started"
