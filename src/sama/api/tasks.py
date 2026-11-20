"""The task board routes of the web API (docs/architecture.md, Section 12.4).

A person sees the shared cards of a workspace that the person can see, and the private cards
that the person owns. The reads run as the user, so RLS applies. The writes use the service
connection after the same check, because the ``authenticated`` role can only read cards.
"""

from __future__ import annotations

import contextlib
import uuid
from datetime import datetime
from typing import Annotated, Any, Literal, Self

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from sama.api.auth import Principal
from sama.db.engine import as_user
from sama.db.models import MAX_TASK_DEPENDENCIES, User, Workspace
from sama.orchestrator.queue import QueueError, RunQueue
from sama.tasks.service import (
    LIST_LIMIT,
    MAX_BODY_CHARS,
    MAX_COMMENT_CHARS,
    MAX_RESULT_CHARS,
    MAX_TITLE_CHARS,
    Change,
    CommentView,
    NewTask,
    TaskError,
    TaskNotFoundError,
    TaskView,
    Viewer,
    add_comment,
    change_task,
    create_task,
    list_comments,
    list_tasks,
    load_task,
)
from sama.tenancy.access import load_grants

TaskStatus = Literal["todo", "running", "blocked", "done", "cancelled"]
SetStatus = Literal["todo", "blocked", "done", "cancelled"]


class TaskOut(BaseModel):
    id: uuid.UUID
    workspace_id: uuid.UUID
    title: str
    body: str
    status: str
    audience: str
    owner_user_id: uuid.UUID | None
    assignee_profile_id: uuid.UUID | None
    assignee_user_id: uuid.UUID | None
    parent_task_id: uuid.UUID | None
    depends_on: list[uuid.UUID]
    session_id: uuid.UUID | None
    run_id: uuid.UUID | None
    result: str | None
    created_by_user_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None


class CommentOut(BaseModel):
    id: uuid.UUID
    task_id: uuid.UUID
    author_kind: str
    author_user_id: uuid.UUID | None
    run_id: uuid.UUID | None
    body: str
    created_at: datetime


class TaskList(BaseModel):
    user_id: uuid.UUID
    """The user row of the caller in the org. The web app uses it for "assign to me"."""
    tasks: list[TaskOut]


class NewTaskIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: Annotated[str, Field(min_length=1, max_length=MAX_TITLE_CHARS)]
    body: Annotated[str, Field(max_length=MAX_BODY_CHARS)] = ""
    audience: Literal["private", "shared"] = "shared"
    assignee_profile_id: uuid.UUID | None = None
    assignee_user_id: uuid.UUID | None = None
    parent_task_id: uuid.UUID | None = None
    depends_on: Annotated[list[uuid.UUID], Field(max_length=MAX_TASK_DEPENDENCIES)] = []

    @model_validator(mode="after")
    def _one_assignee(self) -> Self:
        if self.assignee_profile_id is not None and self.assignee_user_id is not None:
            raise ValueError("give an agent profile or a person, not both")
        return self


class TaskEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: SetStatus | None = None
    title: Annotated[str | None, Field(min_length=1, max_length=MAX_TITLE_CHARS)] = None
    body: Annotated[str | None, Field(max_length=MAX_BODY_CHARS)] = None
    result: Annotated[str | None, Field(max_length=MAX_RESULT_CHARS)] = None
    assignee: Literal["agent", "person", "none"] | None = None
    """With ``agent``, give ``assignee_profile_id``. With ``person``, give
    ``assignee_user_id``."""
    assignee_profile_id: uuid.UUID | None = None
    assignee_user_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _assignee(self) -> Self:
        if self.assignee == "agent" and self.assignee_profile_id is None:
            raise ValueError("give assignee_profile_id")
        if self.assignee == "person" and self.assignee_user_id is None:
            raise ValueError("give assignee_user_id")
        return self


class NewComment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    body: Annotated[str, Field(min_length=1, max_length=MAX_COMMENT_CHARS)]


def _task_out(task: TaskView) -> TaskOut:
    return TaskOut.model_validate(task, from_attributes=True)


def _comment_out(comment: CommentView) -> CommentOut:
    return CommentOut.model_validate(comment, from_attributes=True)


def _error(error: TaskError) -> HTTPException:
    if isinstance(error, TaskNotFoundError):
        return HTTPException(status_code=404, detail=str(error))
    return HTTPException(status_code=422, detail=str(error))


