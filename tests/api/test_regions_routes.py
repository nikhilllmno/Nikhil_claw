"""Tests of the org regions in the HTTP API (docs/architecture.md, Section 17.3, M6.7)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
from sqlalchemy import delete, func, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.db.models import ChannelIdentity, Org, User
from sama.tenancy.regions import DEFAULT_REGION, Regions, set_home_regions
from tests.api.test_app import mia
from tests.integrations.support import add_bot
from tests.orchestrator.conftest import Tenant, add_tenant

EU = "https://eu.sama.test"


@pytest.fixture(autouse=True)
def two_regions() -> None:
    set_home_regions(Regions.of(DEFAULT_REGION, "https://sama.test", {"eu-central-1": EU}))


@pytest.fixture
async def other(engine: AsyncEngine, tenant: Tenant) -> AsyncIterator[Tenant]:
    """A second org in which mia (the same Supabase Auth user) is also a member."""
    other = await add_tenant(engine)
    async with engine.begin() as connection:
        await connection.execute(
            update(User)
            .where(User.id == other.ids["mia"])
            .values(auth_user_id=tenant.ids["mia_auth"])
        )
    yield other
    async with engine.begin() as connection:
        await connection.execute(text("SET LOCAL sama.audit_purge = 'on'"))
        await connection.execute(delete(Org).where(Org.id == other.org))


async def _link(engine: AsyncEngine, tenant: Tenant, mia_id: uuid.UUID) -> uuid.UUID:
    bot_id = await add_bot(engine, tenant, username=f"bot_{uuid.uuid4().hex[:8]}")
    async with engine.begin() as connection:
        identity_id = await connection.scalar(
            insert(ChannelIdentity)
            .values(
                org_id=tenant.org,
                user_id=mia_id,
                integration_id=bot_id,
                channel="telegram",
                external_id=str(uuid.uuid4().int % 10**9),
                workspace_id=tenant.ids["ws"],
                agent_profile_id=tenant.ids["profile"],
            )
            .returning(ChannelIdentity.id)
        )
    assert isinstance(identity_id, uuid.UUID)
    return identity_id


async def _move(engine: AsyncEngine, tenant: Tenant, region: str) -> None:
    async with engine.begin() as connection:
        await connection.execute(update(Org).where(Org.id == tenant.org).values(region=region))


async def test_me_shows_the_region_of_each_org(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    [org] = (await client.get("/me", headers=mia(tenant))).json()["orgs"]
    assert (org["region"], org["region_here"], org["region_url"]) == (
        DEFAULT_REGION,
        True,
        "https://sama.test",
    )
    assert [w["id"] for w in org["workspaces"]] == [str(tenant.ids["ws"])]
    assert org["agent_profiles"] != []

    await _move(engine, tenant, "eu-central-1")
    [org] = (await client.get("/me", headers=mia(tenant))).json()["orgs"]
    assert (org["id"], org["region"], org["region_here"], org["region_url"]) == (
        str(tenant.org),
        "eu-central-1",
        False,
        EU,
    )
    assert (org["workspaces"], org["agent_profiles"], org["integrations"]) == ([], [], [])


async def test_the_data_of_an_org_in_another_region_answers_421(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    messages = f"/sessions/{tenant.ids['s1']}/messages"
    assert (await client.get(messages, headers=mia(tenant))).status_code == 200
    await _move(engine, tenant, "eu-central-1")
    for response in (
        await client.get(f"/workspaces/{tenant.ids['ws']}/review-proposals", headers=mia(tenant)),
        await client.get(messages, headers=mia(tenant)),
        await client.post(messages, headers=mia(tenant), json={"text": "hi"}),
        await client.get(f"/orgs/{tenant.org}/integrations", headers=mia(tenant)),
    ):
        assert response.status_code == 421, response.text
        assert response.json() == {
            "detail": "the org is served in region eu-central-1",
            "region": "eu-central-1",
            "url": EU,
        }


async def test_lists_across_orgs_leave_out_an_org_in_another_region(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant, other: Tenant
) -> None:
    await _link(engine, tenant, tenant.ids["mia"])
    remote = await _link(engine, other, other.ids["mia"])
    sessions = await client.get("/sessions", headers=mia(tenant))
    assert {s["org_id"] for s in sessions.json()} == {str(tenant.org), str(other.org)}

    await _move(engine, other, "eu-central-1")
    sessions = await client.get("/sessions", headers=mia(tenant))
    assert sessions.status_code == 200, sessions.text
    assert {s["org_id"] for s in sessions.json()} == {str(tenant.org)}
    linked = await client.get("/channels/telegram/identities", headers=mia(tenant))
    assert linked.status_code == 200, linked.text
    assert [row["org_id"] for row in linked.json()] == [str(tenant.org)]
    # A targeted request for the org in the other region still answers 421.
    detail = await client.get(f"/sessions/{other.ids['s1']}", headers=mia(tenant))
    assert detail.status_code == 421

    # The linked account of the org in the other region is not changed here.
    unlink = await client.delete(f"/channels/telegram/identities/{remote}", headers=mia(tenant))
    assert unlink.status_code == 421, unlink.text
    async with engine.connect() as connection:
        left = await connection.scalar(
            select(func.count()).select_from(ChannelIdentity).where(ChannelIdentity.id == remote)
        )
    assert left == 1
