"""Fixtures for sama as a server (docs/architecture.md, Section 10.7, M7.6). They use the
database and the tenant of the queue tests. ``mia`` is an org admin here, so she can make
keys."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.apikeys import ApiKeyStore
from sama.db.models import Membership, Run
from sama.runtime.contract import RunCompleted, RunEvent, TextDelta
from sama.runtime.fake import FakeScript
from sama.server import OpenAICompat, ServerCore, ServerSettings
from tests.orchestrator.conftest import Tenant, engine, queue_database_url, tenant  # noqa: F401
from tests.orchestrator.test_worker import Harness
from tests.waiting import async_wait_until

FAST = ServerSettings(poll_s=0.02, keepalive_s=0.05, first_wait_s=5.0, max_wait_s=20.0)


@dataclass
class Keys:
    store: ApiKeyStore
    tenant: Tenant

    async def make(
        self,
        *,
        scopes: Sequence[str] = ("chat", "mcp"),
        approvals: str = "deny",
        profile: bool = False,
        expires_in_days: int | None = None,
    ) -> str:
        _view, key = await self.store.create(
            self.tenant.org,
            actor_user_id=self.tenant.ids["mia"],
            name="test key",
            workspace_id=self.tenant.ids["ws"],
            agent_profile_id=self.tenant.ids["profile"] if profile else None,
            scopes=list(scopes),  # type: ignore[arg-type]
            approvals=approvals,  # type: ignore[arg-type]
            expires_in_days=expires_in_days,
        )
        return key


@pytest.fixture
async def admin(engine: AsyncEngine, tenant: Tenant) -> Tenant:  # noqa: F811
    async with engine.begin() as connection:
        await connection.execute(
            update(Membership)
            .where(
                Membership.org_id == tenant.org,
                Membership.user_id == tenant.ids["mia"],
                Membership.workspace_id.is_(None),
            )
            .values(role="admin")
        )
    return tenant


@pytest.fixture
def keys(engine: AsyncEngine, admin: Tenant) -> Keys:  # noqa: F811
    return Keys(ApiKeyStore(engine), admin)


@pytest.fixture
def core(engine: AsyncEngine) -> ServerCore:  # noqa: F811
    return ServerCore(engine, settings=FAST)


@pytest.fixture
def harness(engine: AsyncEngine) -> Harness:  # noqa: F811
    return Harness(engine)


@pytest.fixture
def chat_app(engine: AsyncEngine, core: ServerCore) -> FastAPI:  # noqa: F811
    app = FastAPI()
    OpenAICompat(core, ApiKeyStore(engine)).install(app)
    return app


@pytest.fixture
async def chat_client(chat_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=chat_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://sama.test", timeout=30
    ) as client:
        yield client


def reply(*texts: str, final: str | None = None) -> FakeScript:
    """A run that streams ``texts`` and ends with ``final`` (default: the joined texts)."""
    events: list[RunEvent] = [TextDelta(text=text) for text in texts]
    events.append(RunCompleted(output=final if final is not None else "".join(texts)))
    return FakeScript(events=tuple(events))


async def queued_run(engine: AsyncEngine, org_id: uuid.UUID) -> uuid.UUID:  # noqa: F811
    """Waits for a queued chat run of the org and returns its id."""

    async def find() -> uuid.UUID | None:
        async with engine.connect() as connection:
            found: uuid.UUID | None = (
                await connection.execute(
                    select(Run.id).where(
                        Run.org_id == org_id, Run.status == "queued", Run.kind == "chat"
                    )
                )
            ).scalar_one_or_none()
            return found

    run_id: uuid.UUID = await async_wait_until(find, what="a queued run")
    return run_id


async def answer(harness: Harness, org_id: uuid.UUID, script: FakeScript) -> uuid.UUID:
    """Runs the next queued run of the org with ``script``."""
    run_id = await queued_run(harness.engine, org_id)
    harness.runtime.add_script(script)
    assert await harness.worker.run_once()
    return run_id
