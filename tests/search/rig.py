"""The world of the session search tests: the tenant of the queue tests, with more rows.

``world`` adds to the tenant (org ``Q``, workspace ``ws``, user ``mia``):

- ``bob``: member of ``ws``. Owns the private session ``bobs``.
- ``cat``: member of ``ws2`` only.
- ``ada``: org admin, with no workspace row.
- ``eve``: a user row with no membership.
- ``ws2``: a second workspace with the shared session ``other_ws``.
- A second org with the shared session ``other_org``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import pytest
from sqlalchemy import delete, insert, text
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.db.models import (
    Membership,
    Message,
    Org,
    Session,
    User,
    Workspace,
)
from sama.tenancy.run_token import RunTokenClaims
from tests.orchestrator.conftest import Tenant, add_tenant


@dataclass
class World:
    tenant: Tenant
    ids: dict[str, uuid.UUID] = field(default_factory=dict)
    other: Tenant | None = None

    @property
    def org(self) -> uuid.UUID:
        return self.tenant.org

    def claims(
        self, session: str = "s3", *, user: str | None = "mia", workspace: str = "ws"
    ) -> RunTokenClaims:
        """The claims of a run in ``session``. ``user=None`` is a shared run."""
        return RunTokenClaims(
            org_id=str(self.org),
            workspace_id=str(self.ids[workspace]),
            session_id=str(self.ids[session]),
            run_id=str(uuid.uuid4()),
            user_id=None if user is None else str(self.ids[user]),
            token_id="test",
            expires_at=2_000_000_000,
        )


async def say(
    engine: AsyncEngine,
    org: uuid.UUID,
    session: uuid.UUID,
    body: str,
    *,
    role: str = "user",
) -> uuid.UUID:
    """Writes one message and gives its id."""
    async with engine.begin() as connection:
        row = await connection.execute(
            insert(Message)
            .values(org_id=org, session_id=session, role=role, content={"text": body})
            .returning(Message.id)
        )
        return row.scalar_one()


async def add_session(
    engine: AsyncEngine,
    org: uuid.UUID,
    workspace: uuid.UUID,
    profile: uuid.UUID,
    *,
    audience: str,
    owner: uuid.UUID | None = None,
    title: str | None = None,
) -> uuid.UUID:
    session_id = uuid.uuid4()
    async with engine.begin() as connection:
        await connection.execute(
            insert(Session).values(
                id=session_id,
                org_id=org,
                workspace_id=workspace,
                agent_profile_id=profile,
                audience=audience,
                owner_user_id=owner,
                title=title,
            )
        )
    return session_id


@pytest.fixture
async def world(engine: AsyncEngine, tenant: Tenant) -> AsyncIterator[World]:
    world = World(tenant, dict(tenant.ids))
    ids = world.ids
    org = tenant.org
    for label in ("bob", "cat", "ada", "eve", "ws2"):
        ids[label] = uuid.uuid4()
    rows: list[tuple[Any, dict[str, Any]]] = [
        (Workspace, {"id": ids["ws2"], "org_id": org, "name": "ws2", "slug": "ws2"}),
    ]
    for label, role, workspace in (
        ("bob", "member", "ws"),
        ("cat", "member", "ws2"),
        ("ada", "admin", None),
        ("eve", None, None),
    ):
        rows.append(
            (
                User,
                {
                    "id": ids[label],
                    "org_id": org,
                    "auth_user_id": uuid.uuid4(),
                    "email": f"{label}@example.test",
                    "display_name": label,
                },
            )
        )
        if role is None:
            continue
        rows.append((Membership, {"org_id": org, "user_id": ids[label], "role": role}))
        if workspace is not None:
            rows.append(
                (
                    Membership,
                    {
                        "org_id": org,
                        "user_id": ids[label],
                        "workspace_id": ids[workspace],
                        "role": role,
                    },
                )
            )
    async with engine.begin() as connection:
        for model, values in rows:
            await connection.execute(insert(model).values(**values))
    profile = ids["profile"]
    ids["bobs"] = await add_session(
        engine, org, ids["ws"], profile, audience="private", owner=ids["bob"], title="bob private"
    )
    ids["other_ws"] = await add_session(
        engine, org, ids["ws2"], profile, audience="shared", title="other workspace"
    )
    other = await add_tenant(engine)
    world.other = other
    ids["other_org"] = other.ids["s1"]
    yield world
    async with engine.begin() as connection:
        await connection.execute(text("SET LOCAL sama.audit_purge = 'on'"))
        await connection.execute(delete(Org).where(Org.id == other.org))
        # The ``tenant`` fixture deletes the org and the rest.
