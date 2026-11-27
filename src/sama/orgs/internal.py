"""Internal orgs (docs/architecture.md, Section 18.4).

``orgs.is_internal`` marks an org of the operator team or of test traffic, so that the product
metrics leave it out. Only an operator command changes it (``python -m sama.admin internal``).
"""

from __future__ import annotations

import uuid

from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.db.models import AuditEntry, Org


class UnknownOrgError(LookupError):
    """The org does not exist. The message is safe to show."""


async def org_is_internal(engine: AsyncEngine, org_id: uuid.UUID) -> bool:
    """The flag of the org. Raises ``UnknownOrgError`` when the org does not exist. Runs as the
    owner of the database: only an operator command calls it."""
    async with engine.connect() as connection:
        value: bool | None = await connection.scalar(
            select(Org.is_internal).where(Org.id == org_id)
        )
    if value is None:
        raise UnknownOrgError("the org does not exist")
    return value


async def set_org_internal(engine: AsyncEngine, org_id: uuid.UUID, internal: bool) -> bool:
    """Sets the flag of the org and gives the value from before. A change writes the audit
    entry ``org.internal_changed``. Raises ``UnknownOrgError`` when the org does not exist."""
    async with engine.begin() as connection:
        before: bool | None = await connection.scalar(
            select(Org.is_internal).where(Org.id == org_id).with_for_update()
        )
        if before is None:
            raise UnknownOrgError("the org does not exist")
        if before != internal:
            await connection.execute(
                update(Org).where(Org.id == org_id).values(is_internal=internal)
            )
            await connection.execute(
                insert(AuditEntry).values(
                    org_id=org_id,
                    actor_kind="system",
                    action="org.internal_changed",
                    target_type="org",
                    target_id=str(org_id),
                    details={"from": before, "to": internal},
                )
            )
        return before
