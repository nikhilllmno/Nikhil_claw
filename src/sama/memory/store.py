"""Storage of memory: the store protocol, an in-process store, and the Postgres store
(docs/architecture.md, Section 8).

Every write is one transaction. It locks the row of the scope, applies the change, writes the
new entries, and adds a row to ``memory_revisions`` with the full new content. A change that
raises writes nothing. A change that does not alter the entries writes no revision.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from sqlalchemy import ColumnElement, and_, func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from sama.db.models import Memory, MemoryRevision, Session
from sama.memory.core import MemoryChangeError, Target, render_snapshot
from sama.tenancy.access import load_grants
from sama.tenancy.authority import run_user
from sama.tenancy.run_token import RunTokenClaims

Change = Callable[[list[str]], list[str]]
RevisionAction = Literal["add", "patch", "remove", "batch", "edit", "revert"]


@dataclass(frozen=True, slots=True)
class MemoryScope:
    """The key of one list of notes. ``agent``: workspace and agent profile. ``user``: the
    user only. ``workspace``: the workspace only."""

    org_id: uuid.UUID
    target: Target
    workspace_id: uuid.UUID | None = None
    agent_profile_id: uuid.UUID | None = None
    user_id: uuid.UUID | None = None

    def __post_init__(self) -> None:
        keys = (
            self.workspace_id is not None,
            self.agent_profile_id is not None,
            self.user_id is not None,
        )
        expected = {
            Target.AGENT: (True, True, False),
            Target.USER: (False, False, True),
            Target.WORKSPACE: (True, False, False),
        }[self.target]
        if keys != expected:
            raise ValueError(f"the keys do not fit the {self.target.value} scope")


@dataclass(frozen=True, slots=True)
class Writer:
    """Who wrote. ``agent`` is the memory tool. ``user_id`` is the person (for ``user``) or the
    user of the run (for ``agent``)."""

    kind: Literal["user", "agent"]
    user_id: uuid.UUID | None = None
    session_id: uuid.UUID | None = None
    run_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class MemoryState:
    entries: tuple[str, ...]
    revision: int
    """The ``seq`` of the last revision. 0 if nobody wrote yet."""


@dataclass(frozen=True, slots=True)
class Revision:
    seq: int
    action: str
    entries: tuple[str, ...]
    author_kind: str
    author_user_id: uuid.UUID | None
    session_id: uuid.UUID | None
    run_id: uuid.UUID | None
    reason: str | None
    reverted_to: int | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class RunContext:
    """What the memory tool needs to know about its run."""

    agent_profile_id: uuid.UUID
    acting_user_id: uuid.UUID | None
    """The user of the run: who started it, else the owner of a private session."""
    workspace_admin: bool
    """True if the acting user is a workspace admin or the org owner (Section 8)."""
    unattended: bool
    delegated: bool
    """True for a run of a delegated child session."""
    review: bool = False
    """True for a background review run (Section 9.4)."""


AfterChange = Callable[[AsyncConnection, MemoryState], Awaitable[None]]
"""Runs in the transaction of a change, after the write, with the new state. If it raises, the
whole change is rolled back. The Postgres store calls it. The in-process store has no
transaction and ignores it."""


class MemoryStore(Protocol):
    async def context(self, claims: RunTokenClaims) -> RunContext:
        """Raises ``MemoryChangeError`` if the run does not exist."""
        ...

    async def read(self, scope: MemoryScope) -> MemoryState: ...

    async def change(
        self,
        scope: MemoryScope,
        change: Change,
        *,
        writer: Writer,
        action: RevisionAction,
        reason: str | None = None,
        reverted_to: int | None = None,
        after: AfterChange | None = None,
    ) -> tuple[MemoryState, bool]:
        """Applies ``change`` to the current entries as one atomic step. Returns the new state
        and whether it changed. If ``change`` or ``after`` raises, nothing is written."""
        ...

    async def revisions(self, scope: MemoryScope, limit: int) -> list[Revision]:
        """The newest first."""
        ...

    async def revision(self, scope: MemoryScope, seq: int) -> Revision | None: ...


class InProcessMemoryStore:
    """For tests and local runs. One process only. ``context`` gives the same answer for
    every run, unless ``contexts`` has an entry for the run id."""

    def __init__(self, context: RunContext | None = None) -> None:
        self.default_context = context or RunContext(
            agent_profile_id=uuid.uuid4(),
            acting_user_id=None,
            workspace_admin=False,
            unattended=False,
            delegated=False,
        )
        self.contexts: dict[str, RunContext] = {}
        self._revisions: dict[MemoryScope, list[Revision]] = {}
        self._lock = asyncio.Lock()

    async def context(self, claims: RunTokenClaims) -> RunContext:
        return self.contexts.get(claims.run_id, self.default_context)

    async def read(self, scope: MemoryScope) -> MemoryState:
        revisions = self._revisions.get(scope, [])
        return MemoryState(revisions[-1].entries if revisions else (), len(revisions))

    async def change(
        self,
        scope: MemoryScope,
        change: Change,
        *,
        writer: Writer,
        action: RevisionAction,
        reason: str | None = None,
        reverted_to: int | None = None,
        after: AfterChange | None = None,
    ) -> tuple[MemoryState, bool]:
        async with self._lock:
            state = await self.read(scope)
            entries = tuple(change(list(state.entries)))
            if entries == state.entries:
                return state, False
            revisions = self._revisions.setdefault(scope, [])
            revisions.append(
                Revision(
                    seq=state.revision + 1,
                    action=action,
                    entries=entries,
                    author_kind=writer.kind,
                    author_user_id=writer.user_id,
                    session_id=writer.session_id,
                    run_id=writer.run_id,
                    reason=reason,
                    reverted_to=reverted_to,
                    created_at=datetime.now(UTC),
                )
            )
            return MemoryState(entries, state.revision + 1), True

    async def revisions(self, scope: MemoryScope, limit: int) -> list[Revision]:
        return list(reversed(self._revisions.get(scope, [])))[:limit]

    async def revision(self, scope: MemoryScope, seq: int) -> Revision | None:
        return next((r for r in self._revisions.get(scope, []) if r.seq == seq), None)


def _where(scope: MemoryScope) -> ColumnElement[bool]:
    def key(column: Any, value: uuid.UUID | None) -> ColumnElement[bool]:
        condition: ColumnElement[bool] = column.is_(None) if value is None else column == value
        return condition

    return and_(
        Memory.org_id == scope.org_id,
        Memory.scope == scope.target.value,
        key(Memory.workspace_id, scope.workspace_id),
        key(Memory.agent_profile_id, scope.agent_profile_id),
        key(Memory.user_id, scope.user_id),
    )


def _entries(raw: Any) -> tuple[str, ...]:
    return tuple(str(item) for item in raw) if isinstance(raw, list) else ()


async def read_state(connection: AsyncConnection, scope: MemoryScope) -> MemoryState:
    """The current notes of the scope. With a connection that runs as a user (``as_user``),
    RLS limits the rows."""
    row = (
        await connection.execute(select(Memory.entries, Memory.revision).where(_where(scope)))
    ).one_or_none()
    return MemoryState((), 0) if row is None else MemoryState(_entries(row.entries), row.revision)


def _revision(row: Any) -> Revision:
    return Revision(
        seq=row.seq,
        action=row.action,
        entries=_entries(row.entries),
        author_kind=row.author_kind,
        author_user_id=row.author_user_id,
        session_id=row.session_id,
        run_id=row.run_id,
        reason=row.reason,
        reverted_to=row.reverted_to,
        created_at=row.created_at,
    )


_REVISION_COLUMNS = (
    MemoryRevision.seq,
    MemoryRevision.action,
    MemoryRevision.entries,
    MemoryRevision.author_kind,
    MemoryRevision.author_user_id,
    MemoryRevision.session_id,
    MemoryRevision.run_id,
    MemoryRevision.reason,
    MemoryRevision.reverted_to,
    MemoryRevision.created_at,
)


def _of_scope(scope: MemoryScope) -> Any:
    return select(Memory.id).where(_where(scope)).scalar_subquery()


async def read_revisions(
    connection: AsyncConnection, scope: MemoryScope, limit: int, *, seq: int | None = None
) -> list[Revision]:
    """The revisions of the scope, newest first. With ``seq``, only that revision."""
    query = select(*_REVISION_COLUMNS).where(
        MemoryRevision.org_id == scope.org_id, MemoryRevision.memory_id == _of_scope(scope)
    )
    if seq is not None:
        query = query.where(MemoryRevision.seq == seq)
    rows = await connection.execute(query.order_by(MemoryRevision.seq.desc()).limit(limit))
    return [_revision(row) for row in rows]


class PostgresMemoryStore:
    """``memories`` and ``memory_revisions``, written as the table owner."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def context(self, claims: RunTokenClaims) -> RunContext:
        from sama.db.models import Run

        org_id, workspace_id = uuid.UUID(claims.org_id), uuid.UUID(claims.workspace_id)
        async with self._engine.connect() as connection:
            row = (
                await connection.execute(
                    select(
                        Run.agent_profile_id,
                        Run.triggered_by_user_id,
                        Run.unattended,
                        Run.kind,
                        Session.audience,
                        Session.owner_user_id,
                        Session.lineage,
                    )
                    .join(Session, and_(Session.org_id == Run.org_id, Session.id == Run.session_id))
                    .where(
                        Run.org_id == org_id,
                        Run.id == uuid.UUID(claims.run_id),
                        Run.session_id == uuid.UUID(claims.session_id),
                        Session.workspace_id == workspace_id,
                    )
                )
            ).one_or_none()
            if row is None:
                raise MemoryChangeError("the run does not exist")
            user = run_user(row.triggered_by_user_id, row.audience, row.owner_user_id)
            admin = False
            if user is not None:
                admin = (await load_grants(connection, org_id, user)).admins_workspace(workspace_id)
        return RunContext(
            agent_profile_id=row.agent_profile_id,
            acting_user_id=user,
            workspace_admin=admin,
            unattended=bool(row.unattended),
            delegated=row.lineage == "delegation",
            review=row.kind == "review",
        )

    async def read(self, scope: MemoryScope) -> MemoryState:
        async with self._engine.connect() as connection:
            return await read_state(connection, scope)

    async def change(
        self,
        scope: MemoryScope,
        change: Change,
        *,
        writer: Writer,
        action: RevisionAction,
        reason: str | None = None,
        reverted_to: int | None = None,
        after: AfterChange | None = None,
    ) -> tuple[MemoryState, bool]:
        async with self._engine.begin() as connection:
            await connection.execute(
                pg_insert(Memory)
                .values(
                    org_id=scope.org_id,
                    scope=scope.target.value,
                    workspace_id=scope.workspace_id,
                    agent_profile_id=scope.agent_profile_id,
                    user_id=scope.user_id,
                    entries=[],
                )
                .on_conflict_do_nothing()
            )
            row = (
                await connection.execute(
                    select(Memory.id, Memory.entries, Memory.revision)
                    .where(_where(scope))
                    .with_for_update()
                )
            ).one()
            old = _entries(row.entries)
            new = tuple(change(list(old)))
            if new == old:
                state = MemoryState(old, row.revision)
                if after is not None:
                    await after(connection, state)
                return state, False
            seq = row.revision + 1
            await connection.execute(
                update(Memory)
                .where(Memory.org_id == scope.org_id, Memory.id == row.id)
                .values(entries=list(new), revision=seq, updated_at=func.now())
            )
            await connection.execute(
                insert(MemoryRevision).values(
                    org_id=scope.org_id,
                    memory_id=row.id,
                    seq=seq,
                    action=action,
                    entries=list(new),
                    session_id=writer.session_id,
                    run_id=writer.run_id,
                    author_kind=writer.kind,
                    author_user_id=writer.user_id,
                    reason=reason,
                    reverted_to=reverted_to,
                )
            )
            state = MemoryState(new, seq)
            if after is not None:
                await after(connection, state)
        return state, True

    async def revisions(self, scope: MemoryScope, limit: int) -> list[Revision]:
        async with self._engine.connect() as connection:
            return await read_revisions(connection, scope, limit)

    async def revision(self, scope: MemoryScope, seq: int) -> Revision | None:
        async with self._engine.connect() as connection:
            found = await read_revisions(connection, scope, 1, seq=seq)
        return found[0] if found else None


async def load_snapshot(
    connection: AsyncConnection,
    *,
    org_id: uuid.UUID,
    workspace_id: uuid.UUID,
    agent_profile_id: uuid.UUID,
    user_id: uuid.UUID | None,
) -> str:
    """The memory text for the frozen prefix of a new session (Section 6.1). ``user_id`` is
    the owner of a private session. A shared session has no user profile."""
    scopes = [
        MemoryScope(org_id, Target.AGENT, workspace_id, agent_profile_id),
        MemoryScope(org_id, Target.WORKSPACE, workspace_id),
    ]
    if user_id is not None:
        scopes.append(MemoryScope(org_id, Target.USER, user_id=user_id))
    notes: dict[Target, Sequence[str]] = {}
    for scope in scopes:
        notes[scope.target] = (await read_state(connection, scope)).entries
    return render_snapshot(notes)


__all__ = [
    "InProcessMemoryStore",
    "MemoryScope",
    "MemoryState",
    "MemoryStore",
    "PostgresMemoryStore",
    "Revision",
    "RunContext",
    "Writer",
    "load_snapshot",
    "read_revisions",
    "read_state",
]
