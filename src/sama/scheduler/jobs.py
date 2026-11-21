"""The job service: create, change, list, approve, and run jobs (docs/architecture.md, Section
12.1).

The tool ``cronjob_manage``, the ``/cron`` commands, and the web API use this one service, so
they edit the same rows with the same rules:

- A job belongs to its owner. Only the owner sees it, changes it, and approves it.
- A person who writes a job approves that version at once. A job that the agent writes and that
  needs an approval (``needs_approval``) waits in ``pending_approval`` and does not fire.
- A job run cannot create or change jobs (the recursion guard). The gate also removes the
  ``cron`` toolset from a job run.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Final, Literal

from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from sama.db.models import (
    ENDED_RUN_STATUSES,
    AgentProfile,
    AuditEntry,
    Delivery,
    Job,
    JobFire,
    Run,
    Workspace,
)
from sama.orchestrator.queue import QueueError, RunQueue
from sama.scheduler import schedule as sched
from sama.scheduler.fire import FireBlockedError, fire_running, owner_email, start_fire
from sama.scheduler.spec import (
    JobError,
    JobInput,
    JobNotFoundError,
    JobSpec,
    approved,
    cleared,
    external_targets,
    needs_approval,
    parse_model,
    parse_preapproval,
    parse_skills,
    parse_target,
    spec_hash,
)
from sama.tenancy.access import load_grants
from sama.vault.redact import redact

MAX_JOBS_PER_USER: Final = 50
CURATOR_FIXED: Final = (
    "mode",
    "prompt",
    "script",
    "skills",
    "model",
    "preapproved_tools",
    "attach_to_session",
)
"""The fields that a curator job does not use, so a person cannot change them."""


def _stored(spec: JobSpec, name: str) -> Any:
    """The value of a field of ``spec`` as a ``JobInput`` gives it."""
    value = getattr(spec, name)
    return list(value) if isinstance(value, tuple) else value


RECENT_FIRES: Final = 10

ActorKind = Literal["user", "agent"]


@dataclass(frozen=True, slots=True)
class Actor:
    """Who calls the service. ``kind="agent"`` is a run of the agent: ``run_id`` is the run
    and ``session_id`` is the session that the target ``origin`` points to."""

    org_id: uuid.UUID
    user_id: uuid.UUID
    kind: ActorKind = "user"
    run_id: uuid.UUID | None = None
    session_id: uuid.UUID | None = None
    workspace_id: uuid.UUID | None = None
    agent_profile_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class JobView:
    id: uuid.UUID
    name: str
    status: str
    version: int
    approved_version: int | None
    needs_approval: bool
    approval_reasons: list[str]
    schedule: dict[str, Any]
    schedule_text: str
    next_fire_at: datetime | None
    next_fires: list[datetime]
    last_fire_at: datetime | None
    prompt: str
    skills: list[str]
    model: str | None
    mode: str
    script: str | None
    deliver: str
    failure_deliver: str | None
    preapproved_tools: list[dict[str, Any]]
    attach_to_session: bool
    workspace_id: uuid.UUID
    agent_profile_id: uuid.UUID
    consecutive_failures: int
    paused_reason: str | None
    created_by_kind: str
    created_at: datetime
    updated_at: datetime
    fires: list[dict[str, Any]] = field(default_factory=list)

    def as_json(self) -> dict[str, Any]:
        def iso(value: datetime | None) -> str | None:
            return value.isoformat() if value is not None else None

        return {
            "id": str(self.id),
            "name": self.name,
            "status": self.status,
            "version": self.version,
            "approved_version": self.approved_version,
            "needs_approval": self.needs_approval,
            "approval_reasons": self.approval_reasons,
            "schedule": self.schedule,
            "schedule_text": self.schedule_text,
            "next_fire_at": iso(self.next_fire_at),
            "next_fires": [iso(when) for when in self.next_fires],
            "last_fire_at": iso(self.last_fire_at),
            "prompt": self.prompt,
            "skills": self.skills,
            "model": self.model,
            "mode": self.mode,
            "script": self.script,
            "deliver": self.deliver,
            "failure_deliver": self.failure_deliver,
            "preapproved_tools": self.preapproved_tools,
            "attach_to_session": self.attach_to_session,
            "workspace_id": str(self.workspace_id),
            "agent_profile_id": str(self.agent_profile_id),
            "consecutive_failures": self.consecutive_failures,
            "paused_reason": self.paused_reason,
            "created_by_kind": self.created_by_kind,
            "created_at": iso(self.created_at),
            "updated_at": iso(self.updated_at),
            "fires": self.fires,
        }


def preview(text: str | dict[str, Any], tz: str | None = None) -> dict[str, Any]:
    """The checked schedule and its next 3 fire times. Raises ``JobError``."""
    now = datetime.now(UTC)
    try:
        if isinstance(text, str):
            schedule = sched.parse(text, tz=tz or sched.DEFAULT_TZ, now=now)
        else:
            schedule = sched.validate({**text, **({"tz": tz} if tz else {})}, now=now)
    except sched.ScheduleError as error:
        raise JobError(str(error)) from None
    return {
        "schedule": schedule,
        "text": sched.describe(schedule),
        "next_fires": [when.isoformat() for when in sched.next_fires(schedule, now)],
    }


def _reasons(spec: JobSpec, email: str | None) -> list[str]:
    reasons = []
    if spec.preapproved_tools:
        names = ", ".join(item["tool"] for item in spec.preapproved_tools)
        reasons.append(f"it pre-approves tools: {names}")
    if spec.mode == "script":
        reasons.append("it runs a script in the sandbox")
    for target in external_targets(spec, email):
        reasons.append(f"it sends to {target}")
    return reasons


class JobService:
    def __init__(self, engine: AsyncEngine, queue: RunQueue) -> None:
        self._engine = engine
        self._queue = queue

    # -- reads ------------------------------------------------------------------------------

    async def list(self, actor: Actor) -> list[JobView]:
        async with self._engine.connect() as connection:
            rows = (
                await connection.execute(
                    select(Job)
                    .where(
                        Job.org_id == actor.org_id,
                        Job.owner_user_id == actor.user_id,
                        Job.status != "removed",
                    )
                    .order_by(Job.created_at)
                )
            ).all()
            email = await owner_email(connection, actor.org_id, actor.user_id)
        return [self._view(row, email) for row in rows]

    async def get(self, actor: Actor, job_id: uuid.UUID) -> JobView:
        async with self._engine.connect() as connection:
            row = await self._owned(connection, actor, job_id)
            email = await owner_email(connection, actor.org_id, actor.user_id)
            fires = (
                await connection.execute(
                    select(
                        JobFire.id,
                        JobFire.status,
                        JobFire.scheduled_for,
                        JobFire.manual,
                        JobFire.session_id,
                        JobFire.output,
                        JobFire.error,
                        JobFire.finished_at,
                    )
                    .where(JobFire.org_id == actor.org_id, JobFire.job_id == job_id)
                    .order_by(JobFire.created_at.desc())
                    .limit(RECENT_FIRES)
                )
            ).all()
            receipts = (
                await connection.execute(
                    select(
                        Delivery.fire_id,
                        Delivery.kind,
                        Delivery.target,
                        Delivery.status,
                        Delivery.attempts,
                        Delivery.error,
                        Delivery.sent_at,
                    ).where(
                        Delivery.org_id == actor.org_id,
                        Delivery.fire_id.in_([fire.id for fire in fires]),
                    )
                )
            ).all()
        view = self._view(row, email)
        by_fire: dict[uuid.UUID, list[dict[str, Any]]] = {}
        for receipt in receipts:
            by_fire.setdefault(receipt.fire_id, []).append(
                {
                    "kind": receipt.kind,
                    "target": receipt.target,
                    "status": receipt.status,
                    "attempts": receipt.attempts,
                    "error": receipt.error,
                    "sent_at": receipt.sent_at.isoformat() if receipt.sent_at else None,
                }
            )
        view.fires.extend(
            {
                "id": str(fire.id),
                "status": fire.status,
                "scheduled_for": fire.scheduled_for.isoformat(),
                "manual": fire.manual,
                "session_id": str(fire.session_id) if fire.session_id else None,
                "output": (fire.output or "")[:2000] or None,
                "error": fire.error,
                "finished_at": fire.finished_at.isoformat() if fire.finished_at else None,
                "deliveries": by_fire.get(fire.id, []),
            }
            for fire in fires
        )
        return view

    def _view(self, row: Any, email: str | None) -> JobView:
        spec = JobSpec.of(row)
        now = datetime.now(UTC)
        upcoming: list[datetime] = []
        if row.status == "active" and row.next_fire_at is not None:
            upcoming = [row.next_fire_at, *sched.next_fires(row.schedule, row.next_fire_at, 2)]
        return JobView(
            id=row.id,
            name=row.name,
            status=row.status,
            version=row.version,
            approved_version=row.approved_version,
            needs_approval=needs_approval(spec, email) and not approved(row),
            approval_reasons=_reasons(spec, email),
            schedule=dict(row.schedule),
            schedule_text=sched.describe(row.schedule),
            next_fire_at=row.next_fire_at,
            next_fires=[when for when in upcoming if when >= now][:3] or upcoming[:3],
            last_fire_at=row.last_fire_at,
            prompt=row.prompt,
            skills=list(row.skills),
            model=row.model,
            mode=row.mode,
            script=row.script,
            deliver=row.deliver,
            failure_deliver=row.failure_deliver,
            preapproved_tools=list(row.preapproved_tools),
            attach_to_session=row.attach_to_session,
            workspace_id=row.workspace_id,
            agent_profile_id=row.agent_profile_id,
            consecutive_failures=row.consecutive_failures,
            paused_reason=row.paused_reason,
            created_by_kind=row.created_by_kind,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    # -- writes -----------------------------------------------------------------------------

    async def create(self, actor: Actor, given: JobInput) -> JobView:
        await self._not_in_a_job_run(actor)
        if not given.name or not given.name.strip():
            raise JobError("a job needs a name")
        if given.schedule is None:
            raise JobError("a job needs a schedule")
        async with self._engine.begin() as connection:
            count = (
                await connection.execute(
                    select(func.count()).where(
                        Job.org_id == actor.org_id,
                        Job.owner_user_id == actor.user_id,
                        Job.status != "removed",
                        # The curator jobs are from sama, so they do not count (Section 9.7).
                        Job.mode != "curator",
                    )
                )
            ).scalar_one()
            if count >= MAX_JOBS_PER_USER:
                raise JobError(f"you have {MAX_JOBS_PER_USER} jobs. Remove one first.")
            workspace_id, profile_id = await self._place(connection, actor, given, None)
            spec = self._spec(given, None, profile_id)
            email = await owner_email(connection, actor.org_id, actor.user_id)
            job_id = uuid.uuid4()
            values = {
                "id": job_id,
                "org_id": actor.org_id,
                "workspace_id": workspace_id,
                "owner_user_id": actor.user_id,
                "name": given.name.strip(),
                **spec.values(),
                "origin_session_id": actor.session_id,
                "created_by_kind": actor.kind,
                "created_by_run_id": actor.run_id,
                **self._state(actor, spec, email, version=1),
            }
            await connection.execute(insert(Job).values(**values))
            await self._audit(connection, actor, "job.created", job_id, workspace_id, version=1)
            row = await self._owned(connection, actor, job_id)
            return self._view(row, email)

    async def update(self, actor: Actor, job_id: uuid.UUID, given: JobInput) -> JobView:
        await self._not_in_a_job_run(actor)
        async with self._engine.begin() as connection:
            row = await self._owned(connection, actor, job_id, lock=True)
            _, profile_id = await self._place(connection, actor, given, row)
            spec = self._spec(given, row, profile_id)
            email = await owner_email(connection, actor.org_id, actor.user_id)
            values: dict[str, Any] = {"updated_at": func.now()}
            if given.name is not None:
                if not given.name.strip():
                    raise JobError("a job needs a name")
                values["name"] = given.name.strip()
            version = row.version
            # A person approves their own change only when the version before it can fire.
            # A version from an agent that waits for an approval needs the explicit approval
            # of that version: a change that did not look at it does not approve it.
            prior_ok = cleared(row, email)
            if spec != JobSpec.of(row):
                version += 1
                values.update(spec.values(), version=version)
                if row.status in ("active", "pending_approval"):
                    values.update(
                        self._state(actor, spec, email, version=version, row=row, prior_ok=prior_ok)
                    )
                elif actor.kind == "user" and prior_ok:
                    values.update(self._approval(actor, spec, version))
            await connection.execute(update(Job).where(Job.id == job_id).values(**values))
            await self._audit(connection, actor, "job.updated", job_id, row.workspace_id, version)
            return self._view(await self._owned(connection, actor, job_id), email)

    async def approve(self, actor: Actor, job_id: uuid.UUID, version: int | None = None) -> JobView:
        """A human approves the current version of the job. ``version`` is the version that
        the person saw. An approval of an old version fails, so a change after the person
        looked needs a new look."""
        if actor.kind != "user":
            raise JobError("only a person can approve a job")
        async with self._engine.begin() as connection:
            row = await self._owned(connection, actor, job_id, lock=True)
            if version is not None and version != row.version:
                raise JobError(
                    f"the job changed: it is version {row.version} now, not {version}. "
                    "Look at it again, then approve it."
                )
            spec = JobSpec.of(row)
            values: dict[str, Any] = {**self._approval(actor, spec, row.version)}
            if row.status == "pending_approval":
                values.update(self._schedule_state(row.schedule, row.last_fire_at))
            await connection.execute(update(Job).where(Job.id == job_id).values(**values))
            await self._audit(
                connection, actor, "job.approved", job_id, row.workspace_id, row.version
            )
            email = await owner_email(connection, actor.org_id, actor.user_id)
            return self._view(await self._owned(connection, actor, job_id), email)

    async def pause(self, actor: Actor, job_id: uuid.UUID) -> JobView:
        view = await self._set_status(actor, job_id, "pause")
        if view is None:
            raise JobNotFoundError("the job does not exist")
        return view

    async def resume(self, actor: Actor, job_id: uuid.UUID) -> JobView:
        """Resumes the job. A version that waits for an approval becomes
        ``pending_approval``: a resume does not approve it."""
        view = await self._set_status(actor, job_id, "resume")
        if view is None:
            raise JobNotFoundError("the job does not exist")
        return view

    async def remove(self, actor: Actor, job_id: uuid.UUID) -> None:
        await self._set_status(actor, job_id, "remove")

    async def _set_status(self, actor: Actor, job_id: uuid.UUID, action: str) -> JobView | None:
        """Pauses, resumes, or removes the job. Returns the new view, or None for a removed
        job."""
        await self._not_in_a_job_run(actor)
        async with self._engine.begin() as connection:
            row = await self._owned(connection, actor, job_id, lock=True)
            email = await owner_email(connection, actor.org_id, actor.user_id)
            values: dict[str, Any] = {"updated_at": func.now()}
            if action == "pause":
                values.update(status="paused", next_fire_at=None, paused_reason="paused by you")
            elif action == "remove":
                values.update(status="removed", next_fire_at=None)
            else:
                values.update(consecutive_failures=0, alerted_at=None, paused_reason=None)
                if cleared(row, email):
                    values.update(self._schedule_state(row.schedule, row.last_fire_at))
                else:
                    values.update(status="pending_approval", next_fire_at=None)
            await connection.execute(update(Job).where(Job.id == job_id).values(**values))
            await self._audit(connection, actor, f"job.{action}", job_id, row.workspace_id)
            if action == "remove":
                return None
            return self._view(await self._owned(connection, actor, job_id), email)

    async def run_now(self, actor: Actor, job_id: uuid.UUID) -> uuid.UUID:
        """Starts a fire now, outside the schedule. Returns the fire id."""
        await self._not_in_a_job_run(actor)
        async with self._engine.begin() as connection:
            row = await self._owned(connection, actor, job_id, lock=True)
            if await fire_running(connection, actor.org_id, job_id):
                raise JobError("the job is running now. Wait for it, or cancel it.")
            try:
                fire_id = await start_fire(
                    connection, self._queue, row, scheduled_for=datetime.now(UTC), manual=True
                )
            except (FireBlockedError, QueueError) as error:
                raise JobError(str(error)) from None
            if fire_id is None:
                raise JobError("the job fired at this moment. Try again.")
            return fire_id

    async def cancel(self, actor: Actor, job_id: uuid.UUID) -> int:
        """Cancels the open runs of the job. Returns how many runs it cancelled."""
        await self._not_in_a_job_run(actor)
        async with self._engine.connect() as connection:
            await self._owned(connection, actor, job_id)
            runs = (
                (
                    await connection.execute(
                        select(Run.id)
                        .select_from(JobFire)
                        .join(Run, (Run.org_id == JobFire.org_id) & (Run.id == JobFire.run_id))
                        .where(
                            JobFire.org_id == actor.org_id,
                            JobFire.job_id == job_id,
                            JobFire.status == "running",
                            Run.status.not_in(ENDED_RUN_STATUSES),
                        )
                    )
                )
                .scalars()
                .all()
            )
        for run_id in runs:
            await self._queue.cancel(org_id=actor.org_id, run_id=run_id)
        return len(runs)

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
        """The actor of a tool call. The job belongs to the user of the run: the private
        session owner, or the person who sent the message in a shared session."""
        async with self._engine.connect() as connection:
            row = (
                await connection.execute(
                    select(Run.triggered_by_user_id, Run.agent_profile_id).where(
                        Run.org_id == org_id, Run.id == run_id, Run.session_id == session_id
                    )
                )
            ).one_or_none()
        if row is None:
            raise JobError("the run does not exist")
        owner = user_id or row.triggered_by_user_id
        if owner is None:
            raise JobError("a job needs a person, and this run has none")
        return Actor(
            org_id=org_id,
            user_id=owner,
            kind="agent",
            run_id=run_id,
            session_id=session_id,
            workspace_id=workspace_id,
            agent_profile_id=row.agent_profile_id,
        )

    async def in_a_job_run(self, org_id: uuid.UUID, run_id: uuid.UUID) -> bool:
        async with self._engine.connect() as connection:
            found = (
                await connection.execute(
                    select(JobFire.id).where(JobFire.org_id == org_id, JobFire.run_id == run_id)
                )
            ).first()
        return found is not None

    async def _not_in_a_job_run(self, actor: Actor) -> None:
        """The recursion guard: a job run cannot create or change jobs."""
        if actor.run_id is not None and await self.in_a_job_run(actor.org_id, actor.run_id):
            raise JobError("a job run cannot create or change jobs")

    async def _owned(
        self, connection: AsyncConnection, actor: Actor, job_id: uuid.UUID, *, lock: bool = False
    ) -> Any:
        query = select(Job).where(
            Job.org_id == actor.org_id,
            Job.id == job_id,
            Job.owner_user_id == actor.user_id,
            Job.status != "removed",
        )
        if lock:
            query = query.with_for_update()
        row = (await connection.execute(query)).one_or_none()
        if row is None:
            raise JobNotFoundError("the job does not exist")
        return row

    async def _place(
        self, connection: AsyncConnection, actor: Actor, given: JobInput, row: Any
    ) -> tuple[uuid.UUID, uuid.UUID]:
        """The workspace and the agent profile of the job. The owner must see the workspace,
        and the profile must be in it."""
        if row is not None and given.workspace_id not in (None, row.workspace_id):
            raise JobError("the workspace of a job does not change")
        workspace_id = (
            row.workspace_id if row is not None else (given.workspace_id or actor.workspace_id)
        )
        if workspace_id is None:
            workspace_id = (
                await connection.execute(
                    select(Workspace.id)
                    .where(Workspace.org_id == actor.org_id)
                    .order_by(Workspace.created_at)
                    .limit(1)
                )
            ).scalar_one_or_none()
        grants = await load_grants(connection, actor.org_id, actor.user_id)
        if workspace_id is None or not grants.sees_workspace(workspace_id):
            raise JobError("you cannot use that workspace")
        profile_id = given.agent_profile_id or (
            row.agent_profile_id if row is not None else actor.agent_profile_id
        )
        if profile_id is None:
            profile_id = (
                await connection.execute(
                    select(AgentProfile.id)
                    .where(
                        AgentProfile.org_id == actor.org_id,
                        (AgentProfile.workspace_id == workspace_id)
                        | AgentProfile.workspace_id.is_(None),
                    )
                    .order_by(AgentProfile.created_at)
                    .limit(1)
                )
            ).scalar_one_or_none()
        found = (
            await connection.execute(
                select(AgentProfile.workspace_id).where(
                    AgentProfile.org_id == actor.org_id, AgentProfile.id == profile_id
                )
            )
        ).one_or_none()
        if profile_id is None or found is None or found[0] not in (None, workspace_id):
            raise JobError("the agent profile does not exist in this workspace")
        return workspace_id, profile_id

    def _spec(self, given: JobInput, row: Any, profile_id: uuid.UUID) -> JobSpec:
        old = JobSpec.of(row) if row is not None else None
        sent = given.model_fields_set

        def pick(name: str, default: Any) -> Any:
            if name in sent:
                return getattr(given, name)
            return getattr(old, name) if old is not None else default

        schedule = old.schedule if old is not None else None
        if given.schedule is not None:
            schedule = preview(given.schedule, given.tz)["schedule"]
        elif given.tz is not None and old is not None:
            schedule = preview(old.schedule, given.tz)["schedule"]
        if schedule is None:
            raise JobError("a job needs a schedule")
        if old is not None and old.mode == "curator":
            return self._curator_spec(given, old, profile_id, schedule)
        mode = pick("mode", "agent") or "agent"
        prompt = (pick("prompt", "") or "").strip()
        script = pick("script", None)
        if mode == "agent":
            script = None
            if not prompt:
                raise JobError("an agent job needs a prompt")
        elif not script or not script.strip():
            raise JobError("a script job needs a script")
        for label, text in (("prompt", prompt), ("script", script)):
            if text and redact(text) != text:
                raise JobError(
                    f"the {label} contains a secret. Keep secrets in the vault, and use a "
                    "connection or a vault reference."
                )
        deliver = parse_target(pick("deliver", "origin") or "origin")
        failure = pick("failure_deliver", None)
        failure = parse_target(failure, field="failure_deliver") if failure else None
        if "preapproved_tools" in sent:
            tools = tuple(parse_preapproval(item) for item in given.preapproved_tools or [])
        else:
            tools = old.preapproved_tools if old is not None else ()
        if mode == "script" and tools:
            raise JobError("a script job has no tools to pre-approve")
        return JobSpec(
            agent_profile_id=profile_id,
            schedule=schedule,
            prompt=prompt,
            skills=parse_skills(pick("skills", ()) or ()),
            model=parse_model(pick("model", None)),
            mode=mode,
            script=script,
            deliver=deliver,
            failure_deliver=failure,
            preapproved_tools=tools,
            attach_to_session=bool(pick("attach_to_session", False)),
        )

    @staticmethod
    def _curator_spec(
        given: JobInput, old: JobSpec, profile_id: uuid.UUID, schedule: dict[str, Any]
    ) -> JobSpec:
        """A curator job runs the curator, not a prompt or a script (Section 9.7). A person
        changes its name, schedule, profile, and targets only."""
        fixed = [
            name
            for name in CURATOR_FIXED
            if name in given.model_fields_set and getattr(given, name) != _stored(old, name)
        ]
        if fixed:
            raise JobError(
                f"a curator job has a fixed {', '.join(fixed)}. You can change its name, "
                "schedule, and delivery targets."
            )
        sent = given.model_fields_set
        deliver = parse_target(given.deliver or "origin") if "deliver" in sent else old.deliver
        failure = given.failure_deliver if "failure_deliver" in sent else old.failure_deliver
        return replace(
            old,
            agent_profile_id=profile_id,
            schedule=schedule,
            deliver=deliver,
            failure_deliver=parse_target(failure, field="failure_deliver") if failure else None,
        )

    def _approval(self, actor: Actor, spec: JobSpec, version: int) -> dict[str, Any]:
        return {
            "approved_version": version,
            "approved_hash": spec_hash(spec),
            "approved_by_user_id": actor.user_id,
            "approved_at": func.now(),
        }

    @staticmethod
    def _schedule_state(schedule: dict[str, Any], last: datetime | None) -> dict[str, Any]:
        when = sched.next_fire(schedule, datetime.now(UTC), last=last)
        if when is None:
            raise JobError("the schedule has no next fire. Change the schedule first.")
        return {"status": "active", "next_fire_at": when, "paused_reason": None}

    def _state(
        self,
        actor: Actor,
        spec: JobSpec,
        email: str | None,
        *,
        version: int,
        row: Any = None,
        prior_ok: bool = True,
    ) -> dict[str, Any]:
        """The status and the approval of a new version. A person approves their own version
        when the version before it can fire (``prior_ok``). Another version that needs an
        approval waits for one."""
        values: dict[str, Any] = {}
        if actor.kind == "user" and prior_ok:
            values.update(self._approval(actor, spec, version))
        elif needs_approval(spec, email):
            return {"status": "pending_approval", "next_fire_at": None}
        last = row.last_fire_at if row is not None else None
        values.update(self._schedule_state(spec.schedule, last))
        return values

    async def _audit(
        self,
        connection: AsyncConnection,
        actor: Actor,
        action: str,
        job_id: uuid.UUID,
        workspace_id: uuid.UUID,
        version: int | None = None,
    ) -> None:
        await connection.execute(
            insert(AuditEntry).values(
                org_id=actor.org_id,
                workspace_id=workspace_id,
                actor_kind=actor.kind,
                actor_user_id=actor.user_id,
                action=action,
                target_type="job",
                target_id=str(job_id),
                details={
                    "version": version,
                    "run_id": str(actor.run_id) if actor.run_id else None,
                },
            )
        )


__all__ = [
    "Actor",
    "JobError",
    "JobInput",
    "JobNotFoundError",
    "JobService",
    "JobView",
    "preview",
]
