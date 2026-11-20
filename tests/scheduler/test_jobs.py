"""Jobs with Postgres: approval, the recursion guard, one fire per time, failures, and the gate
of an unattended job run (M4.1 and M4.2)."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.db.models import Delivery, Job, JobFire, Run
from sama.gate.service import GateService
from sama.orchestrator.queue import RunQueue
from sama.scheduler.fire import finish_fire
from sama.scheduler.jobs import JobService
from sama.scheduler.service import Scheduler
from sama.scheduler.spec import JobError, JobInput, PreapprovalIn
from sama.tenancy import RunTokenClaims
from tests.scheduler.conftest import Tenant

EVERY_HOUR = JobInput(name="news", schedule="every hour", prompt="Summarize the news")
BASH = [PreapprovalIn(tool="Bash", limits={"command": ["make report"]})]


async def _fire(engine: AsyncEngine, fire_id: uuid.UUID) -> Any:
    async with engine.connect() as connection:
        return (await connection.execute(select(JobFire).where(JobFire.id == fire_id))).one()


async def _job(engine: AsyncEngine, job_id: uuid.UUID) -> Any:
    async with engine.connect() as connection:
        return (await connection.execute(select(Job).where(Job.id == job_id))).one()


async def _end_run(engine: AsyncEngine, run_id: uuid.UUID | None) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            update(Run)
            .where(Run.id == run_id)
            .values(status="cancelled", lease_owner=None, lease_expires_at=None)
        )


def _claims(tenant: Tenant, fire: Any) -> RunTokenClaims:
    return RunTokenClaims(
        org_id=str(tenant.org),
        workspace_id=str(tenant.ids["ws"]),
        session_id=str(fire.session_id),
        run_id=str(fire.run_id),
        token_id="t-job",
        expires_at=2_000_000_000,
    )


async def test_a_job_of_a_person_is_active_at_once(jobs: JobService, tenant: Tenant) -> None:
    view = await jobs.create(tenant.user(), EVERY_HOUR)
    assert (view.status, view.version, view.created_by_kind) == ("active", 1, "user")
    assert view.next_fire_at is not None
    assert len(view.next_fires) == 3


async def test_a_job_of_the_agent_with_tools_waits_for_the_owner(
    jobs: JobService, tenant: Tenant
) -> None:
    given = EVERY_HOUR.model_copy(update={"preapproved_tools": BASH})
    view = await jobs.create(tenant.agent(), given)
    assert (view.status, view.approved_version, view.next_fire_at) == (
        "pending_approval",
        None,
        None,
    )
    with pytest.raises(JobError, match="needs the approval"):
        await jobs.run_now(tenant.user(), view.id)
    with pytest.raises(JobError, match="only a person"):
        await jobs.approve(tenant.agent(), view.id, 1)
    changed = await jobs.update(tenant.agent(), view.id, JobInput(prompt="Another prompt"))
    assert changed.version == 2
    with pytest.raises(JobError, match="version 2 now, not 1"):
        await jobs.approve(tenant.user(), view.id, 1)
    approved = await jobs.approve(tenant.user(), view.id, 2)
    assert (approved.status, approved.approved_version) == ("active", 2)
    assert approved.next_fire_at is not None


async def test_a_job_run_cannot_make_jobs(
    engine: AsyncEngine, jobs: JobService, tenant: Tenant
) -> None:
    view = await jobs.create(tenant.user(), EVERY_HOUR)
    fire = await _fire(engine, await jobs.run_now(tenant.user(), view.id))
    assert fire.run_id is not None
    in_job = tenant.user(kind="agent", run_id=fire.run_id, session_id=fire.session_id)
    with pytest.raises(JobError, match="cannot create or change jobs"):
        await jobs.create(in_job, EVERY_HOUR)
    with pytest.raises(JobError, match="cannot create or change jobs"):
        await jobs.pause(in_job, view.id)
    gate = GateService(engine)
    verdict = await gate.check(_claims(tenant, fire), "c1", "mcp__sama__cronjob_manage", {})
    assert verdict.action == "deny"
    chat = RunTokenClaims(
        org_id=str(tenant.org),
        workspace_id=str(tenant.ids["ws"]),
        session_id=str(tenant.ids["chat"]),
        run_id=str(tenant.ids["chat-run"]),
        token_id="t-chat",
        expires_at=2_000_000_000,
    )
    allowed = await gate.check(chat, "c2", "mcp__sama__cronjob_manage", {})
    assert allowed.action == "allow"
    await _end_run(engine, fire.run_id)


async def test_an_agent_job_cannot_run_a_write_tool_without_approval(
    engine: AsyncEngine, jobs: JobService, tenant: Tenant
) -> None:
    """The acceptance test of M4.2: the gate denies a write tool in the unattended run of a
    job that the agent made, until the owner approves this version with a pre-approval."""
    gate = GateService(engine)
    report = {"command": "make report"}
    plain = await jobs.create(tenant.agent(), EVERY_HOUR)
    assert plain.status == "active"
    fire = await _fire(engine, await jobs.run_now(tenant.user(), plain.id))
    verdict = await gate.check(_claims(tenant, fire), "c1", "Bash", report)
    assert (verdict.action, verdict.reason) == (
        "deny",
        "Bash needs an approval, and this run is unattended",
    )
    await _end_run(engine, fire.run_id)

    given = EVERY_HOUR.model_copy(update={"name": "report", "preapproved_tools": BASH})
    tooled = await jobs.create(tenant.agent(), given)
    await jobs.approve(tenant.user(), tooled.id, 1)
    fire = await _fire(engine, await jobs.run_now(tenant.user(), tooled.id))
    claims = _claims(tenant, fire)
    assert (await gate.check(claims, "c2", "Bash", report)).action == "allow"
    other = await gate.check(claims, "c3", "Bash", {"command": "rm -rf /"})
    assert other.action == "deny"
    assert (await gate.check(claims, "c4", "Write", {"file_path": "x"})).action == "deny"

    # The agent changes the job. The open run has the old version, so it loses the grant.
    await jobs.update(tenant.agent(), tooled.id, JobInput(prompt="Another prompt"))
    assert (await _job(engine, tooled.id)).status == "pending_approval"
    assert (await gate.check(claims, "c5", "Bash", report)).action == "deny"
    await _end_run(engine, fire.run_id)


async def test_two_schedulers_fire_a_job_once(
    engine: AsyncEngine, jobs: JobService, tenant: Tenant
) -> None:
    view = await jobs.create(tenant.user(), EVERY_HOUR)
    due = datetime.now(UTC) - timedelta(minutes=1)
    async with engine.begin() as connection:
        await connection.execute(update(Job).where(Job.id == view.id).values(next_fire_at=due))
    one = Scheduler(engine, RunQueue(engine), None)
    two = Scheduler(engine, RunQueue(engine), None)
    results = await asyncio.gather(*(s.tick() for s in (one, two, one, two)))
    assert sum(1 for result in results if result > 0) == 1
    async with engine.connect() as connection:
        fires = (await connection.execute(select(JobFire).where(JobFire.job_id == view.id))).all()
    assert len(fires) == 1
    assert fires[0].scheduled_for == due
    after = await _job(engine, view.id)
    assert after.next_fire_at > datetime.now(UTC)
    assert after.last_fire_at == due
    assert (await one.tick()) == 0
    await _end_run(engine, fires[0].run_id)


async def test_a_busy_job_skips_a_fire(
    engine: AsyncEngine, jobs: JobService, tenant: Tenant
) -> None:
    view = await jobs.create(tenant.user(), EVERY_HOUR)
    first = await _fire(engine, await jobs.run_now(tenant.user(), view.id))
    due = datetime.now(UTC) - timedelta(minutes=1)
    async with engine.begin() as connection:
        await connection.execute(update(Job).where(Job.id == view.id).values(next_fire_at=due))
    assert await Scheduler(engine, RunQueue(engine), None).tick() == 1
    async with engine.connect() as connection:
        skipped = (
            await connection.execute(
                select(JobFire.status).where(
                    JobFire.job_id == view.id, JobFire.scheduled_for == due
                )
            )
        ).scalar_one()
    assert skipped == "skipped"
    await _end_run(engine, first.run_id)


async def test_the_scheduler_writes_the_result_of_an_ended_run(
    engine: AsyncEngine, jobs: JobService, tenant: Tenant
) -> None:
    view = await jobs.create(tenant.user(), EVERY_HOUR)
    fire = await _fire(engine, await jobs.run_now(tenant.user(), view.id))
    await _end_run(engine, fire.run_id)
    assert await Scheduler(engine, RunQueue(engine), None).finish() == 1
    ended = await _fire(engine, fire.id)
    assert (ended.status, ended.error) == ("failed", "the run was cancelled")
    async with engine.connect() as connection:
        kinds = (
            await connection.execute(select(Delivery.kind).where(Delivery.fire_id == fire.id))
        ).all()
    assert [kind for (kind,) in kinds] == ["failure"]


async def test_three_failures_pause_the_job_with_one_alert(
    engine: AsyncEngine, jobs: JobService, tenant: Tenant
) -> None:
    given = EVERY_HOUR.model_copy(update={"mode": "script", "script": "exit 1"})
    view = await jobs.create(tenant.user(), given)
    for _ in range(4):
        fire_id = await jobs.run_now(tenant.user(), view.id)
        async with engine.begin() as connection:
            row = (
                await connection.execute(
                    select(JobFire.id, JobFire.org_id, JobFire.job_id).where(JobFire.id == fire_id)
                )
            ).one()
            await finish_fire(connection, row, ok=False, output="", error="boom")
    job = await _job(engine, view.id)
    assert (job.status, job.consecutive_failures) == ("paused", 4)
    assert job.paused_reason == "paused after 3 failed fires in a row"
    assert job.next_fire_at is None
    async with engine.connect() as connection:
        kinds = (
            await connection.execute(select(Delivery.kind).where(Delivery.job_id == view.id))
        ).all()
    assert sorted(kind for (kind,) in kinds) == ["alert"] + ["failure"] * 4
    resumed = await jobs.resume(tenant.user(), view.id)
    assert (resumed.status, resumed.consecutive_failures, resumed.paused_reason) == (
        "active",
        0,
        None,
    )


async def test_a_once_job_pauses_after_its_fire(
    engine: AsyncEngine, jobs: JobService, tenant: Tenant
) -> None:
    when = (datetime.now(UTC) + timedelta(hours=2)).replace(microsecond=0)
    given = JobInput(
        name="once", schedule={"kind": "once", "at": when.isoformat()}, prompt="Remind me"
    )
    view = await jobs.create(tenant.user(), given)
    assert view.next_fire_at == when
    past = datetime.now(UTC) - timedelta(seconds=5)
    async with engine.begin() as connection:
        await connection.execute(
            update(Job)
            .where(Job.id == view.id)
            .values(schedule={"kind": "once", "at": past.isoformat()}, next_fire_at=past)
        )
    assert await Scheduler(engine, RunQueue(engine), None).tick() == 1
    job = await _job(engine, view.id)
    assert (job.status, job.next_fire_at, job.paused_reason) == (
        "paused",
        None,
        "the job ran once",
    )
    async with engine.connect() as connection:
        run_id = (
            await connection.execute(select(JobFire.run_id).where(JobFire.job_id == view.id))
        ).scalar_one()
    await _end_run(engine, run_id)
