"""Deletes per session set, per user, and per org (docs/architecture.md, Section 17.3).

The control plane deletes as the table owner, in one transaction for each delete:

- A session delete also deletes the ``compaction`` and ``delegation`` children of the session.
  A ``branch`` or ``retry`` child stays and becomes a root session. The rows of the sessions
  cascade: runs, messages, events, files, and the other session rows.
- A row that stays and points to a deleted row with a key that has no cascade gets a null in
  that column. ``no_action_refs`` finds these columns in the models, so a new table is safe.
- A running run blocks a delete. The delete asks the run to stop and raises
  ``DataBusyError``. The caller tries again later. A queued or paused run is deleted.
- A user delete also ends the open runs that the user started in shared sessions: a queued or
  paused run is cancelled, and a running run blocks the delete. A run with no user has no
  user limits, so a run of a deleted user must not go on.
- The delete writes the cleanups outside Postgres to ``data_purges`` in the same transaction:
  the sandboxes of the deleted sessions and the Langfuse traces of the deleted runs. A trace
  purge waits ``TRACE_DELAY``, so a trace that the exporter sends now is in Langfuse first.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from functools import cache
from typing import Any, Final

from sqlalchemy import Table, bindparam, delete, func, insert, select, text, update
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection

from sama.db.models import (
    ENDED_RUN_STATUSES,
    AuditEntry,
    Base,
    Connection,
    DataPurge,
    Membership,
    Org,
    Run,
    Session,
    Task,
    Trigger,
    User,
    VaultSecret,
    WorkflowInstance,
)
from sama.observability.langfuse import MAX_DELETE_IDS
from sama.vault.store import connection_secret, trigger_secret

CHILD_LINEAGES: Final = ("compaction", "delegation")
"""A child session of these lineages is part of its parent and goes with it."""
LOCK_TIMEOUT: Final = "5s"
"""A delete waits at most this long for a row lock. Then it raises ``DataBusyError``."""
TRACE_DELAY: Final = timedelta(hours=1)
"""A trace purge waits this long. The trace exporter marks a run as sent, then sends the trace
(a 10 s request), and Langfuse stores what it gets some time later. The wait lets a trace that
is on its way arrive before the delete."""
_BUSY_SQLSTATES: Final = frozenset({"55P03", "40P01"})
"""``lock_not_available`` and ``deadlock_detected``."""
UUIDS: Final = ARRAY(PG_UUID(as_uuid=True))
"""The Postgres type of a bound list of ids."""
_CANCELLED: Final = {
    "status": "cancelled",
    "failure_code": "cancelled",
    "failure_message": "cancelled: the user was deleted",
}


class DataError(Exception):
    """A delete that sama refuses. The message is safe to show."""

    status = 400


class DataNotFoundError(DataError):
    """The org or the user does not exist."""

    status = 404


class DataBusyError(DataError):
    """Runs or other writers use the data now. Try again later."""

    status = 409


class LastOwnerError(DataError):
    """The user is the last owner of the org. Delete the org, or make another owner first."""

    status = 409


LAST_OWNER: Final = "the last owner cannot leave; delete the org instead"


@dataclass(slots=True)
class Removed:
    """What a delete removed."""

    sessions: list[uuid.UUID] = field(default_factory=list)
    runs: int = 0
    trace_ids: list[str] = field(default_factory=list)
    """The Langfuse trace ids of the deleted runs that the exporter sent."""
    running: list[uuid.UUID] = field(default_factory=list)
    """The running runs that block the delete."""


@cache
def no_action_refs(target: str) -> tuple[tuple[Table, str, bool], ...]:
    """The columns that point to ``<target>.id`` with a key that has no ``ON DELETE`` rule.

    Each item is ``(table, column, own_session)``. ``own_session`` is True when the row also
    has a ``session_id`` that cascades from ``sessions``, so the row goes with a deleted
    session. The self keys of ``sessions`` are not in the list: ``_detach`` handles them."""
    refs: list[tuple[Table, str, bool]] = []
    for table in Base.metadata.sorted_tables:
        if table.name == "sessions" and target in ("sessions", "runs"):
            continue
        own_session = any(
            key.referred_table.name == "sessions"
            and key.ondelete == "CASCADE"
            and "session_id" in key.column_keys
            for key in table.foreign_key_constraints
        )
        for key in table.foreign_key_constraints:
            if key.referred_table.name != target or key.ondelete is not None:
                continue
            for element in key.elements:
                if element.column.name == "id":
                    column = element.parent.name
                    refs.append((table, column, own_session and column != "session_id"))
    return tuple(refs)


def busy(error: DBAPIError) -> bool:
    """True when a lock wait ended the statement."""
    return getattr(error.orig, "sqlstate", None) in _BUSY_SQLSTATES


async def lock_timeout(connection: AsyncConnection) -> None:
    await connection.execute(text(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'"))


async def session_closure(
    connection: AsyncConnection, org_id: uuid.UUID, session_ids: Sequence[uuid.UUID]
) -> list[uuid.UUID]:
    """The sessions and their ``compaction`` and ``delegation`` children, at any depth."""
    if not session_ids:
        return []
    rows = await connection.execute(
        text(
            "WITH RECURSIVE closure(id) AS ("
            " SELECT id FROM sessions WHERE org_id = :org AND id = ANY(:ids)"
            " UNION"
            " SELECT s.id FROM sessions s JOIN closure c ON s.parent_session_id = c.id"
            " WHERE s.org_id = :org AND s.lineage = ANY(:lineages)"
            ") SELECT id FROM closure"
        ).bindparams(
            bindparam("ids", type_=UUIDS),
            bindparam("lineages", type_=ARRAY(Base.metadata.tables["sessions"].c.lineage.type)),
        ),
        {"org": org_id, "ids": list(session_ids), "lineages": list(CHILD_LINEAGES)},
    )
    return [row.id for row in rows]


async def request_cancel(
    connection: AsyncConnection, org_id: uuid.UUID, run_ids: Sequence[uuid.UUID]
) -> None:
    """Asks running runs to stop, like ``RunQueue.cancel``. The worker stops each run at its
    next heartbeat."""
    if run_ids:
        await connection.execute(
            update(Run)
            .where(
                Run.org_id == org_id,
                Run.id.in_(run_ids),
                Run.status == "running",
                Run.cancel_requested_at.is_(None),
            )
            .values(cancel_requested_at=text("now()"))
        )


async def null_refs(
    connection: AsyncConnection,
    org_id: uuid.UUID,
    target: str,
    ids: Sequence[Any],
    *,
    sessions: Sequence[uuid.UUID] = (),
) -> None:
    """Sets a null in each column that points to a deleted row of ``target``. A row that goes
    with one of ``sessions`` keeps its value."""
    if not ids:
        return
    for table, column, own_session in no_action_refs(target):
        statement = (
            update(table)
            .where(table.c.org_id == org_id, table.c[column].in_(list(ids)))
            .values({column: None})
        )
        if own_session and sessions:
            statement = statement.where(table.c.session_id.not_in(list(sessions)))
        await connection.execute(statement)


async def _detach(
    connection: AsyncConnection, org_id: uuid.UUID, session_ids: Sequence[uuid.UUID]
) -> None:
    """A ``branch`` or ``retry`` child of a deleted session becomes a root session."""
    await connection.execute(
        update(Session)
        .where(
            Session.org_id == org_id,
            Session.parent_session_id.in_(list(session_ids)),
            Session.id.not_in(list(session_ids)),
        )
        .values(parent_session_id=None, parent_run_id=None, lineage=None)
    )


async def delete_sessions(
    connection: AsyncConnection, org_id: uuid.UUID, session_ids: Sequence[uuid.UUID]
) -> Removed:
    """Deletes the sessions and their children in the open transaction of ``connection``.

    If a run of these sessions is running, nothing changes and ``Removed.running`` names the
    runs. The caller rolls back and asks them to stop. Writes the ``data_purges`` rows."""
    removed = Removed()
    closure = await session_closure(connection, org_id, session_ids)
    if not closure:
        return removed
    # The session locks come first: a new run of a locked session waits, then fails.
    await connection.execute(
        select(Session.id)
        .where(Session.org_id == org_id, Session.id.in_(closure))
        .with_for_update()
    )
    runs = (
        await connection.execute(
            select(Run.id, Run.status, Run.trace_exported_at)
            .where(Run.org_id == org_id, Run.session_id.in_(closure))
            .with_for_update()
        )
    ).all()
    removed.running = [run.id for run in runs if run.status == "running"]
    if removed.running:
        return removed
    run_ids = [run.id for run in runs]
    await null_refs(connection, org_id, "runs", run_ids, sessions=closure)
    await null_refs(connection, org_id, "sessions", closure, sessions=closure)
    await _detach(connection, org_id, closure)
    await connection.execute(
        delete(Session).where(Session.org_id == org_id, Session.id.in_(closure))
    )
    removed.sessions = closure
    removed.runs = len(run_ids)
    removed.trace_ids = [run.id.hex for run in runs if run.trace_exported_at is not None]
    await write_purges(connection, org_id, sessions=closure, trace_ids=removed.trace_ids)
    return removed


async def write_purges(
    connection: AsyncConnection,
    org_id: uuid.UUID,
    *,
    sessions: Sequence[uuid.UUID] = (),
    all_sandboxes: bool = False,
    trace_ids: Sequence[str] = (),
) -> None:
    """Writes the cleanups outside Postgres for the purge worker."""
    rows: list[dict[str, Any]] = []
    if all_sandboxes:
        rows.append({"org_id": org_id, "kind": "sandboxes", "refs": {"all": True}})
    elif sessions:
        refs = {"session_ids": sorted(str(session) for session in sessions)}
        rows.append({"org_id": org_id, "kind": "sandboxes", "refs": refs})
    if rows:
        await connection.execute(insert(DataPurge), rows)
    ids = sorted(set(trace_ids))
    if ids:
        await connection.execute(
            insert(DataPurge).values(next_attempt_at=func.now() + TRACE_DELAY),
            [
                {
                    "org_id": org_id,
                    "kind": "traces",
                    "refs": {"trace_ids": ids[start : start + MAX_DELETE_IDS]},
                }
                for start in range(0, len(ids), MAX_DELETE_IDS)
            ],
        )


async def owner_ids(
    connection: AsyncConnection, org_id: uuid.UUID, *, lock: bool = False
) -> set[uuid.UUID]:
    """The owners of the org. ``lock`` locks their membership rows."""
    statement = select(Membership.user_id).where(
        Membership.org_id == org_id,
        Membership.workspace_id.is_(None),
        Membership.role == "owner",
    )
    if lock:
        statement = statement.with_for_update()
    return set((await connection.execute(statement)).scalars())


async def private_sessions(
    connection: AsyncConnection, org_id: uuid.UUID, user_id: uuid.UUID
) -> list[uuid.UUID]:
    """The private sessions of the user."""
    return list(
        (
            await connection.execute(
                select(Session.id).where(
                    Session.org_id == org_id,
                    Session.owner_user_id == user_id,
                    Session.audience == "private",
                )
            )
        ).scalars()
    )


async def end_user_runs(
    connection: AsyncConnection, org_id: uuid.UUID, user_id: uuid.UUID
) -> list[uuid.UUID]:
    """Cancels the queued and paused runs that the user started, and returns the running runs.

    The rows are locked, so the queue cannot claim them now. Call after the user row is locked:
    a new run of the user waits for that lock, then fails."""
    runs = (
        await connection.execute(
            select(Run.id, Run.status)
            .where(
                Run.org_id == org_id,
                Run.triggered_by_user_id == user_id,
                Run.status.not_in(ENDED_RUN_STATUSES),
            )
            .with_for_update()
        )
    ).all()
    running = [run.id for run in runs if run.status == "running"]
    if running:
        return running
    waiting = [run.id for run in runs]
    if waiting:
        await connection.execute(
            update(Run)
            .where(Run.org_id == org_id, Run.id.in_(waiting))
            .values(**_CANCELLED, cancel_requested_at=text("now()"), finished_at=text("now()"))
        )
    return []


async def delete_user(
    connection: AsyncConnection,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    actor_user_id: uuid.UUID | None,
) -> Removed:
    """Deletes a user row of an org and the data of the user, in the open transaction.

    Deleted: the private sessions and private task cards of the user, the memberships, the
    personal memory, skills, connections, rules, chat links, jobs, triggers, and workflows
    (the keys cascade), and the vault secrets of the personal connections and the triggers.
    Kept: the shared content. Its user columns get a null. Raises ``LastOwnerError`` for the
    last owner. Returns ``Removed.running`` when a run blocks the delete."""
    await lock_timeout(connection)
    found = (
        await connection.execute(
            select(User.id).where(User.org_id == org_id, User.id == user_id).with_for_update()
        )
    ).scalar_one_or_none()
    if found is None:
        raise DataNotFoundError("the user does not exist")
    if await owner_ids(connection, org_id, lock=True) == {user_id}:
        raise LastOwnerError(LAST_OWNER)

    removed = await delete_sessions(
        connection, org_id, await private_sessions(connection, org_id, user_id)
    )
    if removed.running:
        return removed
    removed.running = await end_user_runs(connection, org_id, user_id)
    if removed.running:
        return removed

    tasks = list(
        (
            await connection.execute(
                select(Task.id).where(
                    Task.org_id == org_id,
                    Task.owner_user_id == user_id,
                    Task.audience == "private",
                )
            )
        ).scalars()
    )
    if tasks:
        await null_refs(connection, org_id, "tasks", tasks)
        await connection.execute(delete(Task).where(Task.org_id == org_id, Task.id.in_(tasks)))

    instances = list(
        (
            await connection.execute(
                select(WorkflowInstance.id).where(
                    WorkflowInstance.org_id == org_id, WorkflowInstance.owner_user_id == user_id
                )
            )
        ).scalars()
    )
    await null_refs(connection, org_id, "workflow_instances", instances)

    # The secrets of the rows that cascade from the user. The services remove the accounts
    # outside sama before this transaction (``Cleanup``).
    names = [
        str(connection_secret(row_id))
        for row_id in (
            await connection.execute(
                select(Connection.id).where(
                    Connection.org_id == org_id, Connection.owner_user_id == user_id
                )
            )
        ).scalars()
    ] + [
        str(trigger_secret(row_id))
        for row_id in (
            await connection.execute(
                select(Trigger.id).where(Trigger.org_id == org_id, Trigger.owner_user_id == user_id)
            )
        ).scalars()
    ]
    if names:
        await connection.execute(
            delete(VaultSecret).where(VaultSecret.org_id == org_id, VaultSecret.name.in_(names))
        )

    await null_refs(connection, org_id, "users", [user_id])
    await connection.execute(delete(User).where(User.org_id == org_id, User.id == user_id))
    await connection.execute(
        insert(AuditEntry).values(
            org_id=org_id,
            actor_kind="user" if actor_user_id is not None else "system",
            actor_user_id=actor_user_id,
            action="user.deleted",
            target_type="user",
            target_id=str(user_id),
            details={
                "sessions": len(removed.sessions),
                "runs": removed.runs,
                "tasks": len(tasks),
                "secrets": len(names),
            },
        )
    )
    return removed


async def delete_org(connection: AsyncConnection, org_id: uuid.UUID) -> Removed:
    """Deletes the org and every row of the org, in the open transaction. The audit log goes
    too (``sama.audit_purge``). Returns ``Removed.running`` when a run blocks the delete."""
    await lock_timeout(connection)
    # The org lock comes first: each insert of a row of the org waits for it, then fails.
    found = (
        await connection.execute(select(Org.id).where(Org.id == org_id).with_for_update())
    ).scalar_one_or_none()
    if found is None:
        raise DataNotFoundError("the org does not exist")
    removed = Removed()
    runs = (
        await connection.execute(
            select(Run.id, Run.status, Run.trace_exported_at)
            .where(Run.org_id == org_id)
            .with_for_update()
        )
    ).all()
    removed.running = [run.id for run in runs if run.status == "running"]
    if removed.running:
        return removed
    removed.sessions = list(
        (await connection.execute(select(Session.id).where(Session.org_id == org_id))).scalars()
    )
    removed.runs = len(runs)
    removed.trace_ids = [run.id.hex for run in runs if run.trace_exported_at is not None]
    await write_purges(connection, org_id, all_sandboxes=True, trace_ids=removed.trace_ids)
    await connection.execute(text("SET LOCAL sama.audit_purge = 'on'"))
    await connection.execute(delete(Org).where(Org.id == org_id))
    return removed
