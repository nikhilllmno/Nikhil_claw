"""One fire of a job (docs/architecture.md, Section 12.1).

``start_fire`` writes the fire, its session, and its run in the transaction of the caller. The
scheduler calls it for a due job, and ``JobService`` calls it for ``run now``. ``finish_fire``
writes the result, the deliveries, and the failure count, and pauses a job after repeated
failures with one alert.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any, Final

from sqlalchemy import func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from sama.db.models import AuditEntry, Delivery, Job, JobFire, Session, User
from sama.orchestrator.queue import RunQueue
from sama.scheduler.spec import cleared
from sama.tenancy.access import load_grants
from sama.vault.redact import redact

log = logging.getLogger("sama.scheduler")

MAX_FAILURES: Final = 3
"""After this many failed fires in a row, the job pauses and the owner gets one alert."""
MAX_DELIVERY_CHARS: Final = 3500
"""Telegram takes 4096 characters and Slack takes more. The text keeps room for the header."""
ALERT_TARGET_FALLBACK: Final = "origin"


class FireBlockedError(Exception):
    """The job cannot fire now. ``status`` is the new status of the job."""

    def __init__(self, message: str, status: str) -> None:
        super().__init__(message)
        self.status = status


async def owner_email(connection: AsyncConnection, org_id: uuid.UUID, user_id: uuid.UUID) -> Any:
    return (
        await connection.execute(
            select(User.email).where(User.org_id == org_id, User.id == user_id)
        )
    ).scalar_one_or_none()


def fire_text(job: Any, scheduled_for: datetime) -> str:
    """The user message of an agent fire. It tells the model that no human is there and that
    its last answer is the delivered result."""
    lines = [
        f'[Scheduled job "{job.name}", fire time {scheduled_for.isoformat()}. No person is in '
        "this run: do not ask questions. Your final answer is the result that sama delivers.]"
    ]
    if job.skills:
        lines.append(f"Use these skills (skill_view): {', '.join(job.skills)}.")
    lines.append(job.prompt)
    return "\n\n".join(lines)


async def start_fire(
    connection: AsyncConnection,
    queue: RunQueue,
    job: Any,
    *,
    scheduled_for: datetime,
    manual: bool,
) -> uuid.UUID | None:
    """Writes the fire of ``job`` (a locked ``jobs`` row) for ``scheduled_for``. Returns the
    fire id, or ``None`` when that fire exists (another scheduler wrote it).

    Raises ``FireBlockedError`` when the job needs an approval that it does not have, or when
    the owner cannot use the workspace now."""
    email = await owner_email(connection, job.org_id, job.owner_user_id)
    if not cleared(job, email):
        raise FireBlockedError(
            "the job version needs the approval of its owner", "pending_approval"
        )
    grants = await load_grants(connection, job.org_id, job.owner_user_id)
    if not grants.sees_workspace(job.workspace_id):
        raise FireBlockedError("the owner cannot use the workspace of the job", "paused")
    row = await insert_fire(connection, job, scheduled_for, cleared=True, manual=manual)
    if row is None:
        return None
    fire_id: uuid.UUID = row.id
    session_id = await _fire_session(connection, job)
    values: dict[str, Any] = {"session_id": session_id}
    if job.mode == "agent":
        values["run_id"] = await queue.enqueue_in(
            connection,
            org_id=job.org_id,
            session_id=session_id,
            text=fire_text(job, scheduled_for),
            user_id=job.owner_user_id,
            unattended=True,
            model=job.model,
            check_rate=False,
        )
        values["started_at"] = func.now()
    await connection.execute(update(JobFire).where(JobFire.id == fire_id).values(**values))
    await connection.execute(
        insert(AuditEntry).values(
            org_id=job.org_id,
            workspace_id=job.workspace_id,
            actor_kind="system",
            actor_user_id=job.owner_user_id,
            action="job.fired",
            target_type="job",
            target_id=str(job.id),
            details={"fire_id": str(fire_id), "version": job.version, "manual": manual},
        )
    )
    return fire_id


async def insert_fire(
    connection: AsyncConnection,
    job: Any,
    scheduled_for: datetime,
    *,
    cleared: bool,
    **values: Any,
) -> Any:
    """Writes a fire of ``job`` for ``scheduled_for``. Returns the row (``id``, ``org_id``,
    ``job_id``), or ``None`` when that fire exists.

    The fire keeps the targets and the script of this job version, so a later version that has
    no approval cannot change what the fire runs or where its result goes. When ``cleared`` is
    false (this version has no approval), the fire sends only to ``origin``."""
    if cleared:
        snapshot = {
            "deliver": job.deliver,
            "failure_deliver": job.failure_deliver,
            "script": job.script if job.mode == "script" else None,
        }
    else:
        snapshot = {"deliver": "origin", "failure_deliver": None, "script": None}
    return (
        await connection.execute(
            pg_insert(JobFire)
            .values(
                org_id=job.org_id,
                job_id=job.id,
                job_version=job.version,
                scheduled_for=scheduled_for,
                **snapshot,
                **values,
            )
            .on_conflict_do_nothing(index_elements=[JobFire.job_id, JobFire.scheduled_for])
            .returning(JobFire.id, JobFire.org_id, JobFire.job_id)
        )
    ).one_or_none()


async def fire_running(connection: AsyncConnection, org_id: uuid.UUID, job_id: uuid.UUID) -> bool:
    """A fire of the job still runs."""
    found = (
        await connection.execute(
            select(JobFire.id).where(
                JobFire.org_id == org_id, JobFire.job_id == job_id, JobFire.status == "running"
            )
        )
    ).first()
    return found is not None


async def _fire_session(connection: AsyncConnection, job: Any) -> uuid.UUID:
    """A fresh private session of the owner, or the long-lived session of the job with
    ``attach_to_session``. A long-lived session that is not active is replaced."""
    if job.attach_to_session and job.session_id is not None:
        status = (
            await connection.execute(
                select(Session.status).where(
                    Session.org_id == job.org_id,
                    Session.id == job.session_id,
                    Session.agent_profile_id == job.agent_profile_id,
                )
            )
        ).scalar_one_or_none()
        if status == "active":
            return uuid.UUID(str(job.session_id))
    session_id = uuid.uuid4()
    await connection.execute(
        insert(Session).values(
            id=session_id,
            org_id=job.org_id,
            workspace_id=job.workspace_id,
            agent_profile_id=job.agent_profile_id,
            audience="private",
            owner_user_id=job.owner_user_id,
            title=f"Job: {job.name}"[:200],
        )
    )
    if job.attach_to_session:
        await connection.execute(update(Job).where(Job.id == job.id).values(session_id=session_id))
    return session_id


def _clip(text: str) -> str:
    text = redact(text.strip())
    if len(text) <= MAX_DELIVERY_CHARS:
        return text
    return text[: MAX_DELIVERY_CHARS - 1] + "…"


async def finish_fire(
    connection: AsyncConnection,
    fire: Any,
    *,
    ok: bool,
    output: str,
    error: str | None,
) -> None:
    """Writes the result of a running fire (``fire`` has ``id``, ``org_id``, and ``job_id``).
    It writes a ``result`` delivery for a good fire, and a ``failure`` delivery for a failed
    one. The third failure in a row pauses the job and writes one ``alert``. The deliveries go
    to the targets that the fire kept when it started, not to the targets of the job now."""
    done = (
        await connection.execute(
            update(JobFire)
            .where(JobFire.id == fire.id, JobFire.status == "running")
            .values(
                status="completed" if ok else "failed",
                output=redact(output)[:20_000] if output else None,
                error=redact(error)[:2000] if error else None,
                finished_at=func.now(),
            )
            .returning(JobFire.deliver, JobFire.failure_deliver)
        )
    ).one_or_none()
    if done is None:
        return
    job = (
        await connection.execute(
            select(Job).where(Job.org_id == fire.org_id, Job.id == fire.job_id).with_for_update()
        )
    ).one_or_none()
    if job is None:
        return
    if ok:
        await connection.execute(update(Job).where(Job.id == job.id).values(consecutive_failures=0))
        if done.deliver != "none":
            body = output.strip() or "The job finished with no answer."
            await _deliver(
                connection, job, fire.id, "result", done.deliver, f"{job.name}\n\n{body}"
            )
        return
    failures = job.consecutive_failures + 1
    values: dict[str, Any] = {"consecutive_failures": failures}
    failure_target = done.failure_deliver or done.deliver
    if failure_target != "none":
        await _deliver(
            connection,
            job,
            fire.id,
            "failure",
            failure_target,
            f"{job.name}: the job failed. {error or 'The reason is not known.'}",
        )
    if failures >= MAX_FAILURES and job.status == "active":
        values.update(
            status="paused",
            next_fire_at=None,
            paused_reason=f"paused after {failures} failed fires in a row",
        )
        if job.alerted_at is None:
            values["alerted_at"] = func.now()
            # The alert always goes somewhere that the owner sees.
            alert_target = ALERT_TARGET_FALLBACK if failure_target == "none" else failure_target
            await _deliver(
                connection,
                job,
                fire.id,
                "alert",
                alert_target,
                f"{job.name}: the job failed {failures} times in a row, so sama paused it. "
                "Fix the job, then resume it with /cron resume or on the jobs page.",
            )
    await connection.execute(update(Job).where(Job.id == job.id).values(**values))


async def _deliver(
    connection: AsyncConnection, job: Any, fire_id: uuid.UUID, kind: str, target: str, text: str
) -> None:
    await connection.execute(
        pg_insert(Delivery)
        .values(
            org_id=job.org_id,
            job_id=job.id,
            fire_id=fire_id,
            kind=kind,
            target=target,
            text=_clip(text),
        )
        .on_conflict_do_nothing(index_elements=[Delivery.org_id, Delivery.fire_id, Delivery.kind])
    )


async def record_failed_fire(
    connection: AsyncConnection, job: Any, *, scheduled_for: datetime, manual: bool, error: str
) -> None:
    """A fire that could not start. It counts as a failure, so a job that cannot start pauses
    after ``MAX_FAILURES`` ticks."""
    email = await owner_email(connection, job.org_id, job.owner_user_id)
    row = await insert_fire(
        connection,
        job,
        scheduled_for,
        cleared=cleared(job, email),
        manual=manual,
        started_at=func.now(),
    )
    if row is not None:
        await finish_fire(connection, row, ok=False, output="", error=error)
