"""The scheduler loop (docs/architecture.md, Section 12.1).

The loop runs in the control plane process, next to the worker. It has four steps:

- ``tick`` (each ``TICK_S``): one process holds the leader lease (a transaction advisory lock)
  and fires the due jobs. With ``curator``, it first adds the missing curator jobs of the
  workspaces (Section 9.7). A fire runs once, also when two schedulers tick at the same time:
  the due rows are locked with ``SKIP LOCKED``, the fire row is unique on
  ``(job_id, scheduled_for)``, and the same transaction moves ``next_fire_at``.
- ``finish`` (each ``POLL_S``): writes the result of each fire whose run ended.
- ``scripts`` (each ``POLL_S``): claims and runs the fires of ``mode: script`` jobs.
- ``deliver`` (each ``POLL_S``): sends the deliveries, with receipts (``delivery.py``).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Protocol

from sqlalchemy import and_, func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from sama.curator.ensure import ensure_curator_jobs
from sama.db.models import ENDED_RUN_STATUSES, Job, JobFire, Message, Run
from sama.orchestrator.queue import RunQueue
from sama.runtime.contract import Audience, RunScope
from sama.sandbox.base import ExecResult
from sama.scheduler import schedule as sched
from sama.scheduler.delivery import Deliverer
from sama.scheduler.fire import (
    FireBlockedError,
    finish_fire,
    fire_running,
    insert_fire,
    record_failed_fire,
    start_fire,
)
from sama.vault.redact import redact

log = logging.getLogger("sama.scheduler")

TICK_S: Final = 60.0
POLL_S: Final = 5.0
BATCH: Final = 50
LEASE_KEY: Final = "sama.scheduler.tick"
SCRIPT_TIMEOUT_S: Final = 600.0
"""The longest time of a job script. It is less than the 1-hour limit of the sandbox."""
SCRIPT_MARGIN_S: Final = 300.0
MAX_SCRIPTS: Final = 4
MAX_OUTPUT_CHARS: Final = 20_000


class ScriptRunner(Protocol):
    async def exec_script(
        self, scope: RunScope, script: str, *, timeout_s: float
    ) -> ExecResult: ...


class Scheduler:
    def __init__(
        self,
        engine: AsyncEngine,
        queue: RunQueue,
        deliverer: Deliverer | None,
        *,
        scripts: ScriptRunner | None = None,
        tick_s: float = TICK_S,
        poll_s: float = POLL_S,
        script_timeout_s: float = SCRIPT_TIMEOUT_S,
        curator: bool = False,
    ) -> None:
        self._engine = engine
        self._curator = curator
        self._queue = queue
        self._deliverer = deliverer
        self._scripts = scripts
        self._tick_s = tick_s
        self._poll_s = poll_s
        self._script_timeout_s = script_timeout_s
        self._running: set[asyncio.Task[None]] = set()

    async def aclose(self) -> None:
        """Closes the HTTP client of the deliveries. Call it after ``serve`` ends."""
        if self._deliverer is not None:
            await self._deliverer.aclose()

    async def serve(self, stop: asyncio.Event) -> None:
        next_tick = time.monotonic()
        try:
            while not stop.is_set():
                if time.monotonic() >= next_tick:
                    next_tick = time.monotonic() + self._tick_s
                    await self._step("tick", self.tick)
                await self._step("finish", self.finish)
                await self._step("scripts", self.start_scripts)
                if self._deliverer is not None:
                    await self._step("deliver", self._deliverer.run_once)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=self._poll_s)
        finally:
            for task in list(self._running):
                task.cancel()
            if self._running:
                await asyncio.gather(*self._running, return_exceptions=True)

    async def _step(self, name: str, step: Any) -> None:
        try:
            await step()
        except Exception:
            log.exception("the scheduler step %s failed", name)

    # -- tick -------------------------------------------------------------------------------

    async def tick(self) -> int:
        """Fires the due jobs when this process holds the leader lease. Returns the number of
        jobs that it handled, or ``-1`` when another process holds the lease."""
        async with self._engine.begin() as connection:
            leader = (
                await connection.execute(
                    select(func.pg_try_advisory_xact_lock(func.hashtextextended(LEASE_KEY, 0)))
                )
            ).scalar_one()
            if not leader:
                return -1
            if self._curator:
                await self._ensure_curators(connection)
            jobs = (
                await connection.execute(
                    select(Job)
                    .where(Job.status == "active", Job.next_fire_at <= func.now())
                    .order_by(Job.next_fire_at)
                    .limit(BATCH)
                    .with_for_update(skip_locked=True)
                )
            ).all()
            for job in jobs:
                await self._due(connection, job)
            return len(jobs)

    async def _ensure_curators(self, connection: AsyncConnection) -> None:
        """Adds the missing curator jobs. A failure does not stop the due jobs."""
        try:
            async with connection.begin_nested():
                await ensure_curator_jobs(connection)
        except Exception:
            log.exception("the curator jobs were not added")

    async def _due(self, connection: AsyncConnection, job: Any) -> None:
        now = datetime.now(UTC)
        scheduled_for = job.next_fire_at
        values: dict[str, Any] = {"last_fire_at": scheduled_for}
        try:
            async with connection.begin_nested():
                if await fire_running(connection, job.org_id, job.id):
                    await self._skip(connection, job, scheduled_for)
                else:
                    await start_fire(
                        connection, self._queue, job, scheduled_for=scheduled_for, manual=False
                    )
        except FireBlockedError as error:
            values.update(status=error.status, next_fire_at=None, paused_reason=str(error))
        except Exception as error:
            log.exception("the job %s did not fire", job.id)
            async with connection.begin_nested():
                await record_failed_fire(
                    connection,
                    job,
                    scheduled_for=scheduled_for,
                    manual=False,
                    error=f"the fire did not start: {type(error).__name__}",
                )
        if "status" not in values:
            following = sched.next_fire(job.schedule, now, last=scheduled_for)
            values["next_fire_at"] = following
            if following is None:
                values.update(status="paused", paused_reason="the job ran once")
        # ``finish_fire`` of a failed start can pause the job. That pause wins.
        current = (
            await connection.execute(select(Job.status).where(Job.id == job.id))
        ).scalar_one()
        if current != "active":
            values = {"last_fire_at": scheduled_for}
        await connection.execute(update(Job).where(Job.id == job.id).values(**values))

    async def _skip(self, connection: AsyncConnection, job: Any, scheduled_for: datetime) -> None:
        """The last fire still runs, so this fire does not start. Fires do not overlap."""
        await insert_fire(
            connection,
            job,
            scheduled_for,
            cleared=False,
            status="skipped",
            error="the last fire still runs",
            finished_at=func.now(),
        )

    # -- finish -----------------------------------------------------------------------------

    async def finish(self) -> int:
        """Writes the result of each agent fire whose run ended."""
        async with self._engine.begin() as connection:
            rows = (
                await connection.execute(
                    select(
                        JobFire.id,
                        JobFire.org_id,
                        JobFire.job_id,
                        JobFire.run_id,
                        Run.status.label("run_status"),
                        Run.failure_message,
                    )
                    .join(Run, and_(Run.org_id == JobFire.org_id, Run.id == JobFire.run_id))
                    .where(JobFire.status == "running", Run.status.in_(ENDED_RUN_STATUSES))
                    .order_by(JobFire.created_at)
                    .limit(BATCH)
                    .with_for_update(of=JobFire, skip_locked=True)
                )
            ).all()
            for row in rows:
                output = await _answer(connection, row.org_id, row.run_id)
                ok = row.run_status == "completed"
                error = None
                if not ok:
                    error = (
                        "the run was cancelled"
                        if row.run_status == "cancelled"
                        else f"the run failed: {row.failure_message or 'the reason is not known'}"
                    )
                await finish_fire(connection, row, ok=ok, output=output, error=error)
            return len(rows)

    # -- scripts ----------------------------------------------------------------------------

    async def start_scripts(self) -> int:
        """Ends the script fires that ran too long, then claims new ones and runs them."""
        await self._expire_scripts()
        free = MAX_SCRIPTS - len(self._running)
        if free <= 0:
            return 0
        waiting = (
            select(JobFire.id)
            .where(
                JobFire.status == "running",
                JobFire.run_id.is_(None),
                JobFire.started_at.is_(None),
            )
            .order_by(JobFire.created_at)
            .limit(free)
            .with_for_update(skip_locked=True)
            .scalar_subquery()
        )
        async with self._engine.begin() as connection:
            claimed = (
                await connection.execute(
                    update(JobFire)
                    .where(JobFire.id.in_(waiting))
                    .values(started_at=func.now())
                    .returning(
                        JobFire.id,
                        JobFire.org_id,
                        JobFire.job_id,
                        JobFire.session_id,
                        JobFire.script,
                    )
                )
            ).all()
            jobs = {
                row.id: row
                for row in (
                    await connection.execute(
                        select(Job.id, Job.workspace_id, Job.owner_user_id).where(
                            Job.id.in_([fire.job_id for fire in claimed])
                        )
                    )
                ).all()
            }
        for fire in claimed:
            task = asyncio.create_task(self._run_script(fire, jobs.get(fire.job_id)))
            self._running.add(task)
            task.add_done_callback(self._running.discard)
        return len(claimed)

    async def _run_script(self, fire: Any, job: Any) -> None:
        ok, output, error = False, "", None
        try:
            if job is None or not fire.script:
                error = "the job has no script"
            elif redact(fire.script) != fire.script:
                error = "the script contains a secret, so it does not run"
            elif self._scripts is None:
                error = "script mode needs the sandbox"
            else:
                scope = RunScope(
                    org_id=str(fire.org_id),
                    workspace_id=str(job.workspace_id),
                    session_id=str(fire.session_id),
                    run_id=str(fire.id),
                    audience=Audience.PRIVATE,
                    user_id=str(job.owner_user_id),
                )
                # The fire keeps the script of the version that started it (Section 12.1).
                result = await self._scripts.exec_script(
                    scope, fire.script, timeout_s=self._script_timeout_s
                )
                output = result.stdout[-MAX_OUTPUT_CHARS:]
                ok = result.exit_code == 0
                if not ok:
                    error = f"the script ended with code {result.exit_code}: " + (
                        result.stderr.strip()[-500:] or "no error text"
                    )
        # On cancel the process stops. ``_expire_scripts`` ends the fire later.
        except Exception as caught:
            log.exception("the script of fire %s failed", fire.id)
            error = f"the script did not run: {type(caught).__name__}"
        async with self._engine.begin() as connection:
            await finish_fire(connection, fire, ok=ok, output=output, error=error)

    async def _expire_scripts(self) -> None:
        """A script fire that started long ago has no result: the process that ran it stopped.
        It fails, and it does not run again, because the script can have done its work."""
        limit = timedelta(seconds=self._script_timeout_s + SCRIPT_MARGIN_S)
        async with self._engine.begin() as connection:
            rows = (
                await connection.execute(
                    select(JobFire.id, JobFire.org_id, JobFire.job_id)
                    .where(
                        JobFire.status == "running",
                        JobFire.run_id.is_(None),
                        JobFire.started_at < func.now() - limit,
                    )
                    .limit(BATCH)
                    .with_for_update(skip_locked=True)
                )
            ).all()
            for row in rows:
                await finish_fire(
                    connection,
                    row,
                    ok=False,
                    output="",
                    error="the result of the script is not known; it does not run again",
                )


async def _answer(connection: AsyncConnection, org_id: uuid.UUID, run_id: uuid.UUID) -> str:
    """The last answer of the assistant in the run."""
    content = (
        await connection.execute(
            select(Message.content)
            .where(Message.org_id == org_id, Message.run_id == run_id, Message.role == "assistant")
            .order_by(Message.seq.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return str((content or {}).get("text") or "")
