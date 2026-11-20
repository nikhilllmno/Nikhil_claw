"""The context routes of the API: compact, compactions, pins, lineage (Section 6.3)."""

from __future__ import annotations

import uuid

import httpx
import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.context import MAX_PINS, ContextArchive
from sama.db.models import Message, Org, Run, Session
from sama.runtime.archive import ArchiveEntry, TranscriptArchive
from sama.runtime.contract import MAX_COMPACT_FOCUS_CHARS
from sama.tenancy.run_token import RunTokenClaims
from tests.api.conftest import bearer
from tests.orchestrator.conftest import Tenant
from tests.orchestrator.test_worker import Harness

pytestmark = pytest.mark.usefixtures("tenant")


def mia(tenant: Tenant) -> dict[str, str]:
    return bearer(tenant.ids["mia_auth"])


def stranger() -> dict[str, str]:
    return bearer(uuid.uuid4())


async def finished_run(engine: AsyncEngine, tenant: Tenant, session: str) -> Harness:
    harness = Harness(engine)
    await harness.send(tenant, session, "hello")
    assert await harness.worker.run_once()
    return harness


# -- compact -----------------------------------------------------------------------------------


async def test_compact_queues_a_compact_run(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    harness = await finished_run(engine, tenant, "s1")
    response = await client.post(
        f"/sessions/{tenant.ids['s1']}/compact", headers=mia(tenant), json={"focus": "the plan"}
    )
    assert response.status_code == 202
    run_id = uuid.UUID(response.json()["run_id"])
    row = await harness.run(run_id)
    assert (row.compact, row.compact_focus, row.status) == (True, "the plan", "queued")
    assert row.triggered_by_user_id == tenant.ids["mia"]


async def test_compact_needs_no_body(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    await finished_run(engine, tenant, "s1")
    response = await client.post(f"/sessions/{tenant.ids['s1']}/compact", headers=mia(tenant))
    assert response.status_code == 202


async def test_compact_follows_the_busy_rules(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    url = f"/sessions/{tenant.ids['s1']}/compact"
    nothing = await client.post(url, headers=mia(tenant))
    assert nothing.status_code == 409
    assert "nothing to compact" in nothing.json()["detail"]
    await finished_run(engine, tenant, "s1")
    assert (await client.post(url, headers=mia(tenant))).status_code == 202
    busy = await client.post(url, headers=mia(tenant))
    assert busy.status_code == 409
    assert "busy" in busy.json()["detail"]


async def test_compact_is_rate_limited(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    await finished_run(engine, tenant, "s1")
    async with engine.begin() as connection:
        await connection.execute(
            update(Org).where(Org.id == tenant.org).values(limits={"user_messages_per_minute": 1})
        )
    response = await client.post(f"/sessions/{tenant.ids['s1']}/compact", headers=mia(tenant))
    assert response.status_code == 429
    assert int(response.headers["Retry-After"]) >= 1


async def test_compact_rejects_a_long_focus_and_other_fields(
    client: httpx.AsyncClient, tenant: Tenant
) -> None:
    url = f"/sessions/{tenant.ids['s1']}/compact"
    long = await client.post(
        url, headers=mia(tenant), json={"focus": "x" * (MAX_COMPACT_FOCUS_CHARS + 1)}
    )
    assert long.status_code == 422
    extra = await client.post(url, headers=mia(tenant), json={"other": 1})
    assert extra.status_code == 422


async def test_a_user_who_cannot_see_the_session_gets_404(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    await finished_run(engine, tenant, "mine")
    for method, path in (
        ("POST", "compact"),
        ("GET", "compactions"),
        ("GET", "pins"),
        ("GET", "lineage"),
    ):
        response = await client.request(
            method, f"/sessions/{tenant.ids['mine']}/{path}", headers=stranger()
        )
        assert response.status_code == 404, path
    pin = await client.post(
        f"/sessions/{tenant.ids['mine']}/pins", headers=stranger(), json={"text": "x"}
    )
    assert pin.status_code == 404
    async with engine.connect() as connection:
        runs = (
            await connection.execute(select(Run.id).where(Run.session_id == tenant.ids["mine"]))
        ).all()
    assert len(runs) == 1


async def test_a_compaction_shows_in_the_list(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    harness = await finished_run(engine, tenant, "s1")
    await client.post(f"/sessions/{tenant.ids['s1']}/compact", headers=mia(tenant))
    assert await harness.worker.run_once()
    response = await client.get(f"/sessions/{tenant.ids['s1']}/compactions", headers=mia(tenant))
    assert response.status_code == 200
    (item,) = response.json()
    assert (item["trigger"], item["tokens_before"], item["tokens_after"]) == ("manual", 1000, 500)
    assert item["archive_session_id"] is None
    assert item["completed_at"] is not None


# -- pins --------------------------------------------------------------------------------------


async def test_pins_can_be_added_listed_and_removed(
    client: httpx.AsyncClient, tenant: Tenant
) -> None:
    url = f"/sessions/{tenant.ids['s1']}/pins"
    added = await client.post(
        url, headers=mia(tenant), json={"text": "  Use key sk-ant-api03-" + "a" * 40 + "  "}
    )
    assert added.status_code == 201
    body = added.json()
    assert body["source"] == "user"
    assert "sk-ant-api03" not in body["text"]
    listed = await client.get(url, headers=mia(tenant))
    assert [p["id"] for p in listed.json()] == [body["id"]]
    gone = await client.delete(f"{url}/{body['id']}", headers=mia(tenant))
    assert gone.status_code == 204
    assert (await client.get(url, headers=mia(tenant))).json() == []
    assert (await client.delete(f"{url}/{body['id']}", headers=mia(tenant))).status_code == 404


async def test_a_message_can_be_pinned(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    await finished_run(engine, tenant, "s1")
    async with engine.connect() as connection:
        message = (
            await connection.execute(
                select(Message.id, Message.content).where(
                    Message.session_id == tenant.ids["s1"], Message.role == "user"
                )
            )
        ).one()
    url = f"/sessions/{tenant.ids['s1']}/pins"
    response = await client.post(url, headers=mia(tenant), json={"message_id": str(message.id)})
    assert response.status_code == 201
    assert response.json()["text"] == message.content["text"]
    assert response.json()["source"] == "message"
    # A message of another session is the same as a message that does not exist.
    other_url = f"/sessions/{tenant.ids['s2']}/pins"
    other = await client.post(other_url, headers=mia(tenant), json={"message_id": str(message.id)})
    assert other.status_code == 409


@pytest.mark.parametrize(
    "body",
    [{}, {"text": "a", "message_id": str(uuid.uuid4())}, {"text": ""}, {"text": "x" * 2001}],
)
async def test_a_bad_pin_request_gets_422(
    client: httpx.AsyncClient, tenant: Tenant, body: dict[str, str]
) -> None:
    response = await client.post(
        f"/sessions/{tenant.ids['s1']}/pins", headers=mia(tenant), json=body
    )
    assert response.status_code == 422


async def test_the_pins_of_a_session_have_a_limit(
    client: httpx.AsyncClient, tenant: Tenant
) -> None:
    url = f"/sessions/{tenant.ids['s1']}/pins"
    for n in range(MAX_PINS):
        assert (
            await client.post(url, headers=mia(tenant), json={"text": f"note {n}"})
        ).status_code == 201
    full = await client.post(url, headers=mia(tenant), json={"text": "one more"})
    assert full.status_code == 409
    assert "at most" in full.json()["detail"]


# -- lineage -----------------------------------------------------------------------------------


async def make_archive(engine: AsyncEngine, tenant: Tenant, session: str) -> uuid.UUID:
    harness = Harness(engine)
    await harness.send(tenant, session, "go")
    claimed = await harness.queue.claim("w-lineage")
    assert claimed is not None
    claims = RunTokenClaims(
        org_id=str(tenant.org),
        workspace_id=str(tenant.ids["ws"]),
        session_id=str(tenant.ids[session]),
        run_id=str(claimed.run_id),
        token_id="t",
        expires_at=2_000_000_000,
    )
    await ContextArchive(engine).archive(
        claims, TranscriptArchive(trigger="auto", entries=[ArchiveEntry(role="user", text="old")])
    )
    async with engine.connect() as connection:
        return (
            await connection.execute(
                select(Session.id).where(Session.parent_session_id == tenant.ids[session])
            )
        ).scalar_one()


async def test_the_lineage_lists_the_archive_as_a_child_and_the_parent_as_an_ancestor(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    archive_id = await make_archive(engine, tenant, "s1")
    live = await client.get(f"/sessions/{tenant.ids['s1']}/lineage", headers=mia(tenant))
    assert [c["session_id"] for c in live.json()["children"]] == [str(archive_id)]
    assert live.json()["children"][0]["lineage"] == "compaction"
    assert live.json()["ancestors"] == []
    archived = await client.get(f"/sessions/{archive_id}/lineage", headers=mia(tenant))
    assert [a["session_id"] for a in archived.json()["ancestors"]] == [str(tenant.ids["s1"])]
    detail = await client.get(f"/sessions/{archive_id}", headers=mia(tenant))
    assert (detail.json()["lineage"], detail.json()["parent_session_id"]) == (
        "compaction",
        str(tenant.ids["s1"]),
    )


async def test_the_session_list_hides_archives(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    archive_id = await make_archive(engine, tenant, "s1")
    listed = await client.get("/sessions", headers=mia(tenant))
    ids = {item["id"] for item in listed.json()}
    assert str(tenant.ids["s1"]) in ids
    assert str(archive_id) not in ids


async def test_an_archive_of_a_private_session_is_private_to_its_owner(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    archive_id = await make_archive(engine, tenant, "mine")
    assert (await client.get(f"/sessions/{archive_id}", headers=mia(tenant))).status_code == 200
    assert (await client.get(f"/sessions/{archive_id}", headers=stranger())).status_code == 404
