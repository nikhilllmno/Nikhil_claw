"""Coding mode: attached repositories and plan mode (docs/architecture.md, Section 15.5).

``CodingService`` is control plane code, so it works for every runtime and harness:

- A person attaches a GitHub repository to a session. The repository clones into the session
  sandbox when the next run starts (``sama.coding_mode.prepare``), through the sama git proxy for a
  repository on the allowlist of the org, else from GitHub without credentials.
- Plan mode: while ``sessions.plan_mode`` is true, the tool gate refuses the tools that change
  files or data (``sama.gate.plan``). The agent proposes a plan with ``plan_propose``
  (``propose``). A person approves the plan, also with an edited text, or rejects it. An
  approval turns plan mode off and sends the plan to the agent as a message.

The caller checks that the user can see the session. Each change writes an audit entry.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from sama.db.models import (
    MAX_PLAN_CHARS,
    MAX_PLAN_FEEDBACK_CHARS,
    MAX_SESSION_REPOS,
    AuditEntry,
    Run,
    Session,
    SessionPlan,
    SessionRepo,
)
from sama.orchestrator.queue import QueueError, RunQueue
from sama.tools.repos import GITHUB_URL, RepoError, parse_repo, valid_folder, valid_ref
from sama.vault.redact import redact

PLAN_OFF_NOTED: Final = "session.plan_mode_noted"
"""The audit entry of the run that told the agent that plan mode is off."""
PLAN_LIST_LIMIT: Final = 10
MAX_PLAN_LIST: Final = 50
MAX_PR_TITLE_CHARS: Final = 200


class CodingError(Exception):
    """The message is for the user. ``status`` is the HTTP status of the API."""

    def __init__(self, message: str, status: int = 409) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True, slots=True)
class RepoView:
    id: uuid.UUID
    repo: str
    url: str
    ref: str | None
    folder: str
    path: str | None
    status: str
    proxied: bool | None
    commit: str | None
    branch: str | None
    error: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class PlanView:
    id: uuid.UUID
    version: int
    body: str
    proposed_body: str
    status: str
    edited: bool
    feedback: str | None
    run_id: uuid.UUID | None
    decided_at: datetime | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class CodingState:
    plan_mode: bool
    repos: list[RepoView]
    plan: PlanView | None


@dataclass(frozen=True, slots=True)
class Decided:
    plan: PlanView
    message: str | None
    """The message for the agent, or None when no run is needed."""


_REPO_COLUMNS: Final = (
    SessionRepo.id,
    SessionRepo.owner,
    SessionRepo.name,
    SessionRepo.ref,
    SessionRepo.folder,
    SessionRepo.path,
    SessionRepo.status,
    SessionRepo.proxied,
    SessionRepo.commit,
    SessionRepo.branch,
    SessionRepo.error,
    SessionRepo.created_at,
)
_PLAN_COLUMNS: Final = (
    SessionPlan.id,
    SessionPlan.version,
    SessionPlan.body,
    SessionPlan.proposed_body,
    SessionPlan.status,
    SessionPlan.edited,
    SessionPlan.feedback,
    SessionPlan.run_id,
    SessionPlan.decided_at,
    SessionPlan.created_at,
)


def repo_view(row: Any) -> RepoView:
    return RepoView(
        id=row.id,
        repo=f"{row.owner}/{row.name}",
        url=f"{GITHUB_URL}/{row.owner}/{row.name}",
        ref=row.ref,
        folder=row.folder,
        path=row.path,
        status=row.status,
        proxied=row.proxied,
        commit=row.commit,
        branch=row.branch,
        error=row.error,
        created_at=row.created_at,
    )


def plan_view(row: Any) -> PlanView:
    return PlanView(
        id=row.id,
        version=row.version,
        body=row.body,
        proposed_body=row.proposed_body,
        status=row.status,
        edited=row.edited,
        feedback=row.feedback,
        run_id=row.run_id,
        decided_at=row.decided_at,
        created_at=row.created_at,
    )


def approved_message(plan: PlanView) -> str:
    """The message that sends an approved plan to the agent."""
    edited = (
        " The user edited the plan: follow this text, not your first version."
        if (plan.edited)
        else ""
    )
    return (
        f"I approve the plan (version {plan.version}). Plan mode is off now.{edited}"
        " Do the work of the plan step by step. Run the tests of the project. At the end, tell"
        " me what you changed and the test results.\n\n"
        f"{plan.body}"
    )


def rejected_message(plan: PlanView, feedback: str) -> str:
    return (
        f"I do not approve the plan (version {plan.version}). Plan mode stays on. Change the"
        f" plan, then call plan_propose again.\n\nMy feedback:\n{feedback}"
    )


def pull_request_message(
    *, repo: str | None, title: str | None, base: str | None, draft: bool
) -> str:
    """The message that asks the agent to open a pull request (Section 15.5)."""
    target = f" of {repo}" if repo else ""
    parts = [
        f"Open a pull request with the changes{target} now.",
        "Commit the changes in the repository folder. Push them to a new branch"
        " sama/<short-name> through the git proxy (git push origin HEAD:sama/<short-name>).",
        "Then call pr_create.",
    ]
    if title:
        parts.append(f"Use this title: {title}")
    if base:
        parts.append(f"The base branch is {base}.")
    if draft:
        parts.append("Open it as a draft.")
    parts.append(
        "If the repository was not cloned through the git proxy, you cannot push: tell me that"
        " an org admin must add the repository to the GitHub allowlist of the org."
    )
    return " ".join(parts)


class CodingService:
    def __init__(self, engine: AsyncEngine, queue: RunQueue) -> None:
        self._engine = engine
        self._queue = queue

    @property
    def queue(self) -> RunQueue:
        return self._queue

    # -- reads ------------------------------------------------------------------------------

    async def state(self, *, org_id: uuid.UUID, session_id: uuid.UUID) -> CodingState:
        async with self._engine.connect() as connection:
            plan_mode = (
                await connection.execute(
                    select(Session.plan_mode).where(
                        Session.org_id == org_id, Session.id == session_id
                    )
                )
            ).scalar_one_or_none()
            if plan_mode is None:
                raise CodingError("The session does not exist.", 404)
            repos = await _repos(connection, org_id, session_id)
            plans = await _plans(connection, org_id, session_id, 1)
        return CodingState(plan_mode=bool(plan_mode), repos=repos, plan=plans[0] if plans else None)

    async def plans(
        self, *, org_id: uuid.UUID, session_id: uuid.UUID, limit: int = PLAN_LIST_LIMIT
    ) -> list[PlanView]:
        async with self._engine.connect() as connection:
            return await _plans(connection, org_id, session_id, max(1, min(limit, MAX_PLAN_LIST)))

    async def latest_proposed(self, *, org_id: uuid.UUID, session_id: uuid.UUID) -> PlanView | None:
        async with self._engine.connect() as connection:
            row = (
                await connection.execute(
                    select(*_PLAN_COLUMNS)
                    .where(
                        SessionPlan.org_id == org_id,
                        SessionPlan.session_id == session_id,
                        SessionPlan.status == "proposed",
                    )
                    .order_by(SessionPlan.version.desc())
                    .limit(1)
                )
            ).one_or_none()
        return None if row is None else plan_view(row)

    # -- repositories -----------------------------------------------------------------------

    async def attach(
        self,
        *,
        org_id: uuid.UUID,
        session_id: uuid.UUID,
        user_id: uuid.UUID | None,
        repo: str,
        ref: str | None = None,
        folder: str | None = None,
    ) -> RepoView:
        """Attaches a repository. It clones when the next run of the session starts."""
        try:
            target = parse_repo(repo)
        except RepoError as error:
            raise CodingError(str(error), 422) from None
        ref = (ref or "").strip() or None
        if ref is not None and not valid_ref(ref):
            raise CodingError(f"{ref!r} is not a valid branch or tag name.", 422)
        folder = (folder or "").strip() or target.name
        if not valid_folder(folder):
            raise CodingError(
                "The folder must be one name of letters, digits, '.', '_', or '-'.", 422
            )
        async with self._engine.begin() as connection:
            workspace_id = await _lock_active(connection, org_id, session_id)
            rows = (
                (
                    await connection.execute(
                        select(SessionRepo.folder).where(
                            SessionRepo.org_id == org_id, SessionRepo.session_id == session_id
                        )
                    )
                )
                .scalars()
                .all()
            )
            if len(rows) >= MAX_SESSION_REPOS:
                raise CodingError(f"A session can have at most {MAX_SESSION_REPOS} repositories.")
            if folder in rows:
                raise CodingError(
                    f"The folder {folder} is in use. Give another folder name for this repository."
                )
            row = (
                await connection.execute(
                    insert(SessionRepo)
                    .values(
                        org_id=org_id,
                        session_id=session_id,
                        owner=target.owner,
                        name=target.name,
                        ref=ref,
                        folder=folder,
                        created_by_user_id=user_id,
                    )
                    .returning(*_REPO_COLUMNS)
                )
            ).one()
            await _audit(
                connection,
                org_id,
                workspace_id,
                user_id,
                "session.repo_attached",
                session_id,
                {"repo": f"{target.owner}/{target.name}", "ref": ref, "folder": folder},
            )
        return repo_view(row)

    async def detach(
        self,
        *,
        org_id: uuid.UUID,
        session_id: uuid.UUID,
        user_id: uuid.UUID | None,
        repo_id: uuid.UUID,
    ) -> None:
        """Removes the attachment. The folder in the sandbox stays."""
        async with self._engine.begin() as connection:
            workspace_id = await _lock_session(connection, org_id, session_id)
            row = (
                await connection.execute(
                    delete(SessionRepo)
                    .where(
                        SessionRepo.org_id == org_id,
                        SessionRepo.session_id == session_id,
                        SessionRepo.id == repo_id,
                    )
                    .returning(SessionRepo.owner, SessionRepo.name, SessionRepo.folder)
                )
            ).one_or_none()
            if row is None:
                raise CodingError("The repository is not attached to this session.", 404)
            await _audit(
                connection,
                org_id,
                workspace_id,
                user_id,
                "session.repo_detached",
                session_id,
                {"repo": f"{row.owner}/{row.name}", "folder": row.folder},
            )

    # -- plan mode --------------------------------------------------------------------------

    async def set_plan_mode(
        self, *, org_id: uuid.UUID, session_id: uuid.UUID, user_id: uuid.UUID | None, on: bool
    ) -> bool:
        async with self._engine.begin() as connection:
            await self._set_mode(connection, org_id, session_id, user_id, on)
        return on

    async def _set_mode(
        self,
        connection: AsyncConnection,
        org_id: uuid.UUID,
        session_id: uuid.UUID,
        user_id: uuid.UUID | None,
        on: bool,
    ) -> None:
        workspace_id = await _lock_active(connection, org_id, session_id)
        before = (
            await connection.execute(
                select(Session.plan_mode).where(Session.org_id == org_id, Session.id == session_id)
            )
        ).scalar_one()
        if bool(before) == on:
            return
        await connection.execute(
            update(Session)
            .where(Session.org_id == org_id, Session.id == session_id)
            .values(plan_mode=on)
        )
        await _audit(
            connection,
            org_id,
            workspace_id,
            user_id,
            "session.plan_mode",
            session_id,
            {"on": on},
            actor_kind="user" if user_id is not None else "system",
        )

    async def start_plan(
        self, *, org_id: uuid.UUID, session_id: uuid.UUID, user_id: uuid.UUID | None, task: str
    ) -> uuid.UUID:
        """``/plan TASK``: turns plan mode on and sends the task as a message. Returns the run.
        The queue errors (rate limit, quota, busy rules) go to the caller; plan mode then stays
        on."""
        await self.set_plan_mode(org_id=org_id, session_id=session_id, user_id=user_id, on=True)
        return await self._queue.enqueue(
            org_id=org_id, session_id=session_id, text=task, user_id=user_id
        )

    async def propose(
        self, *, org_id: uuid.UUID, session_id: uuid.UUID, run_id: uuid.UUID | None, body: str
    ) -> PlanView:
        """Stores a plan of the agent (``plan_propose``). The open plan of the session becomes
        ``superseded``. Plan mode turns on, so the agent waits for the decision."""
        body = redact(body.strip())
        if not body:
            raise CodingError("The plan is empty.", 422)
        if len(body) > MAX_PLAN_CHARS:
            raise CodingError(f"The plan has more than {MAX_PLAN_CHARS} characters.", 422)
        async with self._engine.begin() as connection:
            workspace_id = await _lock_active(connection, org_id, session_id)
            await connection.execute(
                update(SessionPlan)
                .where(
                    SessionPlan.org_id == org_id,
                    SessionPlan.session_id == session_id,
                    SessionPlan.status == "proposed",
                )
                .values(status="superseded")
            )
            version = (
                await connection.execute(
                    select(func.coalesce(func.max(SessionPlan.version), 0)).where(
                        SessionPlan.org_id == org_id, SessionPlan.session_id == session_id
                    )
                )
            ).scalar_one() + 1
            row = (
                await connection.execute(
                    insert(SessionPlan)
                    .values(
                        org_id=org_id,
                        session_id=session_id,
                        run_id=run_id,
                        version=version,
                        body=body,
                        proposed_body=body,
                    )
                    .returning(*_PLAN_COLUMNS)
                )
            ).one()
            await connection.execute(
                update(Session)
                .where(Session.org_id == org_id, Session.id == session_id)
                .values(plan_mode=True)
            )
            await _audit(
                connection,
                org_id,
                workspace_id,
                None,
                "plan.proposed",
                session_id,
                {"version": version, "run_id": None if run_id is None else str(run_id)},
                actor_kind="agent",
            )
        return plan_view(row)

    async def approve(
        self,
        *,
        org_id: uuid.UUID,
        session_id: uuid.UUID,
        user_id: uuid.UUID | None,
        plan_id: uuid.UUID,
        body: str | None = None,
    ) -> Decided:
        """Approves a proposed plan, with ``body`` in its place when the person edited it.
        Plan mode turns off. The caller sends ``Decided.message`` to the agent."""
        edited_body = None if body is None else redact(body.strip())
        if edited_body is not None:
            if not edited_body:
                raise CodingError("The edited plan is empty.", 422)
            if len(edited_body) > MAX_PLAN_CHARS:
                raise CodingError(f"The plan has more than {MAX_PLAN_CHARS} characters.", 422)
        async with self._engine.begin() as connection:
            workspace_id = await _lock_active(connection, org_id, session_id)
            current = await _open_plan(connection, org_id, session_id, plan_id)
            final = current.body if edited_body is None else edited_body
            edited = final != current.proposed_body
            row = (
                await connection.execute(
                    update(SessionPlan)
                    .where(SessionPlan.id == current.id)
                    .values(
                        status="approved",
                        body=final,
                        edited=edited,
                        decided_by_user_id=user_id,
                        decided_at=func.now(),
                    )
                    .returning(*_PLAN_COLUMNS)
                )
            ).one()
            await connection.execute(
                update(Session)
                .where(Session.org_id == org_id, Session.id == session_id)
                .values(plan_mode=False)
            )
            await _audit(
                connection,
                org_id,
                workspace_id,
                user_id,
                "plan.approved",
                session_id,
                {"version": current.version, "edited": edited},
            )
        plan = plan_view(row)
        return Decided(plan, approved_message(plan))

    async def reject(
        self,
        *,
        org_id: uuid.UUID,
        session_id: uuid.UUID,
        user_id: uuid.UUID | None,
        plan_id: uuid.UUID,
        feedback: str | None = None,
    ) -> Decided:
        """Rejects a proposed plan. Plan mode stays on. With feedback, the caller sends
        ``Decided.message`` to the agent."""
        text = redact((feedback or "").strip()) or None
        if text is not None and len(text) > MAX_PLAN_FEEDBACK_CHARS:
            raise CodingError(
                f"The feedback has more than {MAX_PLAN_FEEDBACK_CHARS} characters.", 422
            )
        async with self._engine.begin() as connection:
            workspace_id = await _lock_active(connection, org_id, session_id)
            current = await _open_plan(connection, org_id, session_id, plan_id)
            row = (
                await connection.execute(
                    update(SessionPlan)
                    .where(SessionPlan.id == current.id)
                    .values(
                        status="rejected",
                        feedback=text,
                        decided_by_user_id=user_id,
                        decided_at=func.now(),
                    )
                    .returning(*_PLAN_COLUMNS)
                )
            ).one()
            await _audit(
                connection,
                org_id,
                workspace_id,
                user_id,
                "plan.rejected",
                session_id,
                {"version": current.version, "feedback": text is not None},
            )
        plan = plan_view(row)
        return Decided(plan, None if text is None else rejected_message(plan, text))

    async def decide(
        self,
        *,
        org_id: uuid.UUID,
        session_id: uuid.UUID,
        user_id: uuid.UUID | None,
        plan_id: uuid.UUID,
        approve: bool,
        text: str | None = None,
    ) -> tuple[Decided, uuid.UUID | None]:
        """Approves (``text`` is the edited plan) or rejects (``text`` is the feedback) a plan,
        and sends the message for the agent. When the queue refuses the message (a rate limit,
        a quota, or a busy rule), the decision goes back: the plan is ``proposed`` again and
        plan mode is as before, so the person can try again. The queue error goes to the
        caller."""
        if approve:
            decided = await self.approve(
                org_id=org_id, session_id=session_id, user_id=user_id, plan_id=plan_id, body=text
            )
        else:
            decided = await self.reject(
                org_id=org_id,
                session_id=session_id,
                user_id=user_id,
                plan_id=plan_id,
                feedback=text,
            )
        if decided.message is None:
            return decided, None
        try:
            run_id = await self.send(
                org_id=org_id, session_id=session_id, user_id=user_id, text=decided.message
            )
        except QueueError:
            await self._undo_decision(org_id, session_id, user_id, decided.plan, approve)
            raise
        return decided, run_id

    async def _undo_decision(
        self,
        org_id: uuid.UUID,
        session_id: uuid.UUID,
        user_id: uuid.UUID | None,
        plan: PlanView,
        approved: bool,
    ) -> None:
        async with self._engine.begin() as connection:
            workspace_id = await _lock_session(connection, org_id, session_id)
            newest = (
                await connection.execute(
                    select(func.max(SessionPlan.version)).where(
                        SessionPlan.org_id == org_id, SessionPlan.session_id == session_id
                    )
                )
            ).scalar_one()
            mode = (
                await connection.execute(
                    select(Session.plan_mode).where(
                        Session.org_id == org_id, Session.id == session_id
                    )
                )
            ).scalar_one()
            # Only undo when nothing changed since the decision: no newer plan, and (for an
            # approve) plan mode is still off. Else a newer state of the session stays.
            if newest != plan.version or (approved and mode):
                return
            changed = (
                await connection.execute(
                    update(SessionPlan)
                    .where(SessionPlan.id == plan.id, SessionPlan.status == plan.status)
                    .values(
                        status="proposed",
                        body=SessionPlan.proposed_body,
                        edited=False,
                        feedback=None,
                        decided_by_user_id=None,
                        decided_at=None,
                    )
                    .returning(SessionPlan.id)
                )
            ).first()
            if changed is None:
                return
            if approved:
                await connection.execute(
                    update(Session)
                    .where(Session.org_id == org_id, Session.id == session_id)
                    .values(plan_mode=True)
                )
            await _audit(
                connection,
                org_id,
                workspace_id,
                user_id,
                "plan.decision_undone",
                session_id,
                {"version": plan.version, "reason": "the message could not be queued"},
            )

    async def send(
        self, *, org_id: uuid.UUID, session_id: uuid.UUID, user_id: uuid.UUID | None, text: str
    ) -> uuid.UUID:
        """Queues ``text`` as a message of the user. The queue errors go to the caller."""
        return await self._queue.enqueue(
            org_id=org_id, session_id=session_id, text=text, user_id=user_id
        )

    # -- the sandbox side -------------------------------------------------------------------

    async def pending_repos(self, *, org_id: uuid.UUID, session_id: uuid.UUID) -> list[RepoView]:
        async with self._engine.connect() as connection:
            rows = (
                await connection.execute(
                    select(*_REPO_COLUMNS)
                    .where(
                        SessionRepo.org_id == org_id,
                        SessionRepo.session_id == session_id,
                        SessionRepo.status == "pending",
                    )
                    .order_by(SessionRepo.created_at, SessionRepo.id)
                )
            ).all()
        return [repo_view(row) for row in rows]

    async def chat_run(self, *, org_id: uuid.UUID, run_id: uuid.UUID) -> str | None:
        """The runtime of a chat run that answers a message, or None for another run (a
        review, a ``/compact``, or an unknown run)."""
        async with self._engine.connect() as connection:
            row = (
                await connection.execute(
                    select(Run.kind, Run.compact, Run.runtime).where(
                        Run.org_id == org_id, Run.id == run_id
                    )
                )
            ).one_or_none()
        if row is None or row.kind != "chat" or row.compact:
            return None
        return str(row.runtime or "")

    async def turned_off(
        self, *, org_id: uuid.UUID, session_id: uuid.UUID, run_id: uuid.UUID
    ) -> bool:
        """True for the one run that tells the agent that a person turned plan mode off
        (``/plan off``, the switch, the API). The first chat run that prepares after the change
        claims it with the audit entry ``session.plan_mode_noted`` under the session lock; a
        retry of that run gets it again, and no other run gets it. An approval sends its own
        message, so it does not count. The caller skips reviews and ``/compact`` runs."""
        async with self._engine.begin() as connection:
            workspace_id = await _lock_session(connection, org_id, session_id)
            last = (
                await connection.execute(
                    select(AuditEntry.action, AuditEntry.details, AuditEntry.run_id)
                    .where(
                        AuditEntry.org_id == org_id,
                        AuditEntry.target_type == "session",
                        AuditEntry.target_id == str(session_id),
                        AuditEntry.action.in_(
                            ("session.plan_mode", "plan.approved", PLAN_OFF_NOTED)
                        ),
                    )
                    .order_by(AuditEntry.id.desc())
                    .limit(1)
                )
            ).one_or_none()
            if last is None:
                return False
            if last.action == PLAN_OFF_NOTED:
                return bool(last.run_id == run_id)
            if last.action != "session.plan_mode" or (last.details or {}).get("on") is not False:
                return False
            await connection.execute(
                insert(AuditEntry).values(
                    org_id=org_id,
                    workspace_id=workspace_id,
                    actor_kind="system",
                    actor_user_id=None,
                    action=PLAN_OFF_NOTED,
                    target_type="session",
                    target_id=str(session_id),
                    run_id=run_id,
                    details={},
                )
            )
        return True

    async def plan_mode(self, *, org_id: uuid.UUID, session_id: uuid.UUID) -> bool:
        async with self._engine.connect() as connection:
            value = (
                await connection.execute(
                    select(Session.plan_mode).where(
                        Session.org_id == org_id, Session.id == session_id
                    )
                )
            ).scalar_one_or_none()
        return bool(value)

    async def cloned(
        self,
        *,
        org_id: uuid.UUID,
        repo_id: uuid.UUID,
        path: str,
        commit: str,
        branch: str | None,
        proxied: bool,
    ) -> None:
        async with self._engine.begin() as connection:
            await connection.execute(
                update(SessionRepo)
                .where(
                    SessionRepo.org_id == org_id,
                    SessionRepo.id == repo_id,
                    SessionRepo.status == "pending",
                )
                .values(
                    status="ready",
                    path=path,
                    commit=commit,
                    branch=branch,
                    proxied=proxied,
                    error=None,
                    updated_at=func.now(),
                )
            )

    async def clone_failed(self, *, org_id: uuid.UUID, repo_id: uuid.UUID, error: str) -> None:
        async with self._engine.begin() as connection:
            await connection.execute(
                update(SessionRepo)
                .where(
                    SessionRepo.org_id == org_id,
                    SessionRepo.id == repo_id,
                    SessionRepo.status == "pending",
                )
                .values(
                    status="failed",
                    error=redact(error)[:500] or "the clone failed",
                    updated_at=func.now(),
                )
            )


async def _repos(
    connection: AsyncConnection, org_id: uuid.UUID, session_id: uuid.UUID
) -> list[RepoView]:
    rows = (
        await connection.execute(
            select(*_REPO_COLUMNS)
            .where(SessionRepo.org_id == org_id, SessionRepo.session_id == session_id)
            .order_by(SessionRepo.created_at, SessionRepo.id)
        )
    ).all()
    return [repo_view(row) for row in rows]


async def _plans(
    connection: AsyncConnection, org_id: uuid.UUID, session_id: uuid.UUID, limit: int
) -> list[PlanView]:
    rows = (
        await connection.execute(
            select(*_PLAN_COLUMNS)
            .where(SessionPlan.org_id == org_id, SessionPlan.session_id == session_id)
            .order_by(SessionPlan.version.desc())
            .limit(limit)
        )
    ).all()
    return [plan_view(row) for row in rows]


async def _lock_session(
    connection: AsyncConnection, org_id: uuid.UUID, session_id: uuid.UUID
) -> uuid.UUID:
    row = (
        await connection.execute(
            select(Session.workspace_id, Session.status)
            .where(Session.org_id == org_id, Session.id == session_id)
            .with_for_update()
        )
    ).one_or_none()
    if row is None:
        raise CodingError("The session does not exist.", 404)
    return row.workspace_id  # type: ignore[no-any-return]


async def _lock_active(
    connection: AsyncConnection, org_id: uuid.UUID, session_id: uuid.UUID
) -> uuid.UUID:
    row = (
        await connection.execute(
            select(Session.workspace_id, Session.status)
            .where(Session.org_id == org_id, Session.id == session_id)
            .with_for_update()
        )
    ).one_or_none()
    if row is None:
        raise CodingError("The session does not exist.", 404)
    if row.status != "active":
        raise CodingError("The session is not active.")
    return row.workspace_id  # type: ignore[no-any-return]


async def _open_plan(
    connection: AsyncConnection, org_id: uuid.UUID, session_id: uuid.UUID, plan_id: uuid.UUID
) -> Any:
    row = (
        await connection.execute(
            select(*_PLAN_COLUMNS)
            .where(
                SessionPlan.org_id == org_id,
                SessionPlan.session_id == session_id,
                SessionPlan.id == plan_id,
            )
            .with_for_update()
        )
    ).one_or_none()
    if row is None:
        raise CodingError("The plan does not exist.", 404)
    if row.status != "proposed":
        raise CodingError(f"The plan is {row.status}. Only an open plan can be decided.")
    return row


async def _audit(
    connection: AsyncConnection,
    org_id: uuid.UUID,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID | None,
    action: str,
    session_id: uuid.UUID,
    details: dict[str, Any],
    *,
    actor_kind: str = "user",
) -> None:
    await connection.execute(
        insert(AuditEntry).values(
            org_id=org_id,
            workspace_id=workspace_id,
            actor_kind=actor_kind if user_id is None else "user",
            actor_user_id=user_id,
            action=action,
            target_type="session",
            target_id=str(session_id),
            details=details,
        )
    )


def describe_plan(plan: PlanView | None, plan_mode: bool, prefix: str = "/") -> str:
    """The answer to ``/plan`` with no argument."""
    mode = "on" if plan_mode else "off"
    lines = [f"Plan mode is {mode}."]
    if plan is None:
        lines.append(f"This session has no plan. Send {prefix}plan TASK to start one.")
        return "\n".join(lines)
    lines.append(f"Plan version {plan.version} is {plan.status}.")
    if plan.status == "proposed":
        lines += [
            "",
            plan.body,
            "",
            f"Send {prefix}plan approve, {prefix}plan edit NEW PLAN, or {prefix}plan reject"
            " FEEDBACK.",
        ]
    return "\n".join(lines)


__all__: Sequence[str] = (
    "CodingError",
    "CodingService",
    "CodingState",
    "Decided",
    "PlanView",
    "RepoView",
    "approved_message",
    "describe_plan",
    "pull_request_message",
    "rejected_message",
)
