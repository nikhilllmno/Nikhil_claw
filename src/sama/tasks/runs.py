"""The ``task_*`` tools: the task board for a run (docs/architecture.md, Section 12.4).

A run sees the cards that its session sees: the shared cards of the workspace and, in a
private session, the private cards of the session owner. A new card gets the audience and the
owner of the session. In a run that the dispatcher started for a card, ``task_id`` is that card
when the model does not give one, and a new card is a child of that card.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Literal

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from sama.db.models import ENDED_RUN_STATUSES, Run, Session, Task
from sama.tasks.service import (
    Change,
    NewTask,
    TaskError,
    TaskView,
    Viewer,
    add_comment,
    change_task,
    create_task,
    list_tasks,
)
from sama.tenancy.authority import run_user
from sama.tenancy.run_token import RunTokenClaims

Assignee = Literal["self", "agent", "person", "none"]
TOOL_LIST_LIMIT: Final = 50


@dataclass(frozen=True, slots=True)
class RunContext:
    viewer: Viewer
    audience: str
    profile_id: uuid.UUID
    user_id: uuid.UUID | None
    run_id: uuid.UUID
    current_task_id: uuid.UUID | None


def _uuid(value: str | None, name: str) -> uuid.UUID | None:
    if value is None or not value.strip():
        return None
    try:
        return uuid.UUID(value.strip())
    except ValueError:
        raise TaskError(f"the {name} is not a valid id") from None


class TaskTools:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def _context(self, connection: AsyncConnection, claims: RunTokenClaims) -> RunContext:
        org_id = uuid.UUID(claims.org_id)
        session_id = uuid.UUID(claims.session_id)
        run_id = uuid.UUID(claims.run_id)
        row = (
            await connection.execute(
                select(
                    Session.workspace_id,
                    Session.audience,
                    Session.owner_user_id,
                    Run.agent_profile_id,
                    Run.triggered_by_user_id,
                    Run.status,
                )
                .join(Run, and_(Run.org_id == Session.org_id, Run.session_id == Session.id))
                .where(Session.org_id == org_id, Session.id == session_id, Run.id == run_id)
            )
        ).one_or_none()
        if row is None or row.status in ENDED_RUN_STATUSES:
            raise TaskError("the run is not active")
        current = (
            await connection.execute(
                select(Task.id)
                .where(Task.org_id == org_id, Task.session_id == session_id)
                .order_by(Task.updated_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        owner = row.owner_user_id if row.audience == "private" else None
        return RunContext(
            viewer=Viewer(org_id=org_id, workspace_id=row.workspace_id, owner_user_id=owner),
            audience=row.audience,
            profile_id=row.agent_profile_id,
            user_id=run_user(row.triggered_by_user_id, row.audience, row.owner_user_id),
            run_id=run_id,
            current_task_id=current,
        )

    async def create(
        self,
        claims: RunTokenClaims,
        *,
        title: str,
        body: str = "",
        assignee: Assignee = "self",
        profile_id: str | None = None,
        depends_on: Sequence[str] = (),
        parent_task_id: str | None = None,
    ) -> TaskView:
        async with self._engine.begin() as connection:
            context = await self._context(connection, claims)
            assignee_profile: uuid.UUID | None = None
            assignee_user: uuid.UUID | None = None
            if assignee == "self":
                assignee_profile = context.profile_id
            elif assignee == "agent":
                assignee_profile = _uuid(profile_id, "profile_id")
                if assignee_profile is None:
                    raise TaskError("give profile_id for the assignee agent")
            elif assignee == "person":
                if context.user_id is None:
                    raise TaskError("this run has no person to assign")
                assignee_user = context.user_id
            parent = _uuid(parent_task_id, "parent_task_id") or context.current_task_id
            return await create_task(
                connection,
                context.viewer,
                NewTask(
                    title=title,
                    body=body,
                    audience=context.audience,
                    owner_user_id=context.viewer.owner_user_id,
                    assignee_profile_id=assignee_profile,
                    assignee_user_id=assignee_user,
                    parent_task_id=parent,
                    depends_on=[
                        link for d in depends_on if (link := _uuid(d, "dependency")) is not None
                    ],
                    created_by_user_id=context.user_id,
                    created_by_run_id=context.run_id,
                ),
            )

    async def list(
        self, claims: RunTokenClaims, *, statuses: Sequence[str] = (), limit: int = 20
    ) -> tuple[list[TaskView], uuid.UUID | None]:
        async with self._engine.connect() as connection:
            context = await self._context(connection, claims)
            found = await list_tasks(
                connection, context.viewer, statuses=statuses, limit=min(limit, TOOL_LIST_LIMIT)
            )
        return found, context.current_task_id

    async def finish(
        self,
        claims: RunTokenClaims,
        *,
        status: Literal["done", "blocked"],
        text: str,
        task_id: str | None = None,
    ) -> TaskView:
        async with self._engine.begin() as connection:
            context = await self._context(connection, claims)
            target = _uuid(task_id, "task_id") or context.current_task_id
            if target is None:
                raise TaskError("give task_id: this run is not a task run")
            if status == "done":
                change = Change(status="done", result=text)
            else:
                change = Change(status="blocked")
            changed = await change_task(connection, context.viewer, target, change)
            if changed.cancel_run_id is not None and target != context.current_task_id:
                # The raise undoes the change. Only a person stops the run of another card.
                raise TaskError("the task runs in another session. Only a person can stop it.")
            if status == "blocked" and text.strip():
                await add_comment(
                    connection,
                    context.viewer,
                    target,
                    text,
                    author_user_id=None,
                    run_id=context.run_id,
                )
        # The run of this card is the run that calls the tool. It ends by itself.
        return changed.task

    async def comment(self, claims: RunTokenClaims, *, body: str, task_id: str | None) -> None:
        async with self._engine.begin() as connection:
            context = await self._context(connection, claims)
            target = _uuid(task_id, "task_id") or context.current_task_id
            if target is None:
                raise TaskError("give task_id: this run is not a task run")
            await add_comment(
                connection, context.viewer, target, body, author_user_id=None, run_id=context.run_id
            )


def _line(task: TaskView, current: uuid.UUID | None = None) -> str:
    who = (
        f"agent {task.assignee_profile_id}"
        if task.assignee_profile_id
        else f"person {task.assignee_user_id}"
        if task.assignee_user_id
        else "nobody"
    )
    mark = " (this run's task)" if current is not None and task.id == current else ""
    return f"- {task.id} [{task.status}] {task.title}{mark} -- assignee: {who}"


def render_task(task: TaskView, verb: str) -> str:
    lines = [f"{verb} task {task.id} [{task.status}] {task.title}"]
    if task.parent_task_id:
        lines.append(f"parent: {task.parent_task_id}")
    if task.depends_on:
        lines.append("depends on: " + ", ".join(map(str, task.depends_on)))
    return "\n".join(lines)


def render_tasks(tasks: Sequence[TaskView], current: uuid.UUID | None) -> str:
    if not tasks:
        return "The board has no tasks that match."
    lines = [_line(task, current) for task in tasks]
    return "\n".join(lines)
