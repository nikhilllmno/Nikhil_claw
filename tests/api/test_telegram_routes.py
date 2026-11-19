"""Tests of the Telegram pairing routes of the HTTP API (Section 11.3)."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.api import ApiSettings, JwtVerifier, SamaApi
from sama.db.models import AuditEntry, ChannelIdentity, PairingCode
from sama.gateway.pairing import code_hash, redeem
from tests.api.conftest import ISSUER, SECRET, bearer
from tests.api.test_app import add_user, mia
from tests.orchestrator.conftest import Tenant, add_tenant

pytestmark = pytest.mark.usefixtures("tenant")


@pytest.fixture
async def tg(engine: AsyncEngine) -> AsyncIterator[httpx.AsyncClient]:
    api = SamaApi(
        engine,
        verifier=JwtVerifier(secret=SecretStr(SECRET), issuer=ISSUER),
        settings=ApiSettings(telegram_bot_username="sama_bot"),
    )
    async with engine.begin() as connection:
        await connection.execute(delete(ChannelIdentity))
    transport = httpx.ASGITransport(app=api.app())
    async with httpx.AsyncClient(transport=transport, base_url="http://sama.test") as client:
        yield client


def new_code(tenant: Tenant, kind: str = "user") -> dict[str, str]:
    return {
        "workspace_id": str(tenant.ids["ws"]),
        "agent_profile_id": str(tenant.ids["profile"]),
        "kind": kind,
    }


async def test_pairing_is_off_without_a_bot(client: httpx.AsyncClient, tenant: Tenant) -> None:
    response = await client.post(
        "/channels/telegram/pairing-codes", headers=mia(tenant), json=new_code(tenant)
    )
    assert response.status_code == 409


async def test_a_user_makes_a_code_and_links_an_account(
    tg: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    assert (await tg.get("/config")).json()["telegram_bot_username"] == "sama_bot"
    response = await tg.post(
        "/channels/telegram/pairing-codes", headers=mia(tenant), json=new_code(tenant)
    )
    assert response.status_code == 201
    body = response.json()
    assert (body["kind"], body["bot_username"]) == ("user", "sama_bot")
    code = body["code"]
    async with engine.begin() as connection:
        stored = (
            await connection.execute(
                select(PairingCode.code_hash).where(PairingCode.org_id == tenant.org)
            )
        ).scalars()
        assert list(stored) == [code_hash(code)], "only the digest is stored"
        linked = await redeem(
            connection, channel="telegram", code=code, kind="user", external_id="555"
        )
    assert linked is not None
    assert linked.user_id == tenant.ids["mia"]

    listed = (await tg.get("/channels/telegram/identities", headers=mia(tenant))).json()
    assert [(i["org_id"], i["workspace_id"]) for i in listed] == [
        (str(tenant.org), str(tenant.ids["ws"]))
    ]
    other = await add_user(engine, tenant, workspace_member=True)
    assert (await tg.get("/channels/telegram/identities", headers=bearer(other))).json() == []
    stolen = await tg.delete(
        f"/channels/telegram/identities/{listed[0]['id']}", headers=bearer(other)
    )
    assert stolen.status_code == 404

    gone = await tg.delete(f"/channels/telegram/identities/{listed[0]['id']}", headers=mia(tenant))
    assert gone.status_code == 204
    assert (await tg.get("/channels/telegram/identities", headers=mia(tenant))).json() == []
    async with engine.connect() as connection:
        actions = (
            await connection.execute(
                select(AuditEntry.action).where(
                    AuditEntry.org_id == tenant.org, AuditEntry.target_id == "telegram:555"
                )
            )
        ).scalars()
        assert sorted(actions) == ["channel_identity.linked", "channel_identity.unlinked"]


async def test_a_code_needs_access_to_the_workspace(
    tg: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    outsider = await add_user(engine, tenant, workspace_member=False)
    response = await tg.post(
        "/channels/telegram/pairing-codes", headers=bearer(outsider), json=new_code(tenant)
    )
    assert response.status_code == 404
    other = await add_tenant(engine)
    wrong_profile = new_code(tenant) | {"agent_profile_id": str(other.ids["profile"])}
    response = await tg.post(
        "/channels/telegram/pairing-codes", headers=mia(tenant), json=wrong_profile
    )
    assert response.status_code == 404
    bad_kind = new_code(tenant, kind="admin")
    response = await tg.post("/channels/telegram/pairing-codes", headers=mia(tenant), json=bad_kind)
    assert response.status_code == 422
    missing = await tg.delete(f"/channels/telegram/identities/{uuid.uuid4()}", headers=mia(tenant))
    assert missing.status_code == 404


async def test_codes_made_at_the_same_time_keep_the_cap(
    tg: httpx.AsyncClient, tenant: Tenant
) -> None:
    responses = await asyncio.gather(
        *(
            tg.post("/channels/telegram/pairing-codes", headers=mia(tenant), json=new_code(tenant))
            for _ in range(8)
        )
    )
    assert sorted(response.status_code for response in responses) == [201] * 5 + [429] * 3


async def test_a_user_has_at_most_five_open_codes(tg: httpx.AsyncClient, tenant: Tenant) -> None:
    for kind in ("user", "group", "user", "user", "group"):
        response = await tg.post(
            "/channels/telegram/pairing-codes", headers=mia(tenant), json=new_code(tenant, kind)
        )
        assert response.status_code == 201
    response = await tg.post(
        "/channels/telegram/pairing-codes", headers=mia(tenant), json=new_code(tenant)
    )
    assert response.status_code == 429
