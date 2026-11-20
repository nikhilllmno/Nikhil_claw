"""The task board (docs/architecture.md, Section 12.4).

A card is a row of ``tasks``. It has the audience of a session: a shared card is visible to
each member of the workspace, and a private card only to its owner. A card with an agent
assignee is work for the dispatcher (``sama.tasks.dispatcher``). A card with a human assignee
waits for that person.

The functions here check the input and write the rows. The caller checks that the person or
the run can see the workspace and the card, and gives the ``Viewer``.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from sqlalchemy import and_, func, insert, or_, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from sama.db.models import (
    ENDED_TASK_STATUSES,
    MAX_TASK_DEPENDENCIES,
    AgentProfile,
    Task,
    TaskComment,
)
from sama.tenancy.access import load_grants
from sama.vault.redact import redact

MAX_TITLE_CHARS: Final = 200
MAX_BODY_CHARS: Final = 20_000
MAX_RESULT_CHARS: Final = 20_000
MAX_COMMENT_CHARS: Final = 4_000
MAX_DEPTH: Final = 5
"""A card has 5 parent cards at most. An agent cannot make an endless chain of cards."""
MAX_OPEN_TASKS: Final = 500
"""A workspace has 500 open cards at most (all statuses except done and cancelled)."""
LIST_LIMIT: Final = 200
PERSON_STATUSES: Final = ("todo", "blocked", "done", "cancelled")
"""The statuses that a person or an agent can set. Only the dispatcher sets ``running``."""


class TaskError(Exception):
    """The board cannot do the operation. The message is safe to show."""


class TaskNotFoundError(TaskError):
    pass


@dataclass(frozen=True, slots=True)
class Viewer:
    """Who reads or writes cards. ``owner_user_id`` is the user whose private cards are
    visible: the person, or the owner of the private session of a run. ``None`` sees only
    shared cards."""

    org_id: uuid.UUID
    workspace_id: uuid.UUID
    owner_user_id: uuid.UUID | None


@dataclass(frozen=True, slots=True)
class NewTask:
    title: str
    body: str = ""
    audience: str = "shared"
    owner_user_id: uuid.UUID | None = None
    assignee_profile_id: uuid.UUID | None = None
    assignee_user_id: uuid.UUID | None = None
    parent_task_id: uuid.UUID | None = None
    depends_on: Sequence[uuid.UUID] = ()
    created_by_user_id: uuid.UUID | None = None
    created_by_run_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class TaskView:
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


@dataclass(frozen=True, slots=True)
class CommentView:
    id: uuid.UUID
    task_id: uuid.UUID
    author_kind: str
    author_user_id: uuid.UUID | None
    run_id: uuid.UUID | None
    body: str
    created_at: datetime


TASK_COLUMNS: Final = (
    Task.id,
    Task.workspace_id,
    Task.title,
    Task.body,
    Task.status,
    Task.audience,
    Task.owner_user_id,
    Task.assignee_profile_id,
    Task.assignee_user_id,
    Task.parent_task_id,
    Task.depends_on,
    Task.session_id,
    Task.run_id,
    Task.result,
    Task.created_by_user_id,
    Task.created_at,
    Task.updated_at,
    Task.finished_at,
)


COMMENT_COLUMNS: Final = (
    TaskComment.id,
    TaskComment.task_id,
    TaskComment.author_kind,
    TaskComment.author_user_id,
    TaskComment.run_id,
    TaskComment.body,
    TaskComment.created_at,
)


def task_view(row: Any) -> TaskView:
    return TaskView(
        id=row.id,
        workspace_id=row.workspace_id,
        title=row.title,
        body=row.body,
        status=row.status,
        audience=row.audience,
        owner_user_id=row.owner_user_id,
        assignee_profile_id=row.assignee_profile_id,
        assignee_user_id=row.assignee_user_id,
        parent_task_id=row.parent_task_id,
        depends_on=list(row.depends_on or []),
        session_id=row.session_id,
        run_id=row.run_id,
        result=row.result,
        created_by_user_id=row.created_by_user_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
        finished_at=row.finished_at,
    )


def visible(viewer: Viewer) -> Any:
    """The SQL rule for the cards that ``viewer`` sees in its workspace. It is the same rule
    as the select policy of ``tasks``, for one workspace."""
    shared = Task.audience == "shared"
    rule = (
        shared
        if viewer.owner_user_id is None
        else or_(
            shared, and_(Task.audience == "private", Task.owner_user_id == viewer.owner_user_id)
        )
    )
    return and_(Task.org_id == viewer.org_id, Task.workspace_id == viewer.workspace_id, rule)


def _text(value: str, name: str, limit: int, *, required: bool = False) -> str:
    text = redact(value).strip()
    if required and not text:
        raise TaskError(f"the {name} is empty")
    if len(text) > limit:
        raise TaskError(f"the {name} has {limit} characters at most")
    return text


async def load_task(connection: AsyncConnection, viewer: Viewer, task_id: uuid.UUID) -> TaskView:
    row = (
        await connection.execute(select(*TASK_COLUMNS).where(visible(viewer), Task.id == task_id))
    ).one_or_none()
    if row is None:
        raise TaskNotFoundError("the task does not exist")
    return task_view(row)


async def list_tasks(
    connection: AsyncConnection,
    viewer: Viewer,
    *,
    statuses: Sequence[str] = (),
    limit: int = LIST_LIMIT,
) -> list[TaskView]:
    query = select(*TASK_COLUMNS).where(visible(viewer))
    if statuses:
        query = query.where(Task.status.in_(list(statuses)))
    rows = await connection.execute(
        query.order_by(Task.created_at.desc(), Task.id).limit(min(limit, LIST_LIMIT))
    )
    return [task_view(row) for row in rows]


async def _check_links(
    connection: AsyncConnection, viewer: Viewer, new: NewTask, task_id: uuid.UUID | None = None
) -> None:
    """The parent and the dependencies are visible cards of the workspace, and the chain of
    parents is not too long. A shared card links only to shared cards: the run of a shared
    card reads the results of its dependencies, and a private result must not leak."""
    if new.audience != "private":
        viewer = Viewer(org_id=viewer.org_id, workspace_id=viewer.workspace_id, owner_user_id=None)
    links = list(dict.fromkeys(new.depends_on))
    if len(links) != len(new.depends_on):
        raise TaskError("a dependency is in the list two times")
    if len(links) > MAX_TASK_DEPENDENCIES:
        raise TaskError(f"a task has {MAX_TASK_DEPENDENCIES} dependencies at most")
    if task_id is not None and task_id in links:
        raise TaskError("a task cannot depend on itself")
    if links:
        found = set(
            (
                await connection.execute(select(Task.id).where(visible(viewer), Task.id.in_(links)))
            ).scalars()
        )
        if missing := [str(link) for link in links if link not in found]:
            raise TaskError("the dependency does not exist: " + ", ".join(missing[:3]))
    parent = new.parent_task_id
    depth = 0
    while parent is not None:
        depth += 1
        if depth > MAX_DEPTH:
            raise TaskError(f"a task has {MAX_DEPTH} parent tasks at most")
        row = (
            await connection.execute(
                select(Task.parent_task_id).where(visible(viewer), Task.id == parent)
            )
        ).one_or_none()
        if row is None:
            raise TaskError("the parent task does not exist")
        parent = row.parent_task_id


async def _check_assignee(connection: AsyncConnection, viewer: Viewer, new: NewTask) -> None:
    if new.assignee_profile_id is not None and new.assignee_user_id is not None:
        raise TaskError("a task has one assignee: an agent or a person")
    if new.assignee_profile_id is not None:
        workspace = (
            await connection.execute(
                select(AgentProfile.workspace_id).where(
                    AgentProfile.org_id == viewer.org_id,
                    AgentProfile.id == new.assignee_profile_id,
                )
            )
        ).one_or_none()
        if workspace is None or workspace[0] not in (None, viewer.workspace_id):
            raise TaskError("the agent profile does not exist in this workspace")
    if new.assignee_user_id is not None:
        if new.audience == "private" and new.assignee_user_id != new.owner_user_id:
            raise TaskError("only the owner of a private task can be its assignee")
        grants = await load_grants(connection, viewer.org_id, new.assignee_user_id)
        if not grants.sees_workspace(viewer.workspace_id):
            raise TaskError("the person is not a member of this workspace")


async def create_task(connection: AsyncConnection, viewer: Viewer, new: NewTask) -> TaskView:
    """Checks and stores a new ``todo`` card. Raises ``TaskError``."""
    if new.audience not in ("private", "shared"):
        raise TaskError("the audience is private or shared")
    if new.audience == "private" and new.owner_user_id is None:
        raise TaskError("a private task needs an owner")
    title = _text(new.title, "title", MAX_TITLE_CHARS, required=True)
    body = _text(new.body, "body", MAX_BODY_CHARS)
    await _check_assignee(connection, viewer, new)
    await _check_links(connection, viewer, new)
    open_count = (
        await connection.execute(
            select(func.count()).where(
                Task.org_id == viewer.org_id,
                Task.workspace_id == viewer.workspace_id,
                Task.status.not_in(ENDED_TASK_STATUSES),
            )
        )
    ).scalar_one()
    if open_count >= MAX_OPEN_TASKS:
        raise TaskError(f"the workspace has {MAX_OPEN_TASKS} open tasks. Close some first.")
    row = (
        await connection.execute(
            insert(Task)
            .values(
                org_id=viewer.org_id,
                workspace_id=viewer.workspace_id,
                title=title,
                body=body,
                audience=new.audience,
                owner_user_id=new.owner_user_id if new.audience == "private" else None,
                assignee_profile_id=new.assignee_profile_id,
                assignee_user_id=new.assignee_user_id,
                parent_task_id=new.parent_task_id,
                depends_on=list(dict.fromkeys(new.depends_on)),
                created_by_user_id=new.created_by_user_id,
                created_by_run_id=new.created_by_run_id,
            )
            .returning(*TASK_COLUMNS)
        )
    ).one()
    return task_view(row)


@dataclass(frozen=True, slots=True)
class Change:
    """A change of a card. A field that is ``None`` does not change. ``clear_assignee`` takes
    away the assignee."""

    status: str | None = None
    result: str | None = None
    title: str | None = None
    body: str | None = None
    assignee_profile_id: uuid.UUID | None = None
    assignee_user_id: uuid.UUID | None = None
    clear_assignee: bool = False


@dataclass(frozen=True, slots=True)
class Changed:
    task: TaskView
    cancel_run_id: uuid.UUID | None
    """The run of the card that the caller must cancel after the commit: the card left the
    status ``running``."""


async def change_task(
    connection: AsyncConnection, viewer: Viewer, task_id: uuid.UUID, change: Change
) -> Changed:
    """Changes a card that ``viewer`` sees. Raises ``TaskError``."""
    current = (
        await connection.execute(
            select(*TASK_COLUMNS).where(visible(viewer), Task.id == task_id).with_for_update()
        )
    ).one_or_none()
    if current is None:
        raise TaskNotFoundError("the task does not exist")
    values: dict[str, Any] = {}
    if change.title is not None:
        values["title"] = _text(change.title, "title", MAX_TITLE_CHARS, required=True)
    if change.body is not None:
        values["body"] = _text(change.body, "body", MAX_BODY_CHARS)
    if change.result is not None:
        values["result"] = _text(change.result, "result", MAX_RESULT_CHARS)
    if change.clear_assignee or change.assignee_profile_id or change.assignee_user_id:
        new = NewTask(
            title="-",
            audience=current.audience,
            owner_user_id=current.owner_user_id,
            assignee_profile_id=change.assignee_profile_id,
            assignee_user_id=change.assignee_user_id,
        )
        await _check_assignee(connection, viewer, new)
        values["assignee_profile_id"] = change.assignee_profile_id
        values["assignee_user_id"] = change.assignee_user_id
    cancel_run_id = None
    if change.status is not None and change.status != current.status:
        if change.status not in PERSON_STATUSES:
            raise TaskError("the status is one of: " + ", ".join(PERSON_STATUSES))
        values["status"] = change.status
        if current.status == "running":
            cancel_run_id = current.run_id
        if change.status == "todo":
            # The dispatcher starts a new session for the card.
            values.update(session_id=None, run_id=None, finished_at=None)
        elif change.status in ENDED_TASK_STATUSES:
            values["finished_at"] = func.now()
        else:
            values["finished_at"] = None
    if not values:
        return Changed(task_view(current), None)
    row = (
        await connection.execute(
            update(Task)
            .where(Task.org_id == viewer.org_id, Task.id == task_id)
            .values(**values, updated_at=func.now())
            .returning(*TASK_COLUMNS)
        )
    ).one()
    return Changed(task_view(row), cancel_run_id)


async def add_comment(
    connection: AsyncConnection,
    viewer: Viewer,
    task_id: uuid.UUID,
    body: str,
    *,
    author_user_id: uuid.UUID | None,
    run_id: uuid.UUID | None = None,
) -> CommentView:
    task = await load_task(connection, viewer, task_id)
    text = _text(body, "comment", MAX_COMMENT_CHARS, required=True)
    row = (
        await connection.execute(
            insert(TaskComment)
            .values(
                org_id=viewer.org_id,
                workspace_id=task.workspace_id,
                task_id=task.id,
                author_kind="agent" if run_id is not None else "user",
                author_user_id=author_user_id if run_id is None else None,
                run_id=run_id,
                body=text,
            )
            .returning(*COMMENT_COLUMNS)
        )
    ).one()
    return comment_view(row)


def comment_view(row: Any) -> CommentView:
    return CommentView(
        id=row.id,
        task_id=row.task_id,
        author_kind=row.author_kind,
        author_user_id=row.author_user_id,
        run_id=row.run_id,
        body=row.body,
        created_at=row.created_at,
    )


async def list_comments(
    connection: AsyncConnection, viewer: Viewer, task_id: uuid.UUID, limit: int = 100
) -> list[CommentView]:
    await load_task(connection, viewer, task_id)
    rows = await connection.execute(
        select(*COMMENT_COLUMNS)
        .where(TaskComment.org_id == viewer.org_id, TaskComment.task_id == task_id)
        .order_by(TaskComment.created_at, TaskComment.id)
        .limit(limit)
    )
    return [comment_view(row) for row in rows]
