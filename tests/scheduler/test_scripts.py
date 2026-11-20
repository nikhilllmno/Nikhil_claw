"""Script fires with a fake sandbox (M4.2, ``mode: script``)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.db.models import Delivery, JobFire
from sama.orchestrator.queue import RunQueue
from sama.runtime import RunScope
from sama.sandbox import ExecResult
from sama.scheduler.jobs import JobService
from sama.scheduler.service import Scheduler
from sama.scheduler.spec import JobInput
from tests.scheduler.conftest import Tenant


class FakeScripts:
    def __init__(self, result: ExecResult) -> None:
        self.result = result
        self.calls: list[tuple[RunScope, str, float]] = []

    async def exec_script(self, scope: RunScope, script: str, *, timeout_s: float) -> ExecResult:
        self.calls.append((scope, script, timeout_s))
        return self.result


SCRIPT = JobInput(
    name="disk", schedule="every day at 6am", mode="script", script="df -h", deliver="none"
)


async def _run(engine: AsyncEngine, jobs: JobService, tenant: Tenant, scripts: object) -> Any:
    view = await jobs.create(tenant.user(), SCRIPT)
    fire_id = await jobs.run_now(tenant.user(), view.id)
    scheduler = Scheduler(
        engine,
        RunQueue(engine),
        None,
        scripts=scripts,  # type: ignore[arg-type]
        script_timeout_s=60,
    )
    assert await scheduler.start_scripts() == 1
    for task in list(scheduler._running):
        await task
    async with engine.connect() as connection:
        return (await connection.execute(select(JobFire).where(JobFire.id == fire_id))).one()


async def test_a_script_fire_runs_in_the_sandbox_with_no_run(
    engine: AsyncEngine, jobs: JobService, tenant: Tenant
) -> None:
    scripts = FakeScripts(ExecResult(0, "40% used\n", ""))
    fire = await _run(engine, jobs, tenant, scripts)
    assert (fire.status, fire.output, fire.run_id) == ("completed", "40% used\n", None)
    (scope, script, timeout_s) = scripts.calls[0]
    assert (script, timeout_s) == ("df -h", 60)
    assert (scope.user_id, scope.run_id) == (str(tenant.ids["ana"]), str(fire.id))


async def test_a_script_that_fails_sends_the_failure(
    engine: AsyncEngine, jobs: JobService, tenant: Tenant
) -> None:
    fire = await _run(engine, jobs, tenant, FakeScripts(ExecResult(2, "", "no disk")))
    assert (fire.status, fire.error) == ("failed", "the script ended with code 2: no disk")
    async with engine.connect() as connection:
        kinds = (
            await connection.execute(select(Delivery.kind).where(Delivery.fire_id == fire.id))
        ).all()
    assert kinds == [], "deliver none sends no failure message"


async def test_script_mode_needs_the_sandbox(
    engine: AsyncEngine, jobs: JobService, tenant: Tenant
) -> None:
    fire = await _run(engine, jobs, tenant, None)
    assert (fire.status, fire.error) == ("failed", "script mode needs the sandbox")
