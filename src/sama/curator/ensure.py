"""Each workspace gets one curator job (docs/architecture.md, Section 9.7).

The scheduler tick calls ``ensure_curator_jobs`` under its leader lease. It adds a job for each
workspace that has an owner and an agent profile and no curator job yet. A removed curator job
keeps its row, so a person who removes it does not get it back. The unique index
``uq_jobs_curator`` stops a second job, also when two processes run this at the same time.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Final

from sqlalchemy import Select, exists, insert, or_, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from sama.curator.policy import CURATOR_NAME, curator_schedule
from sama.db.models import AgentProfile, AuditEntry, Job, Membership, Workspace
from sama.scheduler import schedule as sched

BATCH: Final = 50


def _owner() -> Select[uuid.UUID]:
    """The owner of a workspace: its own owner, else the org owner. The oldest one wins."""
    return (
        select(Membership.user_id)
        .where(
            Membership.org_id == Workspace.org_id,
            Membership.role == "owner",
            or_(Membership.workspace_id == Workspace.id, Membership.workspace_id.is_(None)),
        )
        .order_by(Membership.workspace_id.is_(None), Membership.created_at, Membership.id)
        .limit(1)
    )


def _profile() -> Select[uuid.UUID]:
    """The agent profile of the job: the oldest profile of the workspace, else of the org."""
    return (
        select(AgentProfile.id)
        .where(
            AgentProfile.org_id == Workspace.org_id,
            or_(AgentProfile.workspace_id == Workspace.id, AgentProfile.workspace_id.is_(None)),
        )
        .order_by(AgentProfile.workspace_id.is_(None), AgentProfile.created_at, AgentProfile.id)
        .limit(1)
    )


async def ensure_curator_jobs(connection: AsyncConnection, *, limit: int = BATCH) -> int:
    """Adds the missing curator jobs, at most ``limit``. Returns the number added."""
    owner = _owner().scalar_subquery()
    profile = _profile().scalar_subquery()
    has_job = exists().where(
        Job.org_id == Workspace.org_id, Job.workspace_id == Workspace.id, Job.mode == "curator"
    )
    rows = (
        await connection.execute(
            select(Workspace.id, Workspace.org_id, owner.label("owner"), profile.label("profile"))
            .where(~has_job, owner.isnot(None), profile.isnot(None))
            .order_by(Workspace.created_at, Workspace.id)
            .limit(limit)
        )
    ).all()
    added = 0
    now = datetime.now(UTC)
    for row in rows:
        schedule = curator_schedule(row.id)
        job_id = uuid.uuid4()
        done = (
            await connection.execute(
                pg_insert(Job)
                .values(
                    id=job_id,
                    org_id=row.org_id,
                    workspace_id=row.id,
                    owner_user_id=row.owner,
                    agent_profile_id=row.profile,
                    name=CURATOR_NAME,
                    schedule=schedule,
                    prompt="",
                    skills=[],
                    mode="curator",
                    deliver="origin",
                    status="active",
                    next_fire_at=sched.next_fire(schedule, now),
                    created_by_kind="system",
                )
                .on_conflict_do_nothing(
                    index_elements=[Job.org_id, Job.workspace_id],
                    index_where=text("mode = 'curator'"),
                )
                .returning(Job.id)
            )
        ).one_or_none()
        if done is None:
            continue
        added += 1
        await connection.execute(
            insert(AuditEntry).values(
                org_id=row.org_id,
                workspace_id=row.id,
                actor_kind="system",
                actor_user_id=None,
                action="job.created",
                target_type="job",
                target_id=str(job_id),
                details={"version": 1, "mode": "curator"},
            )
        )
    return added


__all__ = ["ensure_curator_jobs"]
