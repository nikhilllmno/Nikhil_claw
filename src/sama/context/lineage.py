"""Session lineage (docs/architecture.md, Sections 6.3 and 7).

A session can have a parent. The ``lineage`` says how it came from the parent:

| Lineage | Meaning |
|---|---|
| ``compaction`` | An archive: the transcript of the parent from before one compaction. |
| ``branch`` | A new session that started from the parent: ``/new`` and ``/model``. |
| ``retry`` | A session that repeats the parent (reserved). |
| ``delegation`` | The task of a sub-agent of the parent (M2.7). |

``ancestors`` and ``children`` read the links. They run on the connection that the caller
gives. A connection that runs as the user (``as_user``) shows only the sessions that RLS lets
the user read, so a link to a hidden session ends the chain. A control-plane connection must
filter by the visibility rule itself.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from sama.db.models import Session

MAX_DEPTH: Final = 20
MAX_CHILDREN: Final = 100


@dataclass(frozen=True, slots=True)
class Link:
    session_id: uuid.UUID
    parent_session_id: uuid.UUID | None
    lineage: str | None
    title: str | None
    status: str
    created_at: datetime


_COLUMNS = (
    Session.id,
    Session.parent_session_id,
    Session.lineage,
    Session.title,
    Session.status,
    Session.created_at,
)


def _link(row: object) -> Link:
    return Link(*(getattr(row, column.key) for column in _COLUMNS))


async def ancestors(
    connection: AsyncConnection, org_id: uuid.UUID, session_id: uuid.UUID
) -> list[Link]:
    """The parents of a session, nearest first. The chain ends at the first parent that the
    connection cannot read, at the root, or after ``MAX_DEPTH`` links."""
    chain: list[Link] = []
    seen = {session_id}
    current = session_id
    for _ in range(MAX_DEPTH):
        row = (
            await connection.execute(
                select(Session.parent_session_id).where(
                    Session.org_id == org_id, Session.id == current
                )
            )
        ).one_or_none()
        if row is None or row.parent_session_id is None or row.parent_session_id in seen:
            break
        parent = (
            await connection.execute(
                select(*_COLUMNS).where(
                    Session.org_id == org_id, Session.id == row.parent_session_id
                )
            )
        ).one_or_none()
        if parent is None:
            break
        chain.append(_link(parent))
        seen.add(parent.id)
        current = parent.id
    return chain


async def children(
    connection: AsyncConnection,
    org_id: uuid.UUID,
    session_id: uuid.UUID,
    *,
    lineage: str | None = None,
) -> list[Link]:
    """The sessions that have this session as their parent, oldest first."""
    statement = select(*_COLUMNS).where(
        Session.org_id == org_id, Session.parent_session_id == session_id
    )
    if lineage is not None:
        statement = statement.where(Session.lineage == lineage)
    rows = await connection.execute(
        statement.order_by(Session.created_at, Session.id).limit(MAX_CHILDREN)
    )
    return [_link(row) for row in rows]
