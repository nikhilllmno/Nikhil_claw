"""The Mem0 memory provider, the run-end sync, and ``memory_recall`` (docs/architecture.md,
Section 8.2; roadmap M7.5). Mem0 is a fake; the runs are in the real database."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import insert, update
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.db.models import AgentProfile, Message, Run, Session
from sama.memory.provider import (
    NOT_ON,
    Mem0Provider,
    MemoryProviderError,
    MemoryRecall,
    MemorySync,
    mem0_user,
)
from sama.orchestrator.queue import RunQueue
from sama.vault.redact import REDACTED, Redactor
from sama.vault.store import OrgKeys, SecretName
from tests.orchestrator.conftest import Tenant

KEY = "m0-test-key-0001"


@dataclass
class Mem0:
    status: int = 200
    hits: list[dict[str, Any]] = field(
        default_factory=lambda: [
            {"memory": "User is vegetarian", "created_at": "2026-11-20T10:00:00Z"},
            {"memory": "User prefers window seats"},
            {"no": "memory"},
        ]
    )
    requests: list[httpx.Request] = field(default_factory=list)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status != 200:
            return httpx.Response(self.status, json={"detail": f"bad {KEY}"})
        if request.url.path == "/v2/memories/search/":
            return httpx.Response(200, json=self.hits)
        return httpx.Response(200, json=[{"status": "PENDING"}])

    def bodies(self, path: str) -> list[dict[str, Any]]:
        return [json.loads(r.content) for r in self.requests if r.url.path == path]


def provider(api: Mem0, *, key: str | None = KEY) -> Mem0Provider:
    return Mem0Provider(
        OrgKeys(None, {SecretName.MEM0_API_KEY: None if key is None else SecretStr(key)}),
        base_url="https://mem0.test",
        http=httpx.AsyncClient(transport=httpx.MockTransport(api)),
    )


async def test_recall_searches_the_memories_of_one_user() -> None:
    api = Mem0()
    hits = await provider(api).recall("org-1", "user-1", "food", 5)
    assert [hit.text for hit in hits] == ["User is vegetarian", "User prefers window seats"]
    assert hits[0].created_at == "2026-11-20"
    [request] = api.requests
    assert request.headers["authorization"] == f"Token {KEY}"
    assert json.loads(request.content) == {
        "query": "food",
        "filters": {"user_id": "sama:org-1:user-1"},
        "top_k": 5,
    }


async def test_remember_and_forget() -> None:
    api = Mem0()
    mem0 = provider(api)
    await mem0.remember("org-1", "user-1", [("user", "I am vegetarian"), ("assistant", " ")])
    [body] = api.bodies("/v1/memories/")
    assert body["user_id"] == "sama:org-1:user-1"
    assert body["messages"] == [{"role": "user", "content": "I am vegetarian"}]
    assert body["async_mode"] is True
    await mem0.forget("org-1", ["user-1", "user-2"])
    deletes = [r for r in api.requests if r.method == "DELETE"]
    assert [r.url.params["user_id"] for r in deletes] == ["sama:org-1:user-1", "sama:org-1:user-2"]


async def test_without_a_key_forget_is_done_and_recall_fails() -> None:
    api = Mem0()
    mem0 = provider(api, key=None)
    assert not await mem0.available("org-1")
    await mem0.forget("org-1", ["user-1"])
    with pytest.raises(MemoryProviderError, match="no Mem0 key"):
        await mem0.recall("org-1", "user-1", "q", 5)
    assert api.requests == []


@pytest.mark.parametrize(
    ("status", "text"), [(401, "refused the key"), (429, "busy"), (500, "not available")]
)
async def test_errors_are_safe(status: int, text: str) -> None:
    api = Mem0(status=status)
    with pytest.raises(MemoryProviderError, match=text) as caught:
        await provider(api).recall("org-1", "user-1", "q", 5)
    assert KEY not in str(caught.value)


def test_the_mem0_user_has_the_org() -> None:
    assert mem0_user("o", "u") == "sama:o:u"


async def _completed_run(
    engine: AsyncEngine,
    tenant: Tenant,
    *,
    session: str = "mine",
    answer: str = "Noted: you are vegetarian.",
    text: str = "I am vegetarian",
    unattended: bool = False,
) -> uuid.UUID:
    ids = tenant.ids
    user = ids["mia"] if session == "mine" else None
    run_id = await RunQueue(engine).enqueue(
        org_id=tenant.org, session_id=ids[session], text=text, user_id=user
    )
    async with engine.begin() as connection:
        await connection.execute(
            update(Run).where(Run.id == run_id).values(status="completed", unattended=unattended)
        )
        await connection.execute(
            insert(Message).values(
                org_id=tenant.org,
                session_id=ids[session],
                run_id=run_id,
                role="assistant",
                content={"text": answer},
            )
        )
    return run_id


async def _provider_of_profile(engine: AsyncEngine, tenant: Tenant, value: str) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            update(AgentProfile)
            .where(AgentProfile.id == tenant.ids["profile"])
            .values(memory_provider=value)
        )


async def test_the_sync_sends_a_private_turn_of_a_mem0_profile(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    await _provider_of_profile(engine, tenant, "mem0")
    secret = "-".join(("sk", "live", "secret", "mem0", "1234567"))
    redactor = Redactor()
    redactor.add(secret)
    api = Mem0()
    sync = MemorySync(engine, provider(api), redactor=redactor)
    run_id = await _completed_run(engine, tenant, text=f"I am vegetarian, key {secret}")
    assert await sync.sync(str(tenant.org), str(run_id))
    [body] = api.bodies("/v1/memories/")
    assert body["user_id"] == mem0_user(tenant.org, tenant.ids["mia"])
    assert [m["role"] for m in body["messages"]] == ["user", "assistant"]
    assert secret not in json.dumps(body)
    assert REDACTED in body["messages"][0]["content"]
    # The hook form schedules the same work.
    sync(str(tenant.org), str(run_id))
    await sync.wait()
    assert len(api.bodies("/v1/memories/")) == 2


async def test_the_sync_sends_nothing_for_other_runs(engine: AsyncEngine, tenant: Tenant) -> None:
    api = Mem0()
    sync = MemorySync(engine, provider(api))
    # A builtin profile sends nothing.
    builtin = await _completed_run(engine, tenant)
    assert not await sync.sync(str(tenant.org), str(builtin))
    await _provider_of_profile(engine, tenant, "mem0")
    # A shared session, an unattended run, and a run that did not complete send nothing.
    shared = await _completed_run(engine, tenant, session="s1")
    unattended = await _completed_run(engine, tenant, unattended=True)
    queued = await RunQueue(engine).enqueue(
        org_id=tenant.org, session_id=tenant.ids["mine"], text="hi", user_id=tenant.ids["mia"]
    )
    for run_id in (shared, unattended, queued, uuid.uuid4()):
        assert not await sync.sync(str(tenant.org), str(run_id))
    assert api.requests == []


async def test_a_failed_sync_does_not_raise(engine: AsyncEngine, tenant: Tenant) -> None:
    await _provider_of_profile(engine, tenant, "mem0")
    sync = MemorySync(engine, provider(Mem0(status=500)))
    run_id = await _completed_run(engine, tenant)
    assert not await sync.sync(str(tenant.org), str(run_id))


async def test_recall_follows_the_profile_and_the_audience(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    api = Mem0()
    recall = MemoryRecall(engine, provider(api))
    run_id = await _completed_run(engine, tenant)
    found, text = await recall.recall(str(tenant.org), str(run_id), "food", 5)
    assert (found, text) == (False, NOT_ON)
    await _provider_of_profile(engine, tenant, "mem0")
    found, text = await recall.recall(str(tenant.org), str(run_id), "food", 5)
    assert found
    assert "User is vegetarian" in text
    assert json.loads(api.requests[-1].content)["filters"]["user_id"] == mem0_user(
        tenant.org, tenant.ids["mia"]
    )
    shared = await _completed_run(engine, tenant, session="s1")
    found, text = await recall.recall(str(tenant.org), str(shared), "food", 5)
    assert not found
    assert "private session" in text
    with pytest.raises(MemoryProviderError, match="not known"):
        await recall.recall(str(tenant.org), str(uuid.uuid4()), "food", 5)
    with pytest.raises(MemoryProviderError, match="limit"):
        await recall.recall(str(tenant.org), str(run_id), "food", 11)


async def test_the_session_of_a_run_keeps_its_owner(engine: AsyncEngine, tenant: Tenant) -> None:
    """Sanity check of the fixture: ``mine`` is the private session of mia."""
    async with engine.connect() as connection:
        row = (
            await connection.execute(
                Session.__table__.select().where(Session.id == tenant.ids["mine"])
            )
        ).one()
    assert row.audience == "private"
    assert row.owner_user_id == tenant.ids["mia"]
