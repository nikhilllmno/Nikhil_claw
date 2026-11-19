"""The run queue in Postgres (docs/architecture.md, Section 4.3, decision D8).

Each run is one ``runs`` row. A worker claims the oldest ``queued`` run with
``FOR UPDATE SKIP LOCKED`` and holds a lease. It extends the lease with ``heartbeat`` and
ends the run with ``complete``, ``fail``, or ``pause``. Only the lease owner
can end a running run.

Rules:

- One active run per session. ``running`` and ``paused`` runs are active. A partial unique
  index enforces the rule, and ``claim`` takes a run only when its session has no active run.
- The runs of one session start in the order of ``created_at`` (FIFO). New messages queue.
- A paused run waits for its approval. When the approval is decided or times out,
  ``release_decided`` queues the run again with the decision, and the next claim resumes it.
- ``reap`` handles runs whose lease expired, for example after a worker crash. A run that
  the worker did not give to its runtime (``dispatch``) goes back to the queue, up to
  ``max_attempts`` claims. Any other run is marked ``failed``: a tool can have run, so a
  second try is not safe.
- Each write of a worker checks the owner, the attempt, and the lease expiry, so an old
  attempt cannot write after a reap.

The worker is a system process, so ``claim``, ``reap``, and ``release_decided`` read all
orgs. Every other method is scoped by ``org_id``.
"""

from __future__ import annotations

import base64
import math
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from typing import Any, Final

