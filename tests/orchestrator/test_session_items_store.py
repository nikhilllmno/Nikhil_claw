"""The Postgres store of the OpenAI session items (docs/architecture.md, Section 5.3)."""

from __future__ import annotations

import asyncio
import random
import uuid
from typing import Any

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.context.session_items import PostgresSessionItems
from sama.db.models import RuntimeSessionItem, Session
from sama.runtime.session_items import SessionItemsError, SessionItemsRequest, apply
from tests.orchestrator.conftest import Tenant
from tests.orchestrator.test_compaction import chat, claims_of, running_run
from tests.orchestrator.test_worker import Harness


@pytest.fixture
def harness(engine: AsyncEngine) -> Harness:
    return Harness(engine)


def _req(op: str, conversation: str = "c-1", **fields: Any) -> SessionItemsRequest:
    return SessionItemsRequest.model_validate({"op": op, "conversation": conversation, **fields})


async def _seqs(engine: AsyncEngine, session_id: uuid.UUID, conversation: str) -> list[int]:
    async with engine.connect() as connection:
        result = await connection.execute(
            select(RuntimeSessionItem.seq)
            .where(
                RuntimeSessionItem.session_id == session_id,
                RuntimeSessionItem.conversation == conversation,
            )
            .order_by(RuntimeSessionItem.seq)
        )
        return list(result.scalars())


async def test_the_store_matches_the_reference_list(
    engine: AsyncEngine, harness: Harness, tenant: Tenant
) -> None:
    run_id = await running_run(harness, tenant)
    store = PostgresSessionItems(engine)
    claims = claims_of(tenant, run_id)
    reference: list[dict[str, Any]] = []
    rng = random.Random(7)  # noqa: S311 - a fixed test sequence
    for step in range(60):
        op = rng.choice(["add", "add", "get", "pop", "trim", "replace", "clear"])
        fields: dict[str, Any] = {}
        if op in {"add", "replace"}:
            fields["items"] = [{"n": step, "k": k} for k in range(rng.randint(0, 3))]
        elif op == "get":
            fields["limit"] = rng.choice([None, 0, 1, 2, 50])
        elif op == "trim":
            fields["count"] = rng.randint(0, 6)
        request = _req(op, **fields)
        got = await store.call(claims, request)
        assert got == apply(reference, request), (step, op)
    assert await _seqs(engine, tenant.ids["s1"], "c-1") == list(range(len(reference)))


async def test_concurrent_adds_keep_the_order_dense(
    engine: AsyncEngine, harness: Harness, tenant: Tenant
) -> None:
    run_id = await running_run(harness, tenant)
    store = PostgresSessionItems(engine)
    claims = claims_of(tenant, run_id)
    await asyncio.gather(
        *(store.call(claims, _req("add", "c-2", items=[{"n": n}])) for n in range(12))
    )
    assert await _seqs(engine, tenant.ids["s1"], "c-2") == list(range(12))
    answer = await store.call(claims, _req("get", "c-2"))
    assert sorted(item["n"] for item in answer.items) == list(range(12))
    # Another conversation of the same session has its own list.
    assert (await store.call(claims, _req("get", "c-3"))).count == 0
    async with engine.connect() as connection:
        owners = set(
            (
                await connection.execute(
                    select(RuntimeSessionItem.run_id).where(
                        RuntimeSessionItem.conversation == "c-2"
                    )
                )
            ).scalars()
        )
    assert owners == {run_id}


async def test_only_a_running_run_of_an_active_session_uses_the_items(
    engine: AsyncEngine, harness: Harness, tenant: Tenant
) -> None:
    store = PostgresSessionItems(engine)
    done = await chat(harness, tenant, "s1", "one")
    for claims in (claims_of(tenant, done), claims_of(tenant, uuid.uuid4())):
        with pytest.raises(SessionItemsError, match="not running"):
            await store.authorize(claims)
        with pytest.raises(SessionItemsError, match="not running"):
            await store.call(claims, _req("get"))
    bad = claims_of(tenant, done).model_copy(update={"run_id": "not-a-uuid"})
    with pytest.raises(SessionItemsError, match="no valid run"):
        await store.authorize(bad)
    run_id = await running_run(harness, tenant)
    live = claims_of(tenant, run_id)
    await store.authorize(live)
    # The token of a run does not open another session.
    with pytest.raises(SessionItemsError, match="not running"):
        await store.authorize(claims_of(tenant, run_id, "mine"))
    async with engine.begin() as connection:
        await connection.execute(
            update(Session).where(Session.id == tenant.ids["s1"]).values(status="archived")
        )
    with pytest.raises(SessionItemsError, match="not active"):
        await store.call(live, _req("add", items=[{"n": 1}]))
