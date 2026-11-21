"""The persistent browser context of each user (docs/architecture.md, Section 14.1).

A browser context keeps the cookies and the local storage of a user from run to run, so a
login stays. ``browser_contexts`` maps ``(org, user, target)`` to the id of the context at the
provider (a Browserbase context id for ``cloud``). The row holds no cookie and no secret: the
provider keeps the context, and only the API key of sama can open it.
"""

from __future__ import annotations

import uuid
from typing import Protocol

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.browser.target import TargetName
from sama.db.models import PersistentBrowserContext


class BrowserContextStore(Protocol):
    async def get(self, org_id: str, user_id: str, target: TargetName) -> str | None:
        """The provider id of the context of the user, or ``None``. It marks the context as
        used."""
        ...

    async def claim(
        self, org_id: str, user_id: str, target: TargetName, provider_context_id: str
    ) -> str:
        """Stores the context if the user has none. Returns the stored id: the id of another
        call that stored one first wins."""
        ...

    async def forget(
        self, org_id: str, user_id: str, target: TargetName, provider_context_id: str
    ) -> None:
        """Removes the row if it still has this id (the provider lost the context)."""
        ...


class MemoryContextStore:
    """For tests and the tool server without a database."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str, str], str] = {}

    async def get(self, org_id: str, user_id: str, target: TargetName) -> str | None:
        return self.rows.get((org_id, user_id, target))

    async def claim(
        self, org_id: str, user_id: str, target: TargetName, provider_context_id: str
    ) -> str:
        return self.rows.setdefault((org_id, user_id, target), provider_context_id)

    async def forget(
        self, org_id: str, user_id: str, target: TargetName, provider_context_id: str
    ) -> None:
        if self.rows.get((org_id, user_id, target)) == provider_context_id:
            del self.rows[(org_id, user_id, target)]


class PostgresContextStore:
    """Every statement has the org in its ``WHERE`` clause."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def get(self, org_id: str, user_id: str, target: TargetName) -> str | None:
        table = PersistentBrowserContext
        async with self._engine.begin() as connection:
            row = await connection.execute(
                update(table)
                .where(
                    table.org_id == uuid.UUID(org_id),
                    table.user_id == uuid.UUID(user_id),
                    table.target == target,
                )
                .values(last_used_at=func.now())
                .returning(table.provider_context_id)
            )
            value = row.scalar_one_or_none()
        return str(value) if value is not None else None

    async def claim(
        self, org_id: str, user_id: str, target: TargetName, provider_context_id: str
    ) -> str:
        table = PersistentBrowserContext
        async with self._engine.begin() as connection:
            await connection.execute(
                insert(table)
                .values(
                    org_id=uuid.UUID(org_id),
                    user_id=uuid.UUID(user_id),
                    target=target,
                    provider_context_id=provider_context_id,
                )
                .on_conflict_do_nothing(index_elements=["org_id", "user_id", "target"])
            )
            stored = await connection.execute(
                select(table.provider_context_id).where(
                    table.org_id == uuid.UUID(org_id),
                    table.user_id == uuid.UUID(user_id),
                    table.target == target,
                )
            )
            return str(stored.scalar_one())

    async def forget(
        self, org_id: str, user_id: str, target: TargetName, provider_context_id: str
    ) -> None:
        table = PersistentBrowserContext
        async with self._engine.begin() as connection:
            await connection.execute(
                delete(table).where(
                    table.org_id == uuid.UUID(org_id),
                    table.user_id == uuid.UUID(user_id),
                    table.target == target,
                    table.provider_context_id == provider_context_id,
                )
            )