class TaskBoard:
    def __init__(self, engine: AsyncEngine, queue: RunQueue) -> None:
        self._engine = engine
        self._queue = queue

    async def _viewer(
        self, connection: AsyncConnection, principal: Principal, workspace_id: uuid.UUID
    ) -> tuple[Viewer, uuid.UUID]:
        """The viewer and the user row. Raises ``404`` if the user cannot see the workspace.
        ``connection`` runs as the user."""
        org_id = (
            await connection.execute(select(Workspace.org_id).where(Workspace.id == workspace_id))
        ).scalar_one_or_none()
        user_id = (
            None
            if org_id is None
            else (
                await connection.execute(
                    select(User.id).where(
                        User.org_id == org_id, User.auth_user_id == principal.auth_user_id
                    )
                )
            ).scalar_one_or_none()
        )
        if org_id is None or user_id is None:
            raise HTTPException(status_code=404, detail="the workspace does not exist")
        grants = await load_grants(connection, org_id, user_id)
        if not grants.sees_workspace(workspace_id):
            raise HTTPException(status_code=404, detail="the workspace does not exist")
        return Viewer(org_id=org_id, workspace_id=workspace_id, owner_user_id=user_id), user_id

    async def list(
        self,
        principal: Principal,
        workspace_id: uuid.UUID,
        statuses: list[TaskStatus],
        limit: int = LIST_LIMIT,
    ) -> TaskList:
        async with as_user(self._engine, principal.auth_user_id) as connection:
            viewer, user_id = await self._viewer(connection, principal, workspace_id)
            tasks = await list_tasks(connection, viewer, statuses=statuses, limit=limit)
        return TaskList(user_id=user_id, tasks=[_task_out(task) for task in tasks])

    async def get(
        self, principal: Principal, workspace_id: uuid.UUID, task_id: uuid.UUID
    ) -> dict[str, Any]:
        async with as_user(self._engine, principal.auth_user_id) as connection:
            viewer, _ = await self._viewer(connection, principal, workspace_id)
            try:
                task = await load_task(connection, viewer, task_id)
                comments = await list_comments(connection, viewer, task_id)
            except TaskError as error:
                raise _error(error) from None
        return {
            "task": _task_out(task).model_dump(mode="json"),
            "comments": [_comment_out(c).model_dump(mode="json") for c in comments],
        }

    async def create(
        self, principal: Principal, workspace_id: uuid.UUID, body: NewTaskIn
    ) -> TaskOut:
        async with as_user(self._engine, principal.auth_user_id) as connection:
            viewer, user_id = await self._viewer(connection, principal, workspace_id)
        try:
            async with self._engine.begin() as connection:
                task = await create_task(
                    connection,
                    viewer,
                    NewTask(
                        title=body.title,
                        body=body.body,
                        audience=body.audience,
                        owner_user_id=user_id if body.audience == "private" else None,
                        assignee_profile_id=body.assignee_profile_id,
                        assignee_user_id=body.assignee_user_id,
                        parent_task_id=body.parent_task_id,
                        depends_on=body.depends_on,
                        created_by_user_id=user_id,
                    ),
                )
        except TaskError as error:
            raise _error(error) from None
        return _task_out(task)

    async def edit(
        self, principal: Principal, workspace_id: uuid.UUID, task_id: uuid.UUID, body: TaskEdit
    ) -> TaskOut:
        async with as_user(self._engine, principal.auth_user_id) as connection:
            viewer, _ = await self._viewer(connection, principal, workspace_id)
        change = Change(
            status=body.status,
            title=body.title,
            body=body.body,
            result=body.result,
            assignee_profile_id=body.assignee_profile_id if body.assignee == "agent" else None,
            assignee_user_id=body.assignee_user_id if body.assignee == "person" else None,
            clear_assignee=body.assignee == "none",
        )
        try:
            async with self._engine.begin() as connection:
                changed = await change_task(connection, viewer, task_id, change)
        except TaskError as error:
            raise _error(error) from None
        if changed.cancel_run_id is not None:
            # The card left ``running``: stop its run. A run that ended already is fine.
            with contextlib.suppress(QueueError):
                await self._queue.cancel(org_id=viewer.org_id, run_id=changed.cancel_run_id)
        return _task_out(changed.task)

    async def comment(
        self, principal: Principal, workspace_id: uuid.UUID, task_id: uuid.UUID, body: NewComment
    ) -> CommentOut:
        async with as_user(self._engine, principal.auth_user_id) as connection:
            viewer, user_id = await self._viewer(connection, principal, workspace_id)
        try:
            async with self._engine.begin() as connection:
                comment = await add_comment(
                    connection, viewer, task_id, body.body, author_user_id=user_id
                )
        except TaskError as error:
            raise _error(error) from None
        return _comment_out(comment)
