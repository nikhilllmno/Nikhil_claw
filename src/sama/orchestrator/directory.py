"""The session-to-sandbox map in Postgres (docs/architecture.md, Section 16.2).

``SandboxedRuntime`` keeps a map in memory. With ``PostgresSandboxDirectory`` it also reads and
writes ``sessions.sandbox_id``, so every worker process resumes the same session sandbox.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.db.models import Session


def _ids(org_id: str, session_id: str) -> tuple[uuid.UUID, uuid.UUID] | None:
    try:
        return uuid.UUID(org_id), uuid.UUID(session_id)
    except ValueError:
        return None


class PostgresSandboxDirectory:
    """A ``SandboxDirectory`` on the ``sessions`` table. Every query is scoped by org."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def get(self, org_id: str, session_id: str) -> str | None:
        ids = _ids(org_id, session_id)
        if ids is None:
            return None
        async with self._engine.connect() as connection:
            return (
                await connection.execute(
                    select(Session.sandbox_id).where(Session.org_id == ids[0], Session.id == ids[1])
                )
            ).scalar_one_or_none()

    async def put(self, org_id: str, session_id: str, sandbox_id: str) -> None:
        ids = _ids(org_id, session_id)
        if ids is None:
            return
        async with self._engine.begin() as connection:
            await connection.execute(
                update(Session)
                .where(Session.org_id == ids[0], Session.id == ids[1])
                .values(sandbox_id=sandbox_id)
            )