from sqlalchemy import (
    and_,
    case,
    exists,
    func,
    insert,
    select,
    tuple_,
    update,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from sqlalchemy.orm import aliased

from sama.db.models import (
    AgentProfile,
    Approval,
    AuditEntry,
    Message,
    Org,
    Run,
    RunEvent,
    Session,
)
from sama.gate.decide import arguments_digest
from sama.gate.service import APPROVAL_TTL, stored_arguments
from sama.orchestrator.limits import QueueLimits
from sama.runtime.contract import ApprovalDecision, FailureCode, Usage

DEFAULT_LEASE: Final = timedelta(seconds=30)
DEFAULT_MAX_ATTEMPTS: Final = 3
REAP_BATCH: Final = 100
ACTIVE_STATUSES: Final = ("running", "paused")
EXPIRED_REASON: Final = "the approval timed out"
RATE_WINDOW: Final = timedelta(seconds=60)
CLAIM_TRIES: Final = 5
"""How many candidate runs one ``claim`` tries before it gives up for this call."""


class QueueError(Exception):
    """The queue cannot do the operation. The message is safe to show to the user."""


class RateLimitedError(QueueError):
    """The user or the org sent too many messages in the last 60 seconds."""

    def __init__(self, message: str, retry_after_s: int) -> None:
        super().__init__(message)
        self.retry_after_s = retry_after_s


def _lock(name: str, org_id: uuid.UUID) -> Any:
    """A transaction-level advisory lock for one org and one purpose."""
    return select(func.pg_advisory_xact_lock(func.hashtextextended(f"{name}:{org_id}", 0)))


class Lease(StrEnum):
    """The answer of ``heartbeat``."""

    HELD = "held"
    CANCEL = "cancel"
    """The lease is held, and a user asked to cancel the run."""
    LOST = "lost"
    """The worker does not own the run any more. It must stop the run and write nothing."""


@dataclass(frozen=True, slots=True)
class RunRef:
    org_id: uuid.UUID
    run_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class Resume:
    approval_id: str
    decision: ApprovalDecision
    reason: str | None


@dataclass(frozen=True, slots=True)
class ClaimedRun:
    """A run that a worker owns until it ends the run or loses the lease."""

    org_id: uuid.UUID
    workspace_id: uuid.UUID
    session_id: uuid.UUID
    run_id: uuid.UUID
    agent_profile_id: uuid.UUID
    user_id: uuid.UUID | None
    """The user who started the run, if any."""
    audience: str
    owner_user_id: uuid.UUID | None
    """The owner of a private session. ``None`` for a shared session."""
    runtime: str
    model: str
    soul: str
    budget: Mapping[str, Any]
    input: str
    runtime_state: bytes | None
    """For a new run, the state of the last completed run of the session with the same
    runtime. For a resume, the state from the pause."""
    resume: Resume | None
    attempt: int
    next_seq: int
    """The ``seq`` of the next run event."""


def _encode_state(state: bytes | None) -> str | None:
    return None if state is None else base64.b64encode(state).decode("ascii")


def _decode_state(text: str | None) -> bytes | None:
    return None if text is None else base64.b64decode(text)


_CANCELLED: Final = {
    "status": "cancelled",
    "failure_code": "cancelled",
    "failure_message": "cancelled",
}
CRASH_MESSAGE: Final = (
    "the worker stopped during the run; a tool can have run, so the run does not start again"
)


class RunQueue:
    """The run queue. One instance can serve many workers in one process."""

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        lease: timedelta = DEFAULT_LEASE,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        approval_ttl: timedelta = APPROVAL_TTL,
    ) -> None:
        if lease <= timedelta(0):
            raise ValueError("lease must be more than 0")
        if max_attempts < 1:
            raise ValueError("max_attempts must be 1 or more")
        self._engine = engine
        self._lease = lease
        self._max_attempts = max_attempts
        self._approval_ttl = approval_ttl

    @property
    def lease(self) -> timedelta:
        return self._lease

    @property
    def engine(self) -> AsyncEngine:
        return self._engine

    # -- producers --------------------------------------------------------------------------

    async def enqueue(
        self,
        *,
        org_id: uuid.UUID,
        session_id: uuid.UUID,
        text: str,
        user_id: uuid.UUID | None = None,
        unattended: bool = False,
    ) -> uuid.UUID:
        """Stores a user message and queues a run for it. Returns the run id.

        The run uses the budget of the session profile. It uses the runtime of the frozen
        session config, or the runtime of the profile before the first run builds the config
        (Section 6.1). If the session has an active run, the new run waits in the queue.

        Raises ``RateLimitedError`` when the user or the org is over its message rate
        (``QueueLimits``).
        """
        async with self._engine.begin() as connection:
            session = (
                await connection.execute(
                    select(
                        Session.status,
                        Session.agent_profile_id,
                        func.coalesce(
                            Session.run_config["runtime"].astext, AgentProfile.runtime
                        ).label("runtime"),
                        AgentProfile.budget,
                    )
                    .join(
                        AgentProfile,
                        and_(
                            AgentProfile.org_id == Session.org_id,
                            AgentProfile.id == Session.agent_profile_id,
                        ),
                    )
                    .where(Session.org_id == org_id, Session.id == session_id)
                )
            ).one_or_none()
            if session is None:
                raise QueueError("the session does not exist")
            if session.status != "active":
                raise QueueError("the session is not active")
            await self._check_rate(connection, org_id, user_id)
            run_id = uuid.uuid4()
            await connection.execute(
                insert(Run).values(
                    id=run_id,
                    org_id=org_id,
                    session_id=session_id,
                    agent_profile_id=session.agent_profile_id,
                    triggered_by_user_id=user_id,
                    runtime=session.runtime,
                    budget=session.budget,
                    unattended=unattended,
                )
            )
            await connection.execute(
                insert(Message).values(
                    org_id=org_id,
                    session_id=session_id,
                    run_id=run_id,
                    role="user",
                    author_user_id=user_id,
                    content={"text": text},
                )
            )
        return run_id

    async def _check_rate(
        self, connection: AsyncConnection, org_id: uuid.UUID, user_id: uuid.UUID | None
    ) -> None:
        """Counts the runs of the last 60 seconds. The lock puts the enqueues of one org in a
        sequence, so two messages at the same time cannot both take the last place."""
        await connection.execute(_lock("sama.queue.rate", org_id))
        limits = QueueLimits.from_json(
            (await connection.execute(select(Org.limits).where(Org.id == org_id))).scalar_one()
        )
        checks: list[tuple[Any, int, str]] = [
            (Run.org_id == org_id, limits.org_messages_per_minute, "the org")
        ]
        if user_id is not None:
            checks.append(
                (
                    and_(Run.org_id == org_id, Run.triggered_by_user_id == user_id),
                    limits.user_messages_per_minute,
                    "you",
                )
            )
        for scope, limit, who in checks:
            row = (
                await connection.execute(
                    select(
                        func.count(),
                        func.extract("epoch", func.min(Run.created_at) + RATE_WINDOW - func.now()),
                    ).where(scope, Run.created_at > func.now() - RATE_WINDOW)
                )
            ).one()
            if row[0] >= limit:
                retry = max(1, math.ceil(float(row[1] or 1)))
                raise RateLimitedError(
                    f"{who} sent {limit} messages in the last minute. Try again in {retry} s.",
                    retry,
                )

    async def cancel(self, *, org_id: uuid.UUID, run_id: uuid.UUID) -> str:
        """Cancels a run and returns its status after the call.

        A queued or paused run is ``cancelled`` at once. For a running run the call records
        the request, and the worker stops the run at its next heartbeat. An ended run does
        not change.
        """
        async with self._engine.begin() as connection:
            status = (
                await connection.execute(
                    select(Run.status)
                    .where(Run.org_id == org_id, Run.id == run_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if status is None:
                raise QueueError("the run does not exist")
            if status in ("queued", "paused"):
                await connection.execute(
                    update(Run)
                    .where(Run.id == run_id)
                    .values(**_CANCELLED, cancel_requested_at=func.now(), finished_at=func.now())
                )
                return "cancelled"
            if status == "running":
                await connection.execute(
                    update(Run)
                    .where(Run.id == run_id, Run.cancel_requested_at.is_(None))
                    .values(cancel_requested_at=func.now())
                )
            return status

    async def release_decided(
        self, *, org_id: uuid.UUID | None = None, run_id: uuid.UUID | None = None
    ) -> list[uuid.UUID]:
        """Queues the paused runs whose approval is decided or timed out. Returns their ids.

        An approved approval resumes with ``approve``. A rejected or expired one resumes with
        ``reject``. The API calls it for one run after ``GateService.decide_approval``. The
        worker calls it for all runs from time to time.
        """
        decision = case((Approval.status == "approved", "approve"), else_="reject")
        reason = func.coalesce(
            Approval.decision_reason,
            case((Approval.status == "expired", EXPIRED_REASON), else_=None),
        )
        conditions = [
            Run.status == "paused",
            Run.cancel_requested_at.is_(None),
            Approval.org_id == Run.org_id,
            Approval.run_id == Run.id,
            Approval.call_id == Run.pending_approval_id,
            Approval.status != "pending",
        ]
        if org_id is not None:
            conditions.append(Run.org_id == org_id)
        if run_id is not None:
            conditions.append(Run.id == run_id)
        async with self._engine.begin() as connection:
            rows = await connection.execute(
                update(Run)
                .where(*conditions)
                .values(status="queued", resume_decision=decision, resume_reason=reason)
                .returning(Run.id)
            )
            return list(rows.scalars())

    # -- workers ----------------------------------------------------------------------------

    async def claim(self, owner: str) -> ClaimedRun | None:
        """Claims the next run for ``owner``, or returns None if no run is ready.

        A run of an org that has ``max_running_runs`` running runs waits (``QueueLimits``). An
        advisory lock per org puts the claims of one org in a sequence, so two workers cannot
        both take the last place.
        """
        full = await self._full_orgs()
        for _ in range(CLAIM_TRIES):
            try:
                async with self._engine.begin() as connection:
                    row = (await connection.execute(self._candidate(full))).one_or_none()
                    if row is None:
                        return None
                    await connection.execute(_lock("sama.queue.running", row.org_id))
                    if await self._at_limit(connection, row.org_id):
                        full.add(row.org_id)
                        continue
                    await connection.execute(
                        update(Run)
                        .where(Run.id == row.id)
                        .values(
                            status="running",
                            lease_owner=owner,
                            lease_expires_at=func.now() + self._lease,
                            attempts=Run.attempts + 1,
                            dispatched_at=None,
                            started_at=func.coalesce(Run.started_at, func.now()),
                        )
                    )
                    return await self._claimed(connection, row.id)
            except IntegrityError:
                # Another worker started a run of the same session at the same time. The
                # partial unique index refused this claim. The run stays queued for a later
                # claim.
                return None
        return None

    @staticmethod
    def _candidate(full: set[uuid.UUID]) -> Any:
        """The oldest ready run: queued, its session has no active run and no older queued
        run, and its org is not in ``full``."""
        other = aliased(Run)
        older = aliased(Run)
        conditions = [
            Run.status == "queued",
            ~exists().where(other.session_id == Run.session_id, other.status.in_(ACTIVE_STATUSES)),
            ~exists().where(
                older.session_id == Run.session_id,
                older.status == "queued",
                tuple_(older.created_at, older.id) < tuple_(Run.created_at, Run.id),
            ),
        ]
        if full:
            conditions.append(Run.org_id.not_in(full))
        return (
            select(Run.id, Run.org_id)
            .where(*conditions)
            .order_by(Run.created_at, Run.id)
            .limit(1)
            .with_for_update(of=Run, skip_locked=True)
        )

    async def _full_orgs(self) -> set[uuid.UUID]:
        """The orgs that have ``max_running_runs`` running runs. Only orgs with a running run
        are read, so the query stays small."""
        async with self._engine.connect() as connection:
            rows = await connection.execute(
                select(Run.org_id, func.count().label("running"), Org.limits)
                .join(Org, Org.id == Run.org_id)
                .where(Run.status == "running")
                .group_by(Run.org_id, Org.limits)
            )
            return {
                row.org_id
                for row in rows
                if row.running >= QueueLimits.from_json(row.limits).max_running_runs
            }

    @staticmethod
    async def _at_limit(connection: AsyncConnection, org_id: uuid.UUID) -> bool:
        limits = (await connection.execute(select(Org.limits).where(Org.id == org_id))).scalar_one()
        running = (
            await connection.execute(
                select(func.count()).where(Run.org_id == org_id, Run.status == "running")
            )
        ).scalar_one()
        return bool(running >= QueueLimits.from_json(limits).max_running_runs)

    async def _claimed(self, connection: AsyncConnection, run_id: uuid.UUID) -> ClaimedRun:
        row = (
            await connection.execute(
                select(
                    Run.org_id,
                    Run.session_id,
                    Run.agent_profile_id,
                    Run.triggered_by_user_id,
                    Run.runtime,
                    Run.budget,
                    Run.runtime_state,
                    Run.pending_approval_id,
                    Run.resume_decision,
                    Run.resume_reason,
                    Run.attempts,
                    Session.workspace_id,
                    Session.audience,
                    Session.owner_user_id,
                    AgentProfile.model,
                    AgentProfile.soul,
                )
                .join(Session, and_(Session.org_id == Run.org_id, Session.id == Run.session_id))
                .join(
                    AgentProfile,
                    and_(
                        AgentProfile.org_id == Run.org_id, AgentProfile.id == Run.agent_profile_id
                    ),
                )
                .where(Run.id == run_id)
            )
        ).one()
        text = (
            await connection.execute(
                select(Message.content)
                .where(
                    Message.org_id == row.org_id,
                    Message.run_id == run_id,
                    Message.role == "user",
                )
                .order_by(Message.seq)
                .limit(1)
            )
        ).scalar_one_or_none()
        next_seq = (
            await connection.execute(
                select(func.coalesce(func.max(RunEvent.seq) + 1, 0)).where(
                    RunEvent.org_id == row.org_id, RunEvent.run_id == run_id
                )
            )
        ).scalar_one()
        if row.resume_decision is not None and row.pending_approval_id is not None:
            resume: Resume | None = Resume(
                row.pending_approval_id, ApprovalDecision(row.resume_decision), row.resume_reason
            )
            state = _decode_state(row.runtime_state)
        else:
            resume = None
            state = await self._session_state(connection, row.org_id, row.session_id, row.runtime)
        return ClaimedRun(
            org_id=row.org_id,
            workspace_id=row.workspace_id,
            session_id=row.session_id,
            run_id=run_id,
            agent_profile_id=row.agent_profile_id,
            user_id=row.triggered_by_user_id,
            audience=row.audience,
            owner_user_id=row.owner_user_id,
            runtime=row.runtime,
            model=row.model,
            soul=row.soul,
            budget=row.budget,
            input=str((text or {}).get("text", "")),
            runtime_state=state,
            resume=resume,
            attempt=row.attempts,
            next_seq=int(next_seq),
        )

    async def _session_state(
        self, connection: AsyncConnection, org_id: uuid.UUID, session_id: uuid.UUID, runtime: str
    ) -> bytes | None:
        """The runtime state at the end of the last completed run of the session."""
        state = (
            await connection.execute(
                select(Run.runtime_state)
                .where(
                    Run.org_id == org_id,
                    Run.session_id == session_id,
                    Run.runtime == runtime,
                    Run.status == "completed",
                    Run.runtime_state.is_not(None),
                )
                .order_by(Run.finished_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        return _decode_state(state)

    async def dispatch(
        self, run: ClaimedRun, owner: str, *, config_fingerprint: str | None = None
    ) -> bool:
        """Records that the worker gives the run to its runtime. Returns False if the lease is
        lost. After this mark, ``reap`` does not start the run again, because a tool can run.

        ``config_fingerprint`` is the fingerprint of the ``RunConfig`` that the run uses."""
        async with self._engine.begin() as connection:
            row = (
                await connection.execute(
                    update(Run)
                    .where(*self._owned(run, owner))
                    .values(dispatched_at=func.now(), config_fingerprint=config_fingerprint)
                    .returning(Run.id)
                )
            ).one_or_none()
        return row is not None

    async def heartbeat(self, run: ClaimedRun, owner: str) -> Lease:
        """Extends the lease. Tells the worker if a user asked to cancel the run."""
        async with self._engine.begin() as connection:
            cancel = (
                await connection.execute(
                    update(Run)
                    .where(*self._owned(run, owner))
                    .values(lease_expires_at=func.now() + self._lease)
                    .returning(Run.cancel_requested_at.is_not(None))
                )
            ).scalar_one_or_none()
        if cancel is None:
            return Lease.LOST
        return Lease.CANCEL if cancel else Lease.HELD

    async def add_events(
        self, run: ClaimedRun, owner: str, events: Sequence[tuple[int, str, dict[str, Any]]]
    ) -> bool:
        """Stores run events as ``(seq, type, payload)``. Returns False if the lease is lost."""
        if not events:
            return True
        async with self._engine.begin() as connection:
            held = (
                await connection.execute(
                    select(Run.id).where(*self._owned(run, owner)).with_for_update()
                )
            ).scalar_one_or_none()
            if held is None:
                return False
            await connection.execute(
                insert(RunEvent),
                [
                    {
                        "org_id": run.org_id,
                        "session_id": run.session_id,
                        "run_id": run.run_id,
                        "seq": seq,
                        "type": kind,
                        "payload": payload,
                    }
                    for seq, kind, payload in events
                ],
            )
        return True

    async def complete(
        self,
        run: ClaimedRun,
        owner: str,
        *,
        output: str,
        runtime_state: bytes | None,
        usage: Usage,
    ) -> bool:
        """Stores the reply as an assistant message and ends the run as ``completed``."""
        async with self._engine.begin() as connection:
            ended = await self._end(
                connection,
                run,
                owner,
                status="completed",
                usage=usage,
                runtime_state=_encode_state(runtime_state),
            )
            if ended:
                await connection.execute(
                    insert(Message).values(
                        org_id=run.org_id,
                        session_id=run.session_id,
                        run_id=run.run_id,
                        role="assistant",
                        content={"text": output},
                    )
                )
            return ended

    async def fail(
        self, run: ClaimedRun, owner: str, *, code: FailureCode, message: str, usage: Usage
    ) -> bool:
        """Ends the run as ``failed``, or as ``cancelled`` for the code ``cancelled``."""
        async with self._engine.begin() as connection:
            return await self._end(
                connection,
                run,
                owner,
                status="cancelled" if code is FailureCode.CANCELLED else "failed",
                usage=usage,
                failure_code=code.value,
                failure_message=message,
            )

    async def pause(
        self,
        run: ClaimedRun,
        owner: str,
        *,
        approval_id: str,
        tool_name: str,
        arguments: Mapping[str, Any],
        reason: str,
        runtime_state: bytes | None,
        usage: Usage,
    ) -> str | None:
        """Pauses the run until its approval is decided. Returns ``paused``, or ``cancelled``
        if a user asked to cancel the run, or None if the lease is lost.

        The tool gate creates the approval row when it answers ``ask``. If no row exists (for
        example, a runtime with a static gate), this call creates it, so every paused run can
        be decided and can time out.
        """
        async with self._engine.begin() as connection:
            cancel = (
                await connection.execute(
                    select(Run.cancel_requested_at.is_not(None))
                    .where(*self._owned(run, owner))
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if cancel is None:
                return None
            if cancel:
                # The cancel came after the last heartbeat. The run must not wait to resume.
                await self._end(
                    connection,
                    run,
                    owner,
                    status="cancelled",
                    usage=usage,
                    failure_code="cancelled",
                    failure_message="cancelled",
                )
                return "cancelled"
            await self._end(
                connection,
                run,
                owner,
                status="paused",
                usage=usage,
                runtime_state=_encode_state(runtime_state),
                pending_approval_id=approval_id,
            )
            created = (
                await connection.execute(
                    pg_insert(Approval)
                    .values(
                        org_id=run.org_id,
                        session_id=run.session_id,
                        run_id=run.run_id,
                        call_id=approval_id,
                        tool_name=tool_name,
                        arguments=stored_arguments(arguments),
                        arguments_digest=arguments_digest(arguments),
                        reason=reason,
                        expires_at=func.now() + self._approval_ttl,
                    )
                    .on_conflict_do_nothing(index_elements=["run_id", "call_id"])
                    .returning(Approval.id)
                )
            ).scalar_one_or_none()
            if created is not None:
                await connection.execute(
                    insert(AuditEntry).values(
                        org_id=run.org_id,
                        workspace_id=run.workspace_id,
                        actor_kind="system",
                        action="approval.requested",
                        target_type="tool_call",
                        target_id=approval_id,
                        run_id=run.run_id,
                        details={"session_id": str(run.session_id), "tool": tool_name},
                    )
                )
            return "paused"

    async def _end(
        self,
        connection: AsyncConnection,
        run: ClaimedRun,
        owner: str,
        *,
        status: str,
        usage: Usage,
        **values: Any,
    ) -> bool:
        finished = func.now() if status != "paused" else None
        # A pause sets ``pending_approval_id`` again in ``values``. Any other end clears it.
        values = {"pending_approval_id": None, **values}
        row = (
            await connection.execute(
                update(Run)
                .where(*self._owned(run, owner))
                .values(
                    status=status,
                    usage=usage.model_dump(mode="json"),
                    lease_owner=None,
                    lease_expires_at=None,
                    resume_decision=None,
                    resume_reason=None,
                    finished_at=finished,
                    **values,
                )
                .returning(Run.id)
            )
        ).one_or_none()
        return row is not None

    @staticmethod
    def _owned(run: ClaimedRun, owner: str) -> tuple[Any, ...]:
        """The run is ``running``, in this attempt, for this owner, and the lease is valid.

        The attempt fences an old attempt of the same owner after a reap and a new claim. The
        expiry check refuses writes after the lease ended, even before a reap.
        """
        return (
            Run.org_id == run.org_id,
            Run.id == run.run_id,
            Run.status == "running",
            Run.lease_owner == owner,
            Run.attempts == run.attempt,
            Run.lease_expires_at > func.now(),
        )

    async def reap(self) -> tuple[list[RunRef], list[RunRef]]:
        """Handles running runs whose lease expired. Returns the runs that went back to the
        queue and the runs that ended.

        A run goes back to the queue only if the worker did not give it to the runtime
        (``dispatched_at`` is null), so no tool of the run can have run.
        """
        async with self._engine.begin() as connection:
            due = (
                await connection.execute(
                    select(
                        Run.org_id,
                        Run.id,
                        Run.cancel_requested_at.is_not(None).label("cancel"),
                        and_(Run.attempts < self._max_attempts, Run.dispatched_at.is_(None)).label(
                            "retry"
                        ),
                    )
                    .where(Run.status == "running", Run.lease_expires_at < func.now())
                    .order_by(Run.lease_expires_at)
                    .limit(REAP_BATCH)
                    .with_for_update(of=Run, skip_locked=True)
                )
            ).all()
            requeued: list[RunRef] = []
            ended: list[RunRef] = []
            for row in due:
                if row.cancel:
                    values: dict[str, Any] = {**_CANCELLED, "finished_at": func.now()}
                elif row.retry:
                    values = {"status": "queued"}
                else:
                    values = {
                        "status": "failed",
                        "failure_code": "internal",
                        "failure_message": CRASH_MESSAGE,
                        "finished_at": func.now(),
                    }
                (requeued if values["status"] == "queued" else ended).append(
                    RunRef(row.org_id, row.id)
                )
                await connection.execute(
                    update(Run)
                    .where(Run.id == row.id)
                    .values(lease_owner=None, lease_expires_at=None, **values)
                )
        return requeued, ended
