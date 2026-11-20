"""Database helpers that the workflow service and the engine share (docs/architecture.md,
Section 12.3).

- ``cleared`` says whether a stored version can run: it needs no approval, or its owner approved
  this version and the stored definition still has the approved hash.
- ``insert_instance`` writes an instance in the transaction of the caller.
- ``close_instance`` ends an instance, its open steps, and its child instances.
- ``workflow_of_run`` tells the gate and the service that a run is an agent step of a workflow.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from sqlalchemy import and_, func, insert, null, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from sama.db.models import (
    AuditEntry,
    Run,
    WorkflowInstance,
    WorkflowStep,
    WorkflowVersion,
)
from sama.gate.decide import Preapproval
from sama.scheduler.spec import preapprovals
from sama.tenancy.access import load_grants
from sama.workflows.spec import definition_hash, find

EPOCH: Final = datetime(2000, 1, 1, tzinfo=UTC)
"""The ``checked_at`` of an instance that the engine must advance at its next poll."""

OWNER_GONE: Final = "the owner cannot use the workspace of the workflow"

OPEN_STEP_STATUSES = ("pending", "running", "waiting", "needs_reconcile", "waiting_human")
OPEN_INSTANCE_STATUSES = ("running", "waiting")


def approved(version: Any) -> bool:
    """The owner approved this version, and its definition still has the approved hash."""
    return (
        version.approved_hash is not None
        and version.approved_hash == version.hash
        and version.hash == definition_hash(version.definition)
    )


def cleared(version: Any) -> bool:
    """The stored version can run: it needs no approval, or it has the approval."""
    return not version.needs_approval or approved(version)


async def owner_sees(
    connection: AsyncConnection, org_id: uuid.UUID, user_id: uuid.UUID, workspace_id: uuid.UUID
) -> bool:
    """The owner can still use the workspace. The engine checks it again before each step
    that acts for the owner: an owner who left the workspace loses the workflow (12.3)."""
    return (await load_grants(connection, org_id, user_id)).sees_workspace(workspace_id)


async def touch_instance(
    connection: AsyncConnection, org_id: uuid.UUID, instance_id: uuid.UUID
) -> None:
    """Asks the engine to advance the instance at its next poll."""
    await connection.execute(
        update(WorkflowInstance)
        .where(WorkflowInstance.org_id == org_id, WorkflowInstance.id == instance_id)
        .values(checked_at=EPOCH)
    )


async def load_version(
    connection: AsyncConnection, org_id: uuid.UUID, workflow_id: uuid.UUID, version: int
) -> Any:
    return (
        await connection.execute(
            select(WorkflowVersion).where(
                WorkflowVersion.org_id == org_id,
                WorkflowVersion.workflow_id == workflow_id,
                WorkflowVersion.version == version,
            )
        )
    ).one_or_none()


async def insert_instance(
    connection: AsyncConnection,
    workflow: Any,
    *,
    inputs: dict[str, Any],
    trigger: str,
    started_by_kind: str,
    started_by_run_id: uuid.UUID | None = None,
    scheduled_for: datetime | None = None,
    parent: Any = None,
    parent_step_id: str | None = None,
    toolsets: Sequence[str] | None = None,
) -> uuid.UUID | None:
    """Writes an instance of the current version of ``workflow`` (a ``workflows`` row). Returns
    the instance id, or ``None`` when an instance for ``scheduled_for`` exists. ``toolsets``
    limits the agent steps of the instance (the toolsets of a trigger). A child instance keeps the
    limit of its parent."""
    values: dict[str, Any] = {
        "org_id": workflow.org_id,
        "workflow_id": workflow.id,
        "version": workflow.version,
        "owner_user_id": workflow.owner_user_id,
        "workspace_id": workflow.workspace_id,
        "agent_profile_id": workflow.agent_profile_id,
        "status": "running",
        "inputs": inputs,
        "trigger": trigger,
        "scheduled_for": scheduled_for,
        "started_by_kind": started_by_kind,
        "started_by_run_id": started_by_run_id,
    }
    if parent is not None:
        values.update(
            parent_instance_id=parent.id, parent_step_id=parent_step_id, depth=parent.depth + 1
        )
        toolsets = parent.toolsets
    values["toolsets"] = list(toolsets) if toolsets is not None else null()
    query = pg_insert(WorkflowInstance).values(**values)
    if scheduled_for is not None:
        query = query.on_conflict_do_nothing(
            index_elements=[WorkflowInstance.workflow_id, WorkflowInstance.scheduled_for]
        )
    instance_id: uuid.UUID | None = (
        await connection.execute(query.returning(WorkflowInstance.id))
    ).scalar_one_or_none()
    if instance_id is not None:
        await connection.execute(
            insert(AuditEntry).values(
                org_id=workflow.org_id,
                workspace_id=workflow.workspace_id,
                actor_kind=started_by_kind,
                actor_user_id=workflow.owner_user_id,
                action="workflow.started",
                target_type="workflow",
                target_id=str(workflow.id),
                details={
                    "instance_id": str(instance_id),
                    "version": workflow.version,
                    "trigger": trigger,
                },
            )
        )
    return instance_id


async def close_instance(
    connection: AsyncConnection,
    org_id: uuid.UUID,
    instance_id: uuid.UUID,
    *,
    status: str,
    error: str | None = None,
    output: Any = None,
) -> None:
    """Ends an open instance with ``status`` (``completed``, ``failed``, or ``cancelled``). It
    cancels the open steps, the open child instances, and the runs of their agent steps, in the
    transaction of the caller. A crash cannot lose the cancel of a run (Section 12.3)."""
    values: dict[str, Any] = {"status": status, "error": error, "finished_at": func.now()}
    if status == "completed":
        values["output"] = output
    closed = (
        await connection.execute(
            update(WorkflowInstance)
            .where(
                WorkflowInstance.org_id == org_id,
                WorkflowInstance.id == instance_id,
                WorkflowInstance.status.in_(OPEN_INSTANCE_STATUSES),
            )
            .values(**values)
            .returning(WorkflowInstance.id)
        )
    ).scalar_one_or_none()
    if closed is None:
        return
    steps = (
        await connection.execute(
            update(WorkflowStep)
            .where(
                WorkflowStep.org_id == org_id,
                WorkflowStep.instance_id == instance_id,
                WorkflowStep.status.in_(OPEN_STEP_STATUSES),
            )
            .values(status="cancelled", lease_expires_at=None, finished_at=func.now())
            .returning(WorkflowStep.run_id, WorkflowStep.child_instance_id)
        )
    ).all()
    await _cancel_runs(connection, org_id, [step.run_id for step in steps if step.run_id])
    for step in steps:
        if step.child_instance_id is not None:
            await close_instance(
                connection,
                org_id,
                step.child_instance_id,
                status="cancelled",
                error="the parent instance ended",
            )


@dataclass(frozen=True, slots=True)
class WorkflowRun:
    """What the gate needs to know about a run that an agent step started."""

    instance_id: uuid.UUID
    step_id: str
    preapproved: tuple[Preapproval, ...]


async def workflow_of_run(
    connection: AsyncConnection, org_id: uuid.UUID, run_id: uuid.UUID
) -> WorkflowRun | None:
    """The agent step of a run, or ``None``. The pre-approvals of the step apply only when the
    owner approved the version of the instance and its stored hash matches."""
    row = (
        await connection.execute(
            select(WorkflowStep.instance_id, WorkflowStep.step_id, WorkflowVersion)
            .join(
                WorkflowInstance,
                and_(
                    WorkflowInstance.org_id == WorkflowStep.org_id,
                    WorkflowInstance.id == WorkflowStep.instance_id,
                ),
            )
            .join(
                WorkflowVersion,
                and_(
                    WorkflowVersion.org_id == WorkflowInstance.org_id,
                    WorkflowVersion.workflow_id == WorkflowInstance.workflow_id,
                    WorkflowVersion.version == WorkflowInstance.version,
                ),
            )
            .where(WorkflowStep.org_id == org_id, WorkflowStep.run_id == run_id)
        )
    ).one_or_none()
    if row is None:
        return None
    preapproved: tuple[Preapproval, ...] = ()
    if approved(row):
        step = find(row.definition["steps"], row.step_id)
        if step is not None and step["kind"] == "agent":
            preapproved = preapprovals(step["preapproved_tools"])
    return WorkflowRun(instance_id=row.instance_id, step_id=row.step_id, preapproved=preapproved)


__all__ = [
    "OPEN_INSTANCE_STATUSES",
    "OPEN_STEP_STATUSES",
    "WorkflowRun",
    "approved",
    "cleared",
    "close_instance",
    "insert_instance",
    "load_version",
    "workflow_of_run",
]


async def _cancel_runs(
    connection: AsyncConnection, org_id: uuid.UUID, run_ids: Sequence[uuid.UUID]
) -> None:
    """Cancels the runs of agent steps with the rules of ``RunQueue.cancel``: a queued or
    paused run ends at once, and the worker stops a running run at its next heartbeat."""
    if not run_ids:
        return
    scope = (Run.org_id == org_id, Run.id.in_(list(run_ids)))
    # Lock the runs first: without the lock, the queue can requeue a running run between the
    # two updates, and the cancel misses it.
    await connection.execute(select(Run.id).where(*scope).order_by(Run.id).with_for_update())
    await connection.execute(
        update(Run)
        .where(*scope, Run.status.in_(("queued", "paused")))
        .values(
            status="cancelled",
            failure_code="cancelled",
            failure_message="cancelled",
            cancel_requested_at=func.now(),
            finished_at=func.now(),
        )
    )
    await connection.execute(
        update(Run)
        .where(*scope, Run.status == "running", Run.cancel_requested_at.is_(None))
        .values(cancel_requested_at=func.now())
    )
