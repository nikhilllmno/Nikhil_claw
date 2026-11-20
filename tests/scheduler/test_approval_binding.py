"""A fire uses the job version that it started with, and only an explicit approval approves a
version that a person did not see (M4.2 and M4.3 review findings)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.db.models import Delivery, JobFire
from sama.orchestrator.queue import RunQueue
from sama.sandbox import ExecResult
from sama.scheduler.fire import finish_fire
from sama.scheduler.jobs import JobService
from sama.scheduler.schedule import ScheduleError, validate
from sama.scheduler.service import Scheduler
from sama.scheduler.spec import JobError, JobInput, PreapprovalIn
from sama.vault.redact import default_redactor
from tests.scheduler.conftest import Tenant
from tests.scheduler.test_scripts import FakeScripts

EVERY_HOUR = JobInput(name="news", schedule="every hour", prompt="Summarize the news")
SCRIPT = JobInput(name="disk", schedule="every day at 6am", mode="script", script="df -h")
BASH = [PreapprovalIn(tool="Bash", limits={"command": ["make report"]})]


async def test_a_secret_in_a_script_or_a_prompt_is_refused(
    jobs: JobService, tenant: Tenant
) -> None:
    secret = "zz-known-secret-value-4711"
    default_redactor().add(secret)
    try:
        with pytest.raises(JobError, match="the script contains a secret"):
            await jobs.create(
                tenant.user(), SCRIPT.model_copy(update={"script": f"curl -H 'k: {secret}' x"})
            )
        with pytest.raises(JobError, match="the prompt contains a secret"):
            await jobs.create(tenant.user(), EVERY_HOUR.model_copy(update={"prompt": secret}))
    finally:
        default_redactor().forget(secret)


async def test_a_fire_runs_the_script_of_its_own_version(
    engine: AsyncEngine, jobs: JobService, tenant: Tenant
) -> None:
    view = await jobs.create(tenant.user(), SCRIPT)
    fire_id = await jobs.run_now(tenant.user(), view.id)
    changed = await jobs.update(tenant.agent(), view.id, JobInput(script="rm -rf data"))
    assert (changed.status, changed.version) == ("pending_approval", 2)
    scripts = FakeScripts(ExecResult(0, "ok", ""))
    scheduler = Scheduler(engine, RunQueue(engine), None, scripts=scripts, script_timeout_s=60)
    assert await scheduler.start_scripts() == 1
    for task in list(scheduler._running):
        await task
    assert [script for (_, script, _) in scripts.calls] == ["df -h"]
    async with engine.connect() as connection:
        status = (
            await connection.execute(select(JobFire.status).where(JobFire.id == fire_id))
        ).scalar_one()
    assert status == "completed"


async def test_a_fire_delivers_to_the_targets_of_its_own_version(
    engine: AsyncEngine, jobs: JobService, tenant: Tenant
) -> None:
    view = await jobs.create(tenant.user(), EVERY_HOUR)
    fire_id = await jobs.run_now(tenant.user(), view.id)
    changed = await jobs.update(
        tenant.agent(),
        view.id,
        JobInput(deliver="email:evil@example.test", failure_deliver="email:evil@example.test"),
    )
    assert changed.status == "pending_approval"
    async with engine.begin() as connection:
        row = (
            await connection.execute(
                select(JobFire.id, JobFire.org_id, JobFire.job_id).where(JobFire.id == fire_id)
            )
        ).one()
        await finish_fire(connection, row, ok=True, output="private news", error=None)
    async with engine.connect() as connection:
        targets = (
            await connection.execute(select(Delivery.target).where(Delivery.fire_id == fire_id))
        ).all()
    assert [target for (target,) in targets] == ["origin"]
    await jobs.cancel(tenant.user(), view.id)


async def test_a_change_by_a_person_does_not_approve_an_unseen_agent_version(
    jobs: JobService, tenant: Tenant
) -> None:
    view = await jobs.create(tenant.user(), EVERY_HOUR)
    widened = await jobs.update(tenant.agent(), view.id, JobInput(preapproved_tools=BASH))
    assert (widened.status, widened.version, widened.approved_version) == (
        "pending_approval",
        2,
        1,
    )
    renamed = await jobs.update(tenant.user(), view.id, JobInput(name="morning news"))
    assert (renamed.status, renamed.approved_version) == ("pending_approval", 1)
    edited = await jobs.update(tenant.user(), view.id, JobInput(prompt="Only the headlines"))
    assert (edited.status, edited.version, edited.approved_version) == ("pending_approval", 3, 1)
    approved = await jobs.approve(tenant.user(), view.id, 3)
    assert (approved.status, approved.approved_version) == ("active", 3)


async def test_a_resume_does_not_approve_an_unseen_version(
    jobs: JobService, tenant: Tenant
) -> None:
    view = await jobs.create(tenant.user(), EVERY_HOUR)
    await jobs.pause(tenant.user(), view.id)
    widened = await jobs.update(tenant.agent(), view.id, JobInput(preapproved_tools=BASH))
    assert (widened.status, widened.version) == ("paused", 2)
    resumed = await jobs.resume(tenant.user(), view.id)
    assert (resumed.status, resumed.approved_version) == ("pending_approval", 1)


async def test_a_person_approves_their_own_change_of_an_approved_version(
    jobs: JobService, tenant: Tenant
) -> None:
    view = await jobs.create(tenant.user(), EVERY_HOUR)
    widened = await jobs.update(tenant.user(), view.id, JobInput(preapproved_tools=BASH))
    assert (widened.status, widened.version, widened.approved_version) == ("active", 2, 2)


def test_a_cron_gap_after_the_first_fires_is_refused() -> None:
    with pytest.raises(ScheduleError, match="at most once in 5 minutes"):
        validate({"kind": "cron", "expr": "0,5,10,15,20,25,30,35,40,45,50,55,59 * * * *"})
    assert validate({"kind": "cron", "expr": "*/5 * * * *"})["expr"] == "*/5 * * * *"


def test_a_cron_gap_across_two_days_is_refused() -> None:
    now = datetime(2026, 11, 20, 10, 30, tzinfo=UTC)
    with pytest.raises(ScheduleError, match="at most once in 5 minutes"):
        validate({"kind": "cron", "expr": "0,59 0,23 31 * mon", "tz": "Asia/Kolkata"}, now=now)


def test_a_cron_gap_that_a_daylight_saving_change_makes_short_is_refused() -> None:
    now = datetime(2026, 12, 1, tzinfo=UTC)
    with pytest.raises(ScheduleError, match="at most once in 5 minutes"):
        validate({"kind": "cron", "expr": "0,58 1,3 * * *", "tz": "America/New_York"}, now=now)
    every_two_hours = {"kind": "cron", "expr": "0 */2 * * *", "tz": "America/New_York"}
    assert validate(every_two_hours, now=now)["expr"] == "0 */2 * * *"
