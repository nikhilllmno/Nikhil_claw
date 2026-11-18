"""The ``todo`` tool: the task list of a session (docs/architecture.md, Section 16.7)."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable, Sequence
from enum import StrEnum
from typing import Final, Protocol

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.db.models import SessionTodo
from sama.tenancy.run_token import RunTokenClaims

MAX_ITEMS: Final = 100
MAX_CONTENT_CHARS: Final = 1_000


class TodoError(ValueError):
    """The change is not valid. The message is for the agent."""


class TodoStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class TodoItem(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1, max_length=64, description="A short id, unique in the list.")
    content: str = Field(min_length=1, max_length=MAX_CONTENT_CHARS)
    status: TodoStatus = TodoStatus.PENDING


class TodoChange(BaseModel):
    """One item in a ``todo`` call. ``content`` and ``status`` can be left out in a merge, to
    keep the current value."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1, max_length=64)
    content: str | None = Field(default=None, min_length=1, max_length=MAX_CONTENT_CHARS)
    status: TodoStatus | None = None


_ITEMS = TypeAdapter(list[TodoItem])
Change = Callable[[list[TodoItem]], list[TodoItem]]


def apply(
    current: Sequence[TodoItem], changes: Sequence[TodoChange], *, merge: bool
) -> list[TodoItem]:
    """The new list. Raises ``TodoError`` for a change that is not valid."""
    ids = [c.id for c in changes]
    if len(ids) != len(set(ids)):
        raise TodoError("each id can be in the list one time only")
    if not merge:
        items: list[TodoItem] = []
        for change in changes:
            if change.content is None:
                raise TodoError(f"item {change.id} needs content when merge is false")
            items.append(
                TodoItem(
                    id=change.id, content=change.content, status=change.status or TodoStatus.PENDING
                )
            )
    else:
        by_id = {item.id: item for item in current}
        for change in changes:
            old = by_id.get(change.id)
            if old is None:
                if change.content is None:
                    raise TodoError(f"item {change.id} is new, so it needs content")
                by_id[change.id] = TodoItem(
                    id=change.id, content=change.content, status=change.status or TodoStatus.PENDING
                )
            else:
                by_id[change.id] = TodoItem(
                    id=old.id,
                    content=change.content if change.content is not None else old.content,
                    status=change.status or old.status,
                )
        items = list(by_id.values())
    if len(items) > MAX_ITEMS:
        raise TodoError(f"the list can have at most {MAX_ITEMS} items")
    return items


_MARKS: Final = {
    TodoStatus.PENDING: "[ ]",
    TodoStatus.IN_PROGRESS: "[>]",
    TodoStatus.COMPLETED: "[x]",
    TodoStatus.CANCELLED: "[-]",
}


def render(items: Sequence[TodoItem]) -> str:
    if not items:
        return "The task list is empty."
    counts = {status: sum(1 for i in items if i.status is status) for status in TodoStatus}
    summary = ", ".join(f"{n} {s.value.replace('_', ' ')}" for s, n in counts.items() if n)
    lines = [f"Task list ({len(items)} items: {summary}):"]
    lines += [f"{_MARKS[i.status]} {i.id}: {i.content}" for i in items]
    return "\n".join(lines)


class TodoStore(Protocol):
    async def read(self, claims: RunTokenClaims) -> list[TodoItem]: ...

    async def change(self, claims: RunTokenClaims, change: Change) -> list[TodoItem]:
        """Applies ``change`` to the current list as one atomic step and returns the new list."""
        ...


class MemoryTodoStore:
    """For tests and local runs. One process only."""

    def __init__(self) -> None:
        self.lists: dict[tuple[str, str], list[TodoItem]] = {}
        self._lock = asyncio.Lock()

    async def read(self, claims: RunTokenClaims) -> list[TodoItem]:
        return list(self.lists.get((claims.org_id, claims.session_id), []))

    async def change(self, claims: RunTokenClaims, change: Change) -> list[TodoItem]:
        async with self._lock:
            key = (claims.org_id, claims.session_id)
            items = change(list(self.lists.get(key, [])))
            self.lists[key] = items
            return list(items)


class PostgresTodoStore:
    """``session_todos``, written as the table owner. A change locks the row of the session."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def read(self, claims: RunTokenClaims) -> list[TodoItem]:
        query = select(SessionTodo.items).where(
            SessionTodo.org_id == uuid.UUID(claims.org_id),
            SessionTodo.session_id == uuid.UUID(claims.session_id),
        )
        async with self._engine.connect() as connection:
            raw = (await connection.execute(query)).scalar_one_or_none()
        return _ITEMS.validate_python(raw or [])

    async def change(self, claims: RunTokenClaims, change: Change) -> list[TodoItem]:
        org_id, session_id = uuid.UUID(claims.org_id), uuid.UUID(claims.session_id)
        async with self._engine.begin() as connection:
            await connection.execute(
                insert(SessionTodo)
                .values(
                    org_id=org_id,
                    workspace_id=uuid.UUID(claims.workspace_id),
                    session_id=session_id,
                    items=[],
                )
                .on_conflict_do_nothing(index_elements=[SessionTodo.org_id, SessionTodo.session_id])
            )
            raw = (
                await connection.execute(
                    select(SessionTodo.items)
                    .where(SessionTodo.org_id == org_id, SessionTodo.session_id == session_id)
                    .with_for_update()
                )
            ).scalar_one()
            items = change(_ITEMS.validate_python(raw))
            await connection.execute(
                update(SessionTodo)
                .where(SessionTodo.org_id == org_id, SessionTodo.session_id == session_id)
                .values(
                    items=_ITEMS.dump_python(items, mode="json"),
                    run_id=uuid.UUID(claims.run_id),
                    updated_at=func.now(),
                )
            )
        return items
