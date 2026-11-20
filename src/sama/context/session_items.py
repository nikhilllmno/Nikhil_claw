"""The session items of the OpenAI runtime in Postgres (docs/architecture.md, Section 5.3).

The runner in the sandbox has no database credential. It sends each operation on its
conversation items to the tool server (``POST /session-items``) with the run token. The tool
server calls ``PostgresSessionItems`` with the claims of the token, so a run reads and writes
only the items of its own session, and only while it runs.

``seq`` is the position of an item in its conversation, from 0 with no gaps: every operation
removes items only from the end, or removes all of them. So ``trim``, ``pop``, and ``get`` with
a limit are simple range operations. An advisory lock for each conversation puts the
operations in order.

``call`` takes the advisory lock first, and then checks the run with a ``FOR SHARE`` lock on
the run row. A cancel (an ``UPDATE`` of the run row) waits for an operation that checked the
run, and an operation that waits for the advisory lock sees the cancel when it gets the lock.
So no operation commits after the cancel of its run.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import Text, cast, delete, func, insert, select, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from sama.db.models import Run, RuntimeSessionItem, Session
from sama.runtime.session_items import (
    MAX_SESSION_ITEMS,
    SessionItemsAnswer,
    SessionItemsError,
    SessionItemsRequest,
    check_size,
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
            # The lock first: the check must see a cancel that came while the operation waited.
            await connection.execute(
                _LOCK, {"session": str(session_id), "conversation": request.conversation}
            )
            await _check_live(connection, org_id, session_id, run_id, lock=True)
            return await _apply(connection, org_id, session_id, run_id, request)


async def _check_live(
    connection: AsyncConnection,
    org_id: uuid.UUID,
    session_id: uuid.UUID,
    run_id: uuid.UUID,
    *,
    lock: bool = False,
) -> None:
    """The run is running, its user still sees the session (Section 5.1), and the session is
    active. This is the authority of the archive endpoint (``ContextArchive``). With ``lock``,
    the run row stays locked ``FOR SHARE`` to the end of the transaction, so the run cannot
    change its status before the operation commits."""
    query = select(Run.status).where(
        Run.org_id == org_id, Run.session_id == session_id, Run.id == run_id
    )
    if lock:
        query = query.with_for_update(read=True)
    status = (await connection.execute(query)).scalar_one_or_none()
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

    async def stored_bytes() -> int:
        size = func.coalesce(func.sum(func.octet_length(cast(table.item, Text))), 0)
        return int((await connection.execute(select(size).where(*where))).scalar_one())

    # The check before a write uses the JSON text of Python. The ``jsonb`` text can be longer
    # (``1e308`` has 309 digits in it), so the check after the write uses the stored text. An
    # error rolls back the transaction.

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
        check_size(await stored_bytes(), request.items)
        await append(request.items, count)
        check_size(await stored_bytes(), [])
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
        check_size(0, request.items)
        await connection.execute(delete(table).where(*where))
        await append(request.items, 0)
        check_size(await stored_bytes(), [])
        return SessionItemsAnswer(count=len(request.items))
    assert request.count is not None  # noqa: S101 -- the model checks it
    if request.count < count:
        await connection.execute(delete(table).where(*where, table.seq >= request.count))
    return SessionItemsAnswer(count=min(count, request.count))
