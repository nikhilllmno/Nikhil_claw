"""The session items of the OpenAI runtime in Postgres (docs/architecture.md, Section 5.3).

The runner in the sandbox has no database credential. It sends each operation on its
conversation items to the tool server (``POST /session-items``) with the run token. The tool
server calls ``PostgresSessionItems`` with the claims of the token, so a run reads and writes
only the items of its own session, and only while it runs.

``seq`` is the position of an item in its conversation, from 0 with no gaps: every operation
removes items only from the end, or removes all of them. So ``trim``, ``pop``, and ``get`` with
a limit are simple range operations. An advisory lock for each conversation puts the
operations in order.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import delete, func, insert, select, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from sama.db.models import Run, RuntimeSessionItem, Session
from sama.runtime.session_items import (
    MAX_SESSION_ITEMS,
    SessionItemsAnswer,
    SessionItemsError,
    SessionItemsRequest,
)
from sama.tenancy.authority import load_authority
from sama.tenancy.run_token import RunTokenClaims

_LOCK = text("SELECT pg_advisory_xact_lock(hashtext(:session), hashtext(:conversation))")


def _ids(claims: RunTokenClaims) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    try:
        return uuid.UUID(claims.org_id), uuid.UUID(claims.session_id), uuid.UUID(claims.run_id)
    except ValueError:
        raise SessionItemsError("the run token has no valid run") from None


class PostgresSessionItems:
    """The ``SessionItemsBackend`` of the tool server."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def authorize(self, claims: RunTokenClaims) -> None:
        """Checks that the run can use the items now, before the tool server reads a body.
        Raises ``SessionItemsError`` if it cannot. ``call`` checks again in its own
        transaction."""
        org_id, session_id, run_id = _ids(claims)
        async with self._engine.connect() as connection:
            await _check_live(connection, org_id, session_id, run_id)

    async def call(
        self, claims: RunTokenClaims, request: SessionItemsRequest
    ) -> SessionItemsAnswer:
        org_id, session_id, run_id = _ids(claims)
        async with self._engine.begin() as connection:
            await _check_live(connection, org_id, session_id, run_id)
            await connection.execute(
                _LOCK, {"session": str(session_id), "conversation": request.conversation}
            )
            return await _apply(connection, org_id, session_id, run_id, request)


async def _check_live(
    connection: AsyncConnection, org_id: uuid.UUID, session_id: uuid.UUID, run_id: uuid.UUID
) -> None:
    """The run is running, its user still sees the session (Section 5.1), and the session is
    active. This is the authority of the archive endpoint (``ContextArchive``)."""
    status = (
        await connection.execute(
            select(Run.status).where(
                Run.org_id == org_id, Run.session_id == session_id, Run.id == run_id
            )
        )
    ).scalar_one_or_none()
    if status != "running":
        raise SessionItemsError("the run is not running")
    authority = await load_authority(connection, org_id, run_id)
    if authority.problem is not None:
        raise SessionItemsError(authority.problem)
    session_status = (
        await connection.execute(
            select(Session.status).where(Session.org_id == org_id, Session.id == session_id)
        )
    ).scalar_one_or_none()
    if session_status != "active":
        raise SessionItemsError("the session is not active")


async def _apply(
    connection: AsyncConnection,
    org_id: uuid.UUID,
    session_id: uuid.UUID,
    run_id: uuid.UUID,
    request: SessionItemsRequest,
) -> SessionItemsAnswer:
    table = RuntimeSessionItem
    where = (
        table.org_id == org_id,
        table.session_id == session_id,
        table.conversation == request.conversation,
    )
    count = (await connection.execute(select(func.count()).where(*where))).scalar_one()

    async def rows(first: int) -> list[dict[str, Any]]:
        result = await connection.execute(
            select(table.item).where(*where, table.seq >= first).order_by(table.seq)
        )
        return [dict(item) for item in result.scalars()]

    async def append(items: list[dict[str, Any]], start: int) -> None:
        if not items:
            return
        await connection.execute(
            insert(table),
            [
                {
                    "org_id": org_id,
                    "session_id": session_id,
                    "conversation": request.conversation,
                    "seq": start + index,
                    "item": item,
                    "run_id": run_id,
                }
                for index, item in enumerate(items)
            ],
        )

    if request.op == "get":
        if request.limit == 0:
            return SessionItemsAnswer(count=count)
        first = 0 if request.limit is None else max(0, count - request.limit)
        return SessionItemsAnswer(items=await rows(first), count=count)
    if request.op == "add":
        if count + len(request.items) > MAX_SESSION_ITEMS:
            raise SessionItemsError("the session has too many items")
        await append(request.items, count)
        return SessionItemsAnswer(count=count + len(request.items))
    if request.op == "pop":
        if count == 0:
            return SessionItemsAnswer(count=0)
        popped = (
            await connection.execute(
                delete(table).where(*where, table.seq == count - 1).returning(table.item)
            )
        ).scalar_one()
        return SessionItemsAnswer(items=[dict(popped)], count=count - 1)
    if request.op in {"clear", "replace"}:
        await connection.execute(delete(table).where(*where))
        await append(request.items, 0)
        return SessionItemsAnswer(count=len(request.items))
    assert request.count is not None  # noqa: S101 -- the model checks it
    if request.count < count:
        await connection.execute(delete(table).where(*where, table.seq >= request.count))
    return SessionItemsAnswer(count=min(count, request.count))
