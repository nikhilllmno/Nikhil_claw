"""Fixtures for the run queue and worker tests.

The queue reads all orgs, so these tests use their own database. Each test gets a new org.
After each test, the runs that are still open are cancelled, so the next test claims only
its own runs.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from typing import Any

import pytest
from sqlalchemy import delete, insert, text, update
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.pool import NullPool

from sama.db import create_engine
from sama.db.models import AgentProfile, Membership, Org, Run, Session, User, Workspace
from tests.db.conftest import migrated_database


@pytest.fixture(scope="session")
def queue_database_url() -> Iterator[str]:
    with migrated_database() as url:
        yield url


@pytest.fixture
async def engine(queue_database_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_engine(queue_database_url, poolclass=NullPool)
    yield engine
    async with engine.begin() as connection:
        await connection.execute(
            update(Run)
            .where(Run.status.in_(("queued", "running", "paused")))
            .values(
                status="cancelled",
                lease_owner=None,
                lease_expires_at=None,
                finished_at=text("now()"),
            )
        )
    await engine.dispose()


@dataclass
class Tenant:
    """One org with a workspace ``ws``, the user ``mia`` (member of ``ws``, Supabase Auth id
    ``mia_auth``), a ``fake`` profile, the shared sessions ``s1`` to ``s3``, and mia's private
    session ``mine``."""

    ids: dict[str, uuid.UUID] = field(default_factory=dict)

    @property
    def org(self) -> uuid.UUID:
        return self.ids["org"]


async def add_tenant(engine: AsyncEngine, *, budget: dict[str, Any] | None = None) -> Tenant:
    tenant = Tenant()
    ids = tenant.ids
    for label in ("org", "ws", "profile", "mia", "mia_auth", "s1", "s2", "s3", "mine"):
        ids[label] = uuid.uuid4()
    org_id = ids["org"]
    rows: list[tuple[Any, dict[str, Any]]] = [
        (Org, {"id": org_id, "name": "Q", "slug": f"q-{org_id.hex[:8]}"}),
        (Workspace, {"id": ids["ws"], "org_id": org_id, "name": "ws", "slug": "ws"}),
        (
            User,
            {
                "id": ids["mia"],
                "org_id": org_id,
                "auth_user_id": ids["mia_auth"],
                "email": "mia@example.test",
                "display_name": "mia",
            },
        ),
        (Membership, {"org_id": org_id, "user_id": ids["mia"], "role": "member"}),
        (
            Membership,
            {"org_id": org_id, "user_id": ids["mia"], "workspace_id": ids["ws"], "role": "member"},
        ),
        (
            AgentProfile,
            {
                "id": ids["profile"],
                "org_id": org_id,
                "name": "p",
                "runtime": "fake",
                "model": "fake-model",
                "soul": "You are a test agent.",
                "toolsets": ["core", "todo", "web", "clarify"],
                "budget": budget or {},
            },
        ),
    ]
    for session in ("s1", "s2", "s3", "mine"):
        private = session == "mine"
        rows.append(
            (
                Session,
                {
                    "id": ids[session],
                    "org_id": org_id,
                    "workspace_id": ids["ws"],
                    "agent_profile_id": ids["profile"],
                    "audience": "private" if private else "shared",
                    "owner_user_id": ids["mia"] if private else None,
                },
            )
        )
    async with engine.begin() as connection:
        for model, values in rows:
            await connection.execute(insert(model).values(**values))
    return tenant


@pytest.fixture
async def tenant(engine: AsyncEngine) -> AsyncIterator[Tenant]:
    tenant = await add_tenant(engine)
    yield tenant
    async with engine.begin() as connection:
        await connection.execute(text("SET LOCAL sama.audit_purge = 'on'"))
        await connection.execute(delete(Org).where(Org.id == tenant.org))
