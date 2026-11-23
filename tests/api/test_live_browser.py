"""The live browser routes of a session (Section 14.1, M8.3)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.api import ApiSettings, JwtVerifier, SamaApi
from sama.browser.service import BrowserService
from sama.orchestrator.queue import RunQueue
from sama.tenancy.run_token import RunTokenClaims
from tests.api.conftest import ISSUER, SECRET, bearer
from tests.browser.fakes import AllowAll, FakeTarget
from tests.browser.test_live_view import LiveSession
from tests.orchestrator.conftest import Tenant, engine, queue_database_url, tenant

__all__ = ["engine", "queue_database_url", "tenant"]


@pytest.fixture
async def browser() -> AsyncIterator[BrowserService]:
    service = BrowserService(FakeTarget(session_class=LiveSession), AllowAll(), control_wait_s=0.1)
    yield service
    await service.aclose()


@pytest.fixture
async def client(
    engine: AsyncEngine,
    browser: BrowserService,
) -> AsyncIterator[httpx.AsyncClient]:
    api = SamaApi(
        engine,
        verifier=JwtVerifier(secret=SecretStr(SECRET), issuer=ISSUER),
        live_browser=browser,
        settings=ApiSettings(supabase_url="https://p.test", supabase_anon_key="anon"),
    )
    transport = httpx.ASGITransport(app=api.app())
    async with httpx.AsyncClient(transport=transport, base_url="http://sama.test") as client:
        yield client


async def open_run(engine: AsyncEngine, tenant: Tenant) -> uuid.UUID:
    queue = RunQueue(engine)
    run_id = await queue.enqueue(
        org_id=tenant.org, session_id=tenant.ids["mine"], text="hi", user_id=tenant.ids["mia"]
    )
    assert await queue.claim("worker") is not None
    return run_id


def claims_of(tenant: Tenant, run_id: uuid.UUID) -> RunTokenClaims:
    return RunTokenClaims(
        org_id=str(tenant.org),
        workspace_id=str(tenant.ids["ws"]),
        session_id=str(tenant.ids["mine"]),
        run_id=str(run_id),
        user_id=str(tenant.ids["mia"]),
        token_id="t1",
        expires_at=2_000_000_000,
    )


async def test_the_owner_sees_the_live_browser_and_takes_control(
    client: httpx.AsyncClient,
    browser: BrowserService,
    engine: AsyncEngine,
    tenant: Tenant,
) -> None:
    mine = tenant.ids["mine"]
    headers = bearer(tenant.ids["mia_auth"])
    assert (await client.get(f"/sessions/{mine}/browser", headers=headers)).json() == {"live": None}
    run_id = await open_run(engine, tenant)
    assert (await client.get(f"/sessions/{mine}/browser", headers=headers)).json() == {"live": None}
    await browser.open(claims_of(tenant, run_id), "https://shop.example/")
    live = (await client.get(f"/sessions/{mine}/browser", headers=headers)).json()["live"]
    assert live == {
        "run_id": str(run_id),
        "target": "cloud",
        "control": "agent",
        "view_url": "https://www.browserbase.com/fs/x?navbar=false",
        "page_url": "https://shop.example/",
        "title": "Page https://shop.example/",
        "tabs": 1,
        "page_id": "p1",
    }
    taken = await client.post(
        f"/sessions/{mine}/browser/control",
        headers=headers,
        json={"run_id": str(run_id), "action": "take"},
    )
    assert taken.status_code == 204
    live = (await client.get(f"/sessions/{mine}/browser", headers=headers)).json()["live"]
    assert live["control"] == "user"
    back = await client.post(
        f"/sessions/{mine}/browser/control",
        headers=headers,
        json={"run_id": str(run_id), "action": "give_back"},
    )
    assert back.status_code == 204
    other = await client.post(
        f"/sessions/{mine}/browser/control",
        headers=headers,
        json={"run_id": str(uuid.uuid4()), "action": "take"},
    )
    assert other.status_code == 404


async def test_a_user_who_cannot_see_the_session_gets_nothing(
    client: httpx.AsyncClient,
    browser: BrowserService,
    engine: AsyncEngine,
    tenant: Tenant,
) -> None:
    mine = tenant.ids["mine"]
    run_id = await open_run(engine, tenant)
    await browser.open(claims_of(tenant, run_id), "https://shop.example/")
    stranger = bearer(uuid.uuid4())
    assert (await client.get(f"/sessions/{mine}/browser", headers=stranger)).status_code in (
        403,
        404,
    )
    control = await client.post(
        f"/sessions/{mine}/browser/control",
        headers=stranger,
        json={"run_id": str(run_id), "action": "take"},
    )
    assert control.status_code in (403, 404)
    assert (await client.get(f"/sessions/{mine}/browser")).status_code == 401
