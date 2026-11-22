"""The session warm route, the warm of a new session, and the wake of the event stream
(docs/architecture.md, Section 4.4)."""

from __future__ import annotations

import time
import uuid

import httpx
from fastapi import FastAPI
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.api import ApiSettings, JwtVerifier, SamaApi
from sama.orchestrator.prewarm import SessionWarmer
from sama.orchestrator.queue import RunQueue
from sama.orchestrator.signals import SessionSignals
from sama.orchestrator.worker import Worker
from sama.runtime.contract import RuntimeRegistry
from sama.runtime.fake import FakeRuntime
from tests.api.conftest import ISSUER, SECRET, bearer
from tests.api.streams import open_stream
from tests.orchestrator.conftest import Tenant, add_tenant


def mia(tenant: Tenant) -> dict[str, str]:
    return bearer(tenant.ids["mia_auth"])


class Warms:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, org_id: str, session_id: str) -> str:
        self.calls.append((org_id, session_id))
        return "ready"


def _api(engine: AsyncEngine, **extra: object) -> SamaApi:
    settings = extra.pop("settings", None) or ApiSettings(poll_s=0.02, max_stream_s=0.3)
    return SamaApi(
        engine,
        verifier=JwtVerifier(secret=SecretStr(SECRET), issuer=ISSUER),
        settings=settings,  # type: ignore[arg-type]
        **extra,  # type: ignore[arg-type]
    )


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://sama.test")


async def test_the_warm_route_starts_one_warm_and_creates_no_run(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    warms = Warms()
    warmer = SessionWarmer(warms)
    api = _api(engine, warmer=warmer)
    s1 = tenant.ids["s1"]
    async with _client(api.app()) as client:
        first = await client.post(f"/sessions/{s1}/warm", headers=mia(tenant))
        again = await client.post(f"/sessions/{s1}/warm", headers=mia(tenant))
        await warmer.wait_idle()
        runs = (await client.get(f"/sessions/{s1}", headers=mia(tenant))).json()["runs"]
    assert (first.status_code, first.json()) == (202, {"status": "warming"})
    assert (again.status_code, again.json()) == (202, {"status": "recent"})
    assert warms.calls == [(str(tenant.org), str(s1))]
    assert runs == []


async def test_the_warm_route_without_a_sandbox_answers_off(
    client: httpx.AsyncClient, tenant: Tenant
) -> None:
    response = await client.post(f"/sessions/{tenant.ids['s1']}/warm", headers=mia(tenant))
    assert (response.status_code, response.json()) == (202, {"status": "off"})


async def test_the_warm_route_needs_access_to_the_session(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    warms = Warms()
    api = _api(engine, warmer=SessionWarmer(warms))
    other = await add_tenant(engine)
    async with _client(api.app()) as client:
        foreign = await client.post(f"/sessions/{other.ids['s1']}/warm", headers=mia(tenant))
        unknown = await client.post(f"/sessions/{uuid.uuid4()}/warm", headers=mia(tenant))
        anonymous = await client.post(f"/sessions/{tenant.ids['s1']}/warm")
    assert (foreign.status_code, unknown.status_code, anonymous.status_code) == (404, 404, 401)
    assert warms.calls == []


async def test_the_warm_route_has_a_rate_limit_for_each_user(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    warmer = SessionWarmer(Warms(), per_user=1)
    api = _api(engine, warmer=warmer)
    async with _client(api.app()) as client:
        ok = await client.post(f"/sessions/{tenant.ids['s1']}/warm", headers=mia(tenant))
        limited = await client.post(f"/sessions/{tenant.ids['s2']}/warm", headers=mia(tenant))
    await warmer.wait_idle()
    assert ok.status_code == 202
    assert limited.status_code == 429
    assert int(limited.headers["Retry-After"]) >= 1


async def test_a_new_session_is_warmed_at_once(engine: AsyncEngine, tenant: Tenant) -> None:
    warms = Warms()
    warmer = SessionWarmer(warms)
    api = _api(engine, warmer=warmer)
    body = {
        "workspace_id": str(tenant.ids["ws"]),
        "agent_profile_id": str(tenant.ids["profile"]),
        "audience": "private",
    }
    async with _client(api.app()) as client:
        response = await client.post("/sessions", headers=mia(tenant), json=body)
    await warmer.wait_idle()
    assert response.status_code == 201
    assert warms.calls == [(str(tenant.org), response.json()["id"])]


async def test_a_rate_limited_warm_does_not_stop_the_new_session(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    warmer = SessionWarmer(Warms(), per_user=0)
    api = _api(engine, warmer=warmer)
    body = {"workspace_id": str(tenant.ids["ws"]), "agent_profile_id": str(tenant.ids["profile"])}
    async with _client(api.app()) as client:
        response = await client.post("/sessions", headers=mia(tenant), json=body)
    assert response.status_code == 201


async def test_a_store_of_the_worker_wakes_the_stream_before_its_poll(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    signals = SessionSignals()
    settings = ApiSettings(poll_s=30, heartbeat_s=30, max_stream_s=60)
    api = _api(engine, settings=settings, signals=signals)
    queue = RunQueue(engine)
    registry = RuntimeRegistry()
    registry.register(FakeRuntime())
    worker = Worker(queue, registry, owner="w1", flush_s=0.0, signals=signals)
    s1 = tenant.ids["s1"]
    async with open_stream(api.app(), f"/sessions/{s1}/events?after=0", mia(tenant)) as stream:
        await stream.wait_for(lambda events: any(e[0] == "runs" for e in events), what="runs")
        started = time.monotonic()
        await queue.enqueue(org_id=tenant.org, session_id=s1, text="hi", user_id=tenant.ids["mia"])
        assert await worker.run_once()
        events = await stream.wait_for(
            lambda events: any(
                e[0] == "run_event" and e[1]["type"] == "run_completed" for e in events
            ),
            what="the run_completed event",
        )
    # Without the signal, the stream reads again only after its poll of 30 s.
    assert time.monotonic() - started < 10
    kinds = [e[1]["type"] for e in events if e[0] == "run_event"]
    assert kinds[0] == "run_phase"
    assert kinds[-1] == "run_completed"
