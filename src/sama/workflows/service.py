"""The workflow service: create, change, approve, start, and steer workflows (docs/architecture.md,
Section 12.3).

The tools ``workflow_manage`` and ``workflow_run``, the web API, and the triggers of M4.4
(``start_in``) use this one service, so they edit the same rows with the same rules:

- A workflow belongs to its owner. Only the owner sees it, changes it, approves it, and starts
  it.
- Each change of the definition is a new version. An instance runs the version that it started
  with.
- A version that writes to an external system (``needs_approval``) is ``pending_approval``
  until its owner approves that version. An agent cannot approve, decide an approval step, or
  resolve a step that waits for a human.
- An agent step of a workflow cannot create, change, or start workflows (the recursion guard).
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Final, Literal

from sqlalchemy import and_, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from sama.db.models import (
    AgentProfile,
    AuditEntry,
    Run,
    Workflow,
    WorkflowInstance,
    WorkflowReceipt,
    WorkflowStep,
    WorkflowVersion,
    Workspace,
)
from sama.orchestrator.queue import RunQueue
from sama.orgs import permissions
from sama.scheduler import schedule as schedules
from sama.scheduler.jobs import Actor
from sama.tenancy.access import load_grants
from sama.vault.redact import redact
from sama.workflows.spec import (
    AccessOf,
    WorkflowError,
    WorkflowNotFoundError,
    approval_reasons,
    definition_hash,
    first_fire,
    needs_approval,
    parse,
    resolve_inputs,
)
from sama.workflows.store import (
    OPEN_INSTANCE_STATUSES,
    OWNER_GONE,
    approved,
    cleared,
    close_instance,
    insert_instance,
    load_version,
    owner_sees,
    touch_instance,
    workflow_of_run,
)

MAX_WORKFLOWS_PER_USER: Final = 50

Rows = list[dict[str, Any]]


def _present[T](value: T | None) -> T:
    if value is None:
        raise WorkflowError("the workflow changed at the same time. Try again")
    return value


RECENT_INSTANCES: Final = 10
MAX_LIST: Final = 50
MAX_VIEW_CHARS: Final = 4000

Resolution = Literal["done", "retry", "fail"]


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _clip(value: Any) -> Any:
    """A short form of an output for a view."""
    if isinstance(value, str) and len(value) > MAX_VIEW_CHARS:
        return value[: MAX_VIEW_CHARS - 1] + "…"
    return value


class WorkflowService:
    def __init__(self, engine: AsyncEngine, queue: RunQueue, access_of: AccessOf) -> None:
        self._engine = engine
        self._queue = queue
        self._access_of = access_of

    # -- definitions ------------------------------------------------------------------------

    def validate(self, source: str) -> dict[str, Any]:
        """Checks a YAML text. Returns the checked definition and whether it needs an
        approval. Raises ``WorkflowError``."""
        definition = self._parse(source)
        now = datetime.now(UTC)
        return {
            "definition": definition,
            "hash": definition_hash(definition),
            "needs_approval": needs_approval(definition, self._access_of),
            "approval_reasons": approval_reasons(definition, self._access_of),
            "next_fire_at": _iso(first_fire(definition, now)),
        }

    def _parse(self, source: str) -> dict[str, Any]:
        if redact(source) != source:
            raise WorkflowError(
                "the workflow contains a secret. Keep secrets in the vault, and use a connection."
            )
        return parse(source, access_of=self._access_of)

    async def create(
        self,
        actor: Actor,
        source: str,
        *,
        workspace_id: uuid.UUID | None = None,
        agent_profile_id: uuid.UUID | None = None,
    ) -> dict[str, Any]:
        await self._guard(actor)
        definition = self._parse(source)
        async with self._engine.begin() as connection:
            try:
                await permissions.require_user(
                    connection, actor.org_id, actor.user_id, "workflows.create"
                )
            except permissions.PermissionDeniedError as error:
                raise WorkflowError(str(error)) from None
            count = (
                await connection.execute(
                    select(func.count()).where(
                        Workflow.org_id == actor.org_id,
                        Workflow.owner_user_id == actor.user_id,
                        Workflow.status != "removed",
                    )
                )
            ).scalar_one()
            if count >= MAX_WORKFLOWS_PER_USER:
                raise WorkflowError(
                    f"you have {MAX_WORKFLOWS_PER_USER} workflows. Remove one first."
                )
            await self._name_free(connection, actor, definition["name"], None)
            workspace, profile = await self._place(
                connection, actor, workspace_id, agent_profile_id
            )
            workflow_id = uuid.uuid4()
            needs = needs_approval(definition, self._access_of)
            await connection.execute(
                insert(Workflow).values(
                    id=workflow_id,
                    org_id=actor.org_id,
                    workspace_id=workspace,
                    owner_user_id=actor.user_id,
                    agent_profile_id=profile,
                    name=definition["name"],
                    description=definition["description"],
                    version=1,
                    origin_session_id=actor.session_id,
                    created_by_kind=actor.kind,
                    created_by_run_id=actor.run_id,
                    **self._state(definition, needs, last=None),
                )
            )
            await self._insert_version(connection, actor, workflow_id, 1, source, definition, needs)
            await self._audit(connection, actor, "workflow.created", workflow_id, workspace, 1)
            return await self._view(connection, actor, workflow_id)

    async def update(self, actor: Actor, workflow_id: uuid.UUID, source: str) -> dict[str, Any]:
        """A new version when the definition changed. A version that needs an approval waits
        for one, also when the version before it had one."""
        await self._guard(actor)
        definition = self._parse(source)
        async with self._engine.begin() as connection:
            row = await self._owned(connection, actor, workflow_id, lock=True)
            current = await load_version(connection, actor.org_id, workflow_id, row.version)
            if current is not None and current.hash == definition_hash(definition):
                return await self._view(connection, actor, workflow_id)
            await self._name_free(connection, actor, definition["name"], workflow_id)
            version = row.version + 1
            needs = needs_approval(definition, self._access_of)
            values: dict[str, Any] = {
                "name": definition["name"],
                "description": definition["description"],
                "version": version,
                "updated_at": func.now(),
            }
            if row.status in ("active", "pending_approval"):
                values.update(self._state(definition, needs, last=row.last_fire_at))
            await connection.execute(
                update(Workflow)
                .where(Workflow.org_id == actor.org_id, Workflow.id == workflow_id)
                .values(**values)
            )
            await self._insert_version(
                connection, actor, workflow_id, version, source, definition, needs
            )
            await self._audit(
                connection, actor, "workflow.updated", workflow_id, row.workspace_id, version
            )
            return await self._view(connection, actor, workflow_id)

    async def approve(self, actor: Actor, workflow_id: uuid.UUID, version: int) -> dict[str, Any]:
        """The owner approves ``version``, the version that the owner saw. An approval of an old
        version fails, so a change after the owner looked needs a new look."""
        if actor.kind != "user":
            raise WorkflowError("only a person can approve a workflow")
        async with self._engine.begin() as connection:
            row = await self._owned(connection, actor, workflow_id, lock=True)
            if version != row.version:
                raise WorkflowError(
                    f"the workflow changed: it is version {row.version} now, not {version}. "
                    "Look at it again, then approve it."
                )
            current = await load_version(connection, actor.org_id, workflow_id, row.version)
            if current is None:
                raise WorkflowNotFoundError("the workflow version does not exist")
            await connection.execute(
                update(WorkflowVersion)
                .where(WorkflowVersion.org_id == actor.org_id, WorkflowVersion.id == current.id)
                .values(
                    approved_hash=definition_hash(current.definition),
                    approved_by_user_id=actor.user_id,
                    approved_at=func.now(),
                )
            )
            if row.status == "pending_approval":
                await connection.execute(
                    update(Workflow)
                    .where(Workflow.org_id == actor.org_id, Workflow.id == workflow_id)
                    .values(
                        updated_at=func.now(),
                        **self._state(current.definition, False, last=row.last_fire_at),
                    )
                )
            await self._audit(
                connection, actor, "workflow.approved", workflow_id, row.workspace_id, version
            )
            return await self._view(connection, actor, workflow_id)

    async def pause(self, actor: Actor, workflow_id: uuid.UUID) -> dict[str, Any]:
        return _present(await self._set_status(actor, workflow_id, "pause"))

    async def resume(self, actor: Actor, workflow_id: uuid.UUID) -> dict[str, Any]:
        """Resumes the workflow. A version that waits for an approval stays
        ``pending_approval``: a resume does not approve it."""
        return _present(await self._set_status(actor, workflow_id, "resume"))

    async def remove(self, actor: Actor, workflow_id: uuid.UUID) -> None:
        await self._set_status(actor, workflow_id, "remove")

    async def _set_status(
        self, actor: Actor, workflow_id: uuid.UUID, action: str
    ) -> dict[str, Any] | None:
        await self._guard(actor)
        async with self._engine.begin() as connection:
            row = await self._owned(connection, actor, workflow_id, lock=True)
            values: dict[str, Any] = {"updated_at": func.now()}
            if action == "pause":
                values.update(status="paused", next_fire_at=None)
            elif action == "remove":
                values.update(status="removed", next_fire_at=None)
            else:
                current = await load_version(connection, actor.org_id, workflow_id, row.version)
                if current is None:
                    raise WorkflowNotFoundError("the workflow version does not exist")
                values.update(
                    self._state(current.definition, not cleared(current), last=row.last_fire_at)
                )
            await connection.execute(
                update(Workflow)
                .where(Workflow.org_id == actor.org_id, Workflow.id == workflow_id)
                .values(**values)
            )
            await self._audit(
                connection, actor, f"workflow.{action}", workflow_id, row.workspace_id
            )
            if action == "remove":
                return None
            return await self._view(connection, actor, workflow_id)

    # -- reads ------------------------------------------------------------------------------

    async def list(self, actor: Actor) -> Rows:
        async with self._engine.connect() as connection:
            rows = (
                await connection.execute(
                    select(Workflow)
                    .where(
                        Workflow.org_id == actor.org_id,
                        Workflow.owner_user_id == actor.user_id,
                        Workflow.status != "removed",
                    )
                    .order_by(Workflow.created_at)
                )
            ).all()
        return [self._summary(row) for row in rows]

    async def get(self, actor: Actor, workflow_id: uuid.UUID) -> dict[str, Any]:
        async with self._engine.connect() as connection:
            return await self._view(connection, actor, workflow_id, instances=True)

    async def find(self, actor: Actor, ref: str) -> uuid.UUID:
        """The id of a workflow of the owner, from its id or its name."""
        try:
            workflow_id = uuid.UUID(ref)
        except ValueError:
            workflow_id = None
        async with self._engine.connect() as connection:
            if workflow_id is not None:
                return uuid.UUID(str((await self._owned(connection, actor, workflow_id)).id))
            found = (
                await connection.execute(
                    select(Workflow.id).where(
                        Workflow.org_id == actor.org_id,
                        Workflow.owner_user_id == actor.user_id,
                        Workflow.name == ref,
                        Workflow.status != "removed",
                    )
                )
            ).scalar_one_or_none()
        if found is None:
            raise WorkflowNotFoundError("the workflow does not exist")
        return uuid.UUID(str(found))

    def _summary(self, row: Any) -> dict[str, Any]:
        return {
            "id": str(row.id),
            "name": row.name,
            "description": row.description,
            "version": row.version,
            "status": row.status,
            "next_fire_at": _iso(row.next_fire_at),
            "last_fire_at": _iso(row.last_fire_at),
            "workspace_id": str(row.workspace_id),
            "agent_profile_id": str(row.agent_profile_id),
            "created_by_kind": row.created_by_kind,
            "created_at": _iso(row.created_at),
            "updated_at": _iso(row.updated_at),
        }

    async def _view(
        self,
        connection: AsyncConnection,
        actor: Actor,
        workflow_id: uuid.UUID,
        *,
        instances: bool = False,
    ) -> dict[str, Any]:
        row = await self._owned(connection, actor, workflow_id)
        current = await load_version(connection, actor.org_id, workflow_id, row.version)
        view = self._summary(row)
        if current is not None:
            view.update(
                source=current.source,
                definition=current.definition,
                hash=current.hash,
                needs_approval=current.needs_approval and not approved(current),
                approved=approved(current),
                approval_reasons=approval_reasons(current.definition, self._access_of),
                triggers=[
                    {**trigger, "text": schedules.describe(trigger)}
                    for trigger in current.definition.get("triggers", [])
                ],
            )
        if instances:
            rows = (
                await connection.execute(
                    select(WorkflowInstance)
                    .where(
                        WorkflowInstance.org_id == actor.org_id,
                        WorkflowInstance.workflow_id == workflow_id,
                    )
                    .order_by(WorkflowInstance.created_at.desc())
                    .limit(RECENT_INSTANCES)
                )
            ).all()
            view["instances"] = [self._instance_summary(item, row.name) for item in rows]
        return view

    # -- instances --------------------------------------------------------------------------

    async def start(
        self, actor: Actor, workflow_id: uuid.UUID, inputs: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        """Starts an instance now. A paused workflow can start by hand; a version that waits
        for an approval cannot."""
        await self._guard(actor)
        async with self._engine.begin() as connection:
            row = await self._owned(connection, actor, workflow_id, lock=True)
            instance_id = await self._start(
                connection,
                row,
                inputs,
                trigger="manual",
                started_by_kind=actor.kind,
                started_by_run_id=actor.run_id,
                allowed=("active", "paused"),
            )
        return await self.instance(actor, _present(instance_id))

    async def start_in(
        self,
        connection: AsyncConnection,
        *,
        org_id: uuid.UUID,
        workflow_id: uuid.UUID,
        inputs: Mapping[str, Any] | None,
        trigger: str,
        scheduled_for: datetime | None = None,
        toolsets: Sequence[str] | None = None,
        event: str | None = None,
    ) -> uuid.UUID | None:
        """Starts an instance in the transaction of the caller (the triggers of M4.4 and the
        schedule tick). Returns the instance id, or ``None`` when an instance for
        ``scheduled_for`` exists. Raises ``WorkflowError`` when the workflow is not active or
        its version waits for an approval. ``toolsets`` limits the agent steps of the instance
        (the toolsets of a trigger, Section 12.2). ``event`` is the text of a trigger
        event. It becomes the input ``event`` when the workflow defines that input."""
        row = (
            await connection.execute(
                select(Workflow)
                .where(
                    Workflow.org_id == org_id,
                    Workflow.id == workflow_id,
                    Workflow.status != "removed",
                )
                .with_for_update()
            )
        ).one_or_none()
        if row is None:
            raise WorkflowNotFoundError("the workflow does not exist")
        return await self._start(
            connection,
            row,
            inputs,
            trigger=trigger,
            started_by_kind="system",
            scheduled_for=scheduled_for,
            allowed=("active",),
            toolsets=toolsets,
            event=event,
        )

    async def _start(
        self,
        connection: AsyncConnection,
        row: Any,
        inputs: Mapping[str, Any] | None,
        *,
        trigger: str,
        started_by_kind: str,
        started_by_run_id: uuid.UUID | None = None,
        scheduled_for: datetime | None = None,
        allowed: tuple[str, ...],
        toolsets: Sequence[str] | None = None,
        event: str | None = None,
    ) -> uuid.UUID | None:
        if row.status == "pending_approval":
            raise WorkflowError(
                f"workflow {row.name} version {row.version} needs the approval of its owner"
            )
        if row.status not in allowed:
            raise WorkflowError(f"workflow {row.name} is {row.status}")
        current = await load_version(connection, row.org_id, row.id, row.version)
        if current is None or not cleared(current):
            raise WorkflowError(
                f"workflow {row.name} version {row.version} needs the approval of its owner"
            )
        if not await owner_sees(connection, row.org_id, row.owner_user_id, row.workspace_id):
            raise WorkflowError(OWNER_GONE)
        given = dict(inputs or {})
        if event is not None and "event" in current.definition["inputs"]:
            given.setdefault("event", _clip_event(event))
        return await insert_instance(
            connection,
            row,
            inputs=resolve_inputs(current.definition, given),
            trigger=trigger,
            started_by_kind=started_by_kind,
            started_by_run_id=started_by_run_id,
            scheduled_for=scheduled_for,
            toolsets=toolsets,
        )

    async def instances(
        self, actor: Actor, workflow_id: uuid.UUID | None = None, *, limit: int = 20
    ) -> Rows:
        query = (
            select(WorkflowInstance, Workflow.name)
            .join(
                Workflow,
                and_(
                    Workflow.org_id == WorkflowInstance.org_id,
                    Workflow.id == WorkflowInstance.workflow_id,
                ),
            )
            .where(
                WorkflowInstance.org_id == actor.org_id,
                WorkflowInstance.owner_user_id == actor.user_id,
            )
            .order_by(WorkflowInstance.created_at.desc())
            .limit(max(1, min(limit, MAX_LIST)))
        )
        if workflow_id is not None:
            query = query.where(WorkflowInstance.workflow_id == workflow_id)
        async with self._engine.connect() as connection:
            rows = (await connection.execute(query)).all()
        return [self._instance_summary(row, row.name) for row in rows]

    async def instance(self, actor: Actor, instance_id: uuid.UUID) -> dict[str, Any]:
        async with self._engine.connect() as connection:
            row = await self._instance(connection, actor, instance_id)
            name = (
                await connection.execute(
                    select(Workflow.name).where(
                        Workflow.org_id == actor.org_id, Workflow.id == row.workflow_id
                    )
                )
            ).scalar_one()
            steps = (
                await connection.execute(
                    select(WorkflowStep)
                    .where(
                        WorkflowStep.org_id == actor.org_id,
                        WorkflowStep.instance_id == instance_id,
                    )
                    .order_by(WorkflowStep.created_at, WorkflowStep.step_id)
                )
            ).all()
            receipts = (
                await connection.execute(
                    select(WorkflowReceipt)
                    .where(
                        WorkflowReceipt.org_id == actor.org_id,
                        WorkflowReceipt.instance_id == instance_id,
                    )
                    .order_by(WorkflowReceipt.created_at)
                )
            ).all()
        view = self._instance_summary(row, name)
        view["steps"] = [
            {
                "step_id": step.step_id,
                "kind": step.kind,
                "status": step.status,
                "input": step.input,
                "output": _clip(step.output),
                "error": step.error,
                "attempts": step.attempts,
                "operation_id": step.operation_id,
                "session_id": str(step.session_id) if step.session_id else None,
                "run_id": str(step.run_id) if step.run_id else None,
                "child_instance_id": (
                    str(step.child_instance_id) if step.child_instance_id else None
                ),
                "wake_at": _iso(step.wake_at),
                "event": step.event,
                "started_at": _iso(step.started_at),
                "finished_at": _iso(step.finished_at),
            }
            for step in steps
        ]
        view["receipts"] = [
            {
                "step_id": receipt.step_id,
                "operation_id": receipt.operation_id,
                "attempt": receipt.attempt,
                "tool": receipt.tool,
                "status": receipt.status,
                "idempotency_key": receipt.idempotency_key,
                "response": _clip(receipt.response),
                "error": receipt.error,
                "created_at": _iso(receipt.created_at),
                "finished_at": _iso(receipt.finished_at),
            }
            for receipt in receipts
        ]
        return view

    def _instance_summary(self, row: Any, name: str) -> dict[str, Any]:
        return {
            "id": str(row.id),
            "workflow_id": str(row.workflow_id),
            "workflow": name,
            "version": row.version,
            "status": row.status,
            "inputs": row.inputs,
            "output": _clip(row.output),
            "error": row.error,
            "trigger": row.trigger,
            "scheduled_for": _iso(row.scheduled_for),
            "parent_instance_id": str(row.parent_instance_id) if row.parent_instance_id else None,
            "depth": row.depth,
            "created_at": _iso(row.created_at),
            "finished_at": _iso(row.finished_at),
        }

    async def cancel(self, actor: Actor, instance_id: uuid.UUID) -> dict[str, Any]:
        """Cancels an open instance, its open steps, the runs of its agent steps, and its child
        instances."""
        await self._guard(actor)
        async with self._engine.begin() as connection:
            row = await self._instance(connection, actor, instance_id, lock=True)
            if row.status not in OPEN_INSTANCE_STATUSES:
                raise WorkflowError(f"the instance is {row.status}")
            await close_instance(
                connection, actor.org_id, instance_id, status="cancelled", error="cancelled"
            )
            await self._audit(
                connection,
                actor,
                "workflow.cancelled",
                row.workflow_id,
                row.workspace_id,
                details={"instance_id": str(instance_id)},
            )
        return await self.instance(actor, instance_id)

    async def signal(
        self, actor: Actor, instance_id: uuid.UUID, event: str, data: Any = None
    ) -> int:
        """Sends ``event`` to the open instance. Each ``wait`` step that waits for it ends with
        ``data`` as its output. Returns the number of steps that ended."""
        await self._guard(actor)
        async with self._engine.begin() as connection:
            row = await self._instance(connection, actor, instance_id, lock=True)
            if row.status not in OPEN_INSTANCE_STATUSES:
                raise WorkflowError(f"the instance is {row.status}")
            ended = (
                await connection.execute(
                    update(WorkflowStep)
                    .where(
                        WorkflowStep.org_id == actor.org_id,
                        WorkflowStep.instance_id == instance_id,
                        WorkflowStep.kind == "wait",
                        WorkflowStep.status == "waiting",
                        WorkflowStep.event == event,
                    )
                    .values(status="completed", output=data, finished_at=func.now())
                    .returning(WorkflowStep.id)
                )
            ).all()
            await touch_instance(connection, actor.org_id, instance_id)
            return len(ended)

    async def decide(
        self, actor: Actor, instance_id: uuid.UUID, step_id: str, *, approve: bool
    ) -> dict[str, Any]:
        """The owner approves or rejects an approval step. A rejection fails the instance."""
        if actor.kind != "user":
            raise WorkflowError("only a person can decide an approval step")
        async with self._engine.begin() as connection:
            row = await self._instance(connection, actor, instance_id, lock=True)
            values: dict[str, Any] = {
                "resolved_by_user_id": actor.user_id,
                "finished_at": func.now(),
            }
            if approve:
                values.update(status="completed", output={"approved": True})
            else:
                values.update(status="failed", error="the owner rejected the step")
            done = (
                await connection.execute(
                    update(WorkflowStep)
                    .where(
                        WorkflowStep.org_id == actor.org_id,
                        WorkflowStep.instance_id == instance_id,
                        WorkflowStep.step_id == step_id,
                        WorkflowStep.kind == "approval",
                        WorkflowStep.status == "waiting",
                    )
                    .values(**values)
                    .returning(WorkflowStep.id)
                )
            ).scalar_one_or_none()
            if done is None:
                raise WorkflowError(f"step {step_id} does not wait for an approval")
            await touch_instance(connection, actor.org_id, instance_id)
            await self._audit(
                connection,
                actor,
                "workflow.step_approved" if approve else "workflow.step_rejected",
                row.workflow_id,
                row.workspace_id,
                details={"instance_id": str(instance_id), "step_id": step_id},
            )
        return await self.instance(actor, instance_id)

    async def resolve(
        self,
        actor: Actor,
        instance_id: uuid.UUID,
        step_id: str,
        action: Resolution,
        output: Any = None,
    ) -> dict[str, Any]:
        """The owner resolves a tool step whose result is not known (``waiting_human`` or
        ``needs_reconcile``): ``done`` (the write happened; ``output`` is its output),
        ``retry`` (the write did not happen; call the tool again), or ``fail``."""
        if actor.kind != "user":
            raise WorkflowError("only a person can resolve a step")
        if action not in ("done", "retry", "fail"):
            raise WorkflowError("the action must be done, retry, or fail")
        async with self._engine.begin() as connection:
            row = await self._instance(connection, actor, instance_id, lock=True)
            values: dict[str, Any] = {"resolved_by_user_id": actor.user_id}
            if action == "done":
                values.update(status="completed", output=output, finished_at=func.now())
            elif action == "retry":
                values.update(status="pending", next_attempt_at=func.now(), error=None)
            else:
                values.update(
                    status="failed",
                    error="the owner marked the step as failed",
                    finished_at=func.now(),
                )
            done = (
                await connection.execute(
                    update(WorkflowStep)
                    .where(
                        WorkflowStep.org_id == actor.org_id,
                        WorkflowStep.instance_id == instance_id,
                        WorkflowStep.step_id == step_id,
                        WorkflowStep.kind == "tool",
                        WorkflowStep.status.in_(("waiting_human", "needs_reconcile")),
                    )
                    .values(**values)
                    .returning(WorkflowStep.id)
                )
            ).scalar_one_or_none()
            if done is None:
                raise WorkflowError(f"step {step_id} does not wait for a person")
            await touch_instance(connection, actor.org_id, instance_id)
            await self._audit(
                connection,
                actor,
                f"workflow.step_resolved.{action}",
                row.workflow_id,
                row.workspace_id,
                details={"instance_id": str(instance_id), "step_id": step_id},
            )
        return await self.instance(actor, instance_id)

    # -- helpers ----------------------------------------------------------------------------

    async def agent_actor(
        self,
        *,
        org_id: uuid.UUID,
        workspace_id: uuid.UUID,
        session_id: uuid.UUID,
        run_id: uuid.UUID,
        user_id: uuid.UUID | None,
    ) -> Actor:
        """The actor of a tool call. The workflow belongs to the user of the run."""
        async with self._engine.connect() as connection:
            row = (
                await connection.execute(
                    select(Run.triggered_by_user_id, Run.agent_profile_id).where(
                        Run.org_id == org_id, Run.id == run_id, Run.session_id == session_id
                    )
                )
            ).one_or_none()
        if row is None:
            raise WorkflowError("the run does not exist")
        owner = user_id or row.triggered_by_user_id
        if owner is None:
            raise WorkflowError("a workflow needs a person, and this run has none")
        return Actor(
            org_id=org_id,
            user_id=owner,
            kind="agent",
            run_id=run_id,
            session_id=session_id,
            workspace_id=workspace_id,
            agent_profile_id=row.agent_profile_id,
        )

    async def in_a_workflow_run(self, org_id: uuid.UUID, run_id: uuid.UUID) -> bool:
        async with self._engine.connect() as connection:
            return await workflow_of_run(connection, org_id, run_id) is not None

    async def _guard(self, actor: Actor) -> None:
        """The recursion guard: an agent step cannot create, change, or start workflows."""
        if actor.run_id is not None and await self.in_a_workflow_run(actor.org_id, actor.run_id):
            raise WorkflowError("an agent step of a workflow cannot change or start workflows")

    async def _owned(
        self,
        connection: AsyncConnection,
        actor: Actor,
        workflow_id: uuid.UUID,
        *,
        lock: bool = False,
    ) -> Any:
        query = select(Workflow).where(
            Workflow.org_id == actor.org_id,
            Workflow.id == workflow_id,
            Workflow.owner_user_id == actor.user_id,
            Workflow.status != "removed",
        )
        if lock:
            query = query.with_for_update()
        row = (await connection.execute(query)).one_or_none()
        if row is None:
            raise WorkflowNotFoundError("the workflow does not exist")
        return row

    async def _instance(
        self,
        connection: AsyncConnection,
        actor: Actor,
        instance_id: uuid.UUID,
        *,
        lock: bool = False,
    ) -> Any:
        query = select(WorkflowInstance).where(
            WorkflowInstance.org_id == actor.org_id,
            WorkflowInstance.id == instance_id,
            WorkflowInstance.owner_user_id == actor.user_id,
        )
        if lock:
            query = query.with_for_update()
        row = (await connection.execute(query)).one_or_none()
        if row is None:
            raise WorkflowNotFoundError("the instance does not exist")
        return row

    async def _name_free(
        self,
        connection: AsyncConnection,
        actor: Actor,
        name: str,
        workflow_id: uuid.UUID | None,
    ) -> None:
        query = select(Workflow.id).where(
            Workflow.org_id == actor.org_id,
            Workflow.owner_user_id == actor.user_id,
            Workflow.name == name,
            Workflow.status != "removed",
        )
        if workflow_id is not None:
            query = query.where(Workflow.id != workflow_id)
        if (await connection.execute(query)).first() is not None:
            raise WorkflowError(f"you have a workflow with the name {name}")

    async def _place(
        self,
        connection: AsyncConnection,
        actor: Actor,
        workspace_id: uuid.UUID | None,
        profile_id: uuid.UUID | None,
    ) -> tuple[uuid.UUID, uuid.UUID]:
        """The workspace and the agent profile of a new workflow. The owner must see the
        workspace, and the profile must be in it."""
        workspace = workspace_id or actor.workspace_id
        if workspace is None:
            workspace = (
                await connection.execute(
                    select(Workspace.id)
                    .where(Workspace.org_id == actor.org_id)
                    .order_by(Workspace.created_at)
                    .limit(1)
                )
            ).scalar_one_or_none()
        # An org admin sees every workspace, so the workspace must exist in the org first.
        found_workspace = (
            None
            if workspace is None
            else (
                await connection.execute(
                    select(Workspace.id).where(
                        Workspace.org_id == actor.org_id, Workspace.id == workspace
                    )
                )
            ).scalar_one_or_none()
        )
        grants = await load_grants(connection, actor.org_id, actor.user_id)
        if found_workspace is None or not grants.sees_workspace(found_workspace):
            raise WorkflowError("you cannot use that workspace")
        profile = profile_id or actor.agent_profile_id
        if profile is None:
            profile = (
                await connection.execute(
                    select(AgentProfile.id)
                    .where(
                        AgentProfile.org_id == actor.org_id,
                        (AgentProfile.workspace_id == workspace)
                        | AgentProfile.workspace_id.is_(None),
                    )
                    .order_by(AgentProfile.created_at)
                    .limit(1)
                )
            ).scalar_one_or_none()
        found = (
            await connection.execute(
                select(AgentProfile.workspace_id).where(
                    AgentProfile.org_id == actor.org_id, AgentProfile.id == profile
                )
            )
        ).one_or_none()
        if profile is None or found is None or found[0] not in (None, workspace):
            raise WorkflowError("the agent profile does not exist in this workspace")
        return uuid.UUID(str(workspace)), uuid.UUID(str(profile))

    @staticmethod
    def _state(
        definition: Mapping[str, Any], needs: bool, *, last: datetime | None
    ) -> dict[str, Any]:
        """The status and the next fire of a version that can be active."""
        if needs:
            return {"status": "pending_approval", "next_fire_at": None}
        return {
            "status": "active",
            "next_fire_at": first_fire(definition, datetime.now(UTC), last=last),
        }

    async def _insert_version(
        self,
        connection: AsyncConnection,
        actor: Actor,
        workflow_id: uuid.UUID,
        version: int,
        source: str,
        definition: dict[str, Any],
        needs: bool,
    ) -> None:
        await connection.execute(
            insert(WorkflowVersion).values(
                org_id=actor.org_id,
                workflow_id=workflow_id,
                version=version,
                source=source,
                definition=definition,
                hash=definition_hash(definition),
                needs_approval=needs,
                created_by_kind=actor.kind,
                created_by_run_id=actor.run_id,
            )
        )

    async def _audit(
        self,
        connection: AsyncConnection,
        actor: Actor,
        action: str,
        workflow_id: uuid.UUID,
        workspace_id: uuid.UUID,
        version: int | None = None,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        await connection.execute(
            insert(AuditEntry).values(
                org_id=actor.org_id,
                workspace_id=workspace_id,
                actor_kind=actor.kind,
                actor_user_id=actor.user_id,
                action=action,
                target_type="workflow",
                target_id=str(workflow_id),
                details={
                    "version": version,
                    "run_id": str(actor.run_id) if actor.run_id else None,
                    **(details or {}),
                },
            )
        )


EVENT_INPUT_MAX_BYTES: Final = 9_000
"""The largest ``event`` input from a trigger. The event is compact JSON text, and its escape in
the inputs is at most two times as long, so the inputs stay under ``MAX_INPUTS_BYTES``."""
EVENT_CUT: Final = "\n[the event is longer; the rest is not shown]"


def _clip_event(event: str) -> str:
    data = event.encode("utf-8")
    if len(data) <= EVENT_INPUT_MAX_BYTES:
        return event
    return data[:EVENT_INPUT_MAX_BYTES].decode("utf-8", errors="ignore") + EVENT_CUT


__all__ = ["MAX_WORKFLOWS_PER_USER", "Actor", "Resolution", "WorkflowService"]
