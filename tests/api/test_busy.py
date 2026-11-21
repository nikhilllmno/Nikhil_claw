"""The busy controls of the API (M3.9): the queued state, steer, and handoff (Sections 4.3,
11.1, and 11.4)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Sequence
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.api import ApiSettings, JwtVerifier, SamaApi
from sama.db.models import ChannelIdentity, RunSteer, SessionBinding, SessionHandoff
from sama.gateway.handoff import Chat, Handoffs
from sama.gateway.sessions import Target
from sama.orchestrator.queue import RunQueue
from tests.api.conftest import ISSUER, SECRET, bearer
from tests.integrations.support import add_bot
from tests.orchestrator.conftest import Tenant

pytestmark = pytest.mark.usefixtures("tenant")

WEB_URL = "https://app.test"


def mia(tenant: Tenant) -> dict[str, str]:
    return bearer(tenant.ids["mia_auth"])


class FakeDoor:
    def __init__(self) -> None:
        self.adopted: list[tuple[Chat, Target, list[uuid.UUID], str]] = []

    async def open_chat(self, identity: Any) -> Chat:
        return Chat(f"bot:chat:{identity.external_id}", int(identity.external_id))

    async def adopt(
        self,
        chat: Chat,
        target: Target,
        run_ids: Sequence[uuid.UUID],
        text: str,
        *,
        handoff_id: int,
    ) -> None:
        self.adopted.append((chat, target, list(run_ids), text))


@pytest.fixture
def door() -> FakeDoor:
    return FakeDoor()


@pytest.fixture
async def busy_client(engine: AsyncEngine, door: FakeDoor) -> AsyncIterator[httpx.AsyncClient]:
    handoffs = Handoffs(engine, web_url=WEB_URL)

    async def find(integration_id: uuid.UUID) -> FakeDoor:
        return door

    handoffs.register("telegram", find)
    app = SamaApi(
        engine,
        verifier=JwtVerifier(secret=SecretStr(SECRET), issuer=ISSUER),
        settings=ApiSettings(
            supabase_url="https://project.example.test", supabase_anon_key="anon-key"
        ),
        handoffs=handoffs,
    ).app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://sama.test") as client:
        yield client


async def link_telegram(engine: AsyncEngine, tenant: Tenant) -> None:
    bot_id = await add_bot(engine, tenant, username=f"bot_{uuid.uuid4().hex[:8]}")
    async with engine.begin() as connection:
        await connection.execute(
            insert(ChannelIdentity).values(
                org_id=tenant.org,
                user_id=tenant.ids["mia"],
                integration_id=bot_id,
                channel="telegram",
                external_id=str(uuid.uuid4().int % 10**9),
                workspace_id=tenant.ids["ws"],
                agent_profile_id=tenant.ids["profile"],
            )
        )


# -- queued and steer --------------------------------------------------------------------------


async def test_a_message_says_when_it_waits_behind_an_open_run(
    client: httpx.AsyncClient, tenant: Tenant
) -> None:
    url = f"/sessions/{tenant.ids['s1']}/messages"
    first = await client.post(url, headers=mia(tenant), json={"text": "one"})
    assert (first.status_code, first.json()["queued"]) == (202, False)
    second = await client.post(url, headers=mia(tenant), json={"text": "two"})
    assert (second.status_code, second.json()["queued"]) == (202, True)


async def test_steer_adds_a_note_to_the_running_run(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    session = tenant.ids["s1"]
    await client.post(f"/sessions/{session}/messages", headers=mia(tenant), json={"text": "go"})
    run = await RunQueue(engine).claim("test-owner")
    assert run is not None
    response = await client.post(
        f"/sessions/{session}/steer", headers=mia(tenant), json={"text": "use metric units"}
    )
    assert response.status_code == 202
    assert response.json() == {"steered": True, "run_id": str(run.run_id), "queued": False}
    async with engine.connect() as connection:
        note = (
            await connection.execute(select(RunSteer.run_id, RunSteer.user_id, RunSteer.text))
        ).one()
    assert tuple(note) == (run.run_id, tenant.ids["mia"], "use metric units")


async def test_a_message_joins_the_running_run_by_default(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    """M7.1: in the busy mode ``steer`` (the default), a message joins the running run."""
    session = tenant.ids["s1"]
    url = f"/sessions/{session}/messages"
    await client.post(url, headers=mia(tenant), json={"text": "write the report"})
    run = await RunQueue(engine).claim("test-owner")
    assert run is not None
    joined = await client.post(url, headers=mia(tenant), json={"text": "add a chart too"})
    assert joined.status_code == 202
    assert joined.json() == {"run_id": str(run.run_id), "queued": False, "steered": True}
    notes = await client.get(f"/sessions/{session}/steers", headers=mia(tenant))
    assert notes.status_code == 200
    [note] = notes.json()
    assert (note["run_id"], note["text"], note["status"]) == (
        str(run.run_id),
        "add a chart too",
        "pending",
    )
    await RunQueue(engine).take_steers(org_id=tenant.org, run_id=run.run_id)
    [note] = (await client.get(f"/sessions/{session}/steers", headers=mia(tenant))).json()
    assert note["status"] == "delivered"
    assert note["delivered_at"] is not None


async def test_a_parallel_message_starts_a_second_run(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    session = tenant.ids["s1"]
    url = f"/sessions/{session}/messages"
    await client.post(url, headers=mia(tenant), json={"text": "slow task"})
    queue = RunQueue(engine)
    first = await queue.claim("test-owner")
    assert first is not None
    sent = await client.post(
        url, headers=mia(tenant), json={"text": "quick question", "parallel": True}
    )
    assert sent.status_code == 202
    body = sent.json()
    assert (body["queued"], body["steered"]) == (False, False)
    second = await queue.claim("test-owner")
    assert second is not None
    assert str(second.run_id) == body["run_id"]
    assert second.side
    runs = (await client.get(f"/sessions/{session}/open-runs", headers=mia(tenant))).json()
    assert [(r["number"], r["text"], r["side"]) for r in runs] == [
        (1, "slow task", False),
        (2, "quick question", True),
    ]


async def test_the_steers_and_open_runs_need_access(
    client: httpx.AsyncClient, tenant: Tenant
) -> None:
    other = bearer(uuid.uuid4())
    for path in ("steers", "open-runs"):
        response = await client.get(f"/sessions/{tenant.ids['s1']}/{path}", headers=other)
        assert response.status_code in (401, 403, 404)


async def test_steer_without_an_open_run_sends_a_message(
    client: httpx.AsyncClient, tenant: Tenant
) -> None:
    response = await client.post(
        f"/sessions/{tenant.ids['s1']}/steer", headers=mia(tenant), json={"text": "hello"}
    )
    assert response.status_code == 202
    body = response.json()
    assert (body["steered"], body["queued"]) == (False, False)
    assert uuid.UUID(body["run_id"])


async def test_steer_checks_the_text_and_the_access(
    client: httpx.AsyncClient, tenant: Tenant
) -> None:
    url = f"/sessions/{tenant.ids['s1']}/steer"
    assert (await client.post(url, headers=mia(tenant), json={"text": ""})).status_code == 422
    long = {"text": "x" * 4001}
    assert (await client.post(url, headers=mia(tenant), json=long)).status_code == 422
    stranger = await client.post(url, headers=bearer(uuid.uuid4()), json={"text": "hi"})
    assert stranger.status_code in (403, 404)


# -- handoff -----------------------------------------------------------------------------------


async def test_handoff_without_the_service_answers_409(
    client: httpx.AsyncClient, tenant: Tenant
) -> None:
    response = await client.post(
        f"/sessions/{tenant.ids['mine']}/handoff", headers=mia(tenant), json={"to": "telegram"}
    )
    assert response.status_code == 409


async def test_handoff_refuses_a_shared_session_the_web_and_an_unlinked_channel(
    busy_client: httpx.AsyncClient, tenant: Tenant
) -> None:
    def post(session: str, to: str) -> Any:
        return busy_client.post(
            f"/sessions/{tenant.ids[session]}/handoff", headers=mia(tenant), json={"to": to}
        )

    shared = await post("s1", "telegram")
    assert shared.status_code == 409
    assert "private" in shared.json()["detail"]
    web = await post("mine", "web")
    assert (web.status_code, web.json()["detail"]) == (409, "The session is in the web app now.")
    unlinked = await post("mine", "telegram")
    assert unlinked.status_code == 409
    assert unlinked.json()["detail"].startswith("Link your Telegram account")
    assert (await post("mine", "mars")).status_code == 422


async def test_handoff_moves_a_private_session_to_telegram(
    busy_client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant, door: FakeDoor
) -> None:
    await link_telegram(engine, tenant)
    session = tenant.ids["mine"]
    await busy_client.post(
        f"/sessions/{session}/messages", headers=mia(tenant), json={"text": "a long task"}
    )
    response = await busy_client.post(
        f"/sessions/{session}/handoff", headers=mia(tenant), json={"to": "telegram"}
    )
    assert response.status_code == 200
    assert response.json()["to_channel"] == "telegram"
    assert response.json()["text"].startswith("The session moved to Telegram.")
    ((chat, target, run_ids, _),) = door.adopted
    assert (target.user_id, target.audience) == (tenant.ids["mia"], "private")
    assert len(run_ids) == 1
    async with engine.connect() as connection:
        bound = (
            await connection.execute(
                select(SessionBinding.channel, SessionBinding.external_key).where(
                    SessionBinding.session_id == session
                )
            )
        ).all()
        handoff = (
            await connection.execute(
                select(SessionHandoff.from_channel, SessionHandoff.to_channel).where(
                    SessionHandoff.session_id == session
                )
            )
        ).one()
    assert [tuple(row) for row in bound] == [("telegram", chat.key)]
    assert tuple(handoff) == ("web", "telegram")
