"""The jobs routes of the API (M4.2)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.api import ApiSettings, JwtVerifier, SamaApi
from sama.orchestrator.queue import RunQueue
from sama.scheduler.jobs import JobService
from sama.tools.clarify import ClarifyBroker
from tests.api.conftest import ISSUER, SECRET, bearer
from tests.orchestrator.conftest import Tenant


@pytest.fixture
async def jobs_client(engine: AsyncEngine) -> AsyncIterator[httpx.AsyncClient]:
    api = SamaApi(
        engine,
        verifier=JwtVerifier(secret=SecretStr(SECRET), issuer=ISSUER),
        clarify=ClarifyBroker(wait_s=5),
        settings=ApiSettings(supabase_url="https://project.example.test", supabase_anon_key="k"),
        jobs=JobService(engine, RunQueue(engine)),
    )
    transport = httpx.ASGITransport(app=api.app())
    async with httpx.AsyncClient(transport=transport, base_url="http://sama.test") as client:
        yield client


def mia(tenant: Tenant) -> dict[str, str]:
    return bearer(tenant.ids["mia_auth"])


async def test_the_preview_shows_the_next_three_fires(jobs_client: httpx.AsyncClient) -> None:
    response = await jobs_client.post(
        "/jobs/preview",
        json={"schedule": "every weekday at 9am", "tz": "Asia/Kolkata"},
        headers=bearer(uuid.uuid4()),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["schedule"]["kind"] == "cron"
    assert len(body["next_fires"]) == 3
    bad = await jobs_client.post(
        "/jobs/preview", json={"schedule": "every 1 minute"}, headers=bearer(uuid.uuid4())
    )
    assert bad.status_code == 400


async def test_a_member_makes_and_changes_a_job(
    jobs_client: httpx.AsyncClient, tenant: Tenant
) -> None:
    base = f"/orgs/{tenant.org}/jobs"
    made = await jobs_client.post(
        base,
        json={"name": "news", "schedule": "every hour", "prompt": "Summarize the news"},
        headers=mia(tenant),
    )
    assert made.status_code == 201, made.text
    job = made.json()
    assert (job["status"], job["version"]) == ("active", 1)
    listed = await jobs_client.get(base, headers=mia(tenant))
    assert [item["id"] for item in listed.json()] == [job["id"]]

    path = f"{base}/{job['id']}"
    changed = await jobs_client.patch(path, json={"deliver": "slack:#news"}, headers=mia(tenant))
    assert changed.json()["version"] == 2
    assert changed.json()["status"] == "active", "a person who changes the job approves it"
    stale = await jobs_client.post(f"{path}/approve", json={"version": 1}, headers=mia(tenant))
    assert stale.status_code == 400
    paused = await jobs_client.post(f"{path}/pause", headers=mia(tenant))
    assert paused.json()["status"] == "paused"
    unknown = await jobs_client.post(f"{path}/explode", headers=mia(tenant))
    assert unknown.status_code == 422
    removed = await jobs_client.delete(path, headers=mia(tenant))
    assert removed.status_code == 204
    gone = await jobs_client.get(path, headers=mia(tenant))
    assert gone.status_code == 404


async def test_a_stranger_cannot_read_the_jobs(
    jobs_client: httpx.AsyncClient, tenant: Tenant
) -> None:
    response = await jobs_client.get(f"/orgs/{tenant.org}/jobs", headers=bearer(uuid.uuid4()))
    assert response.status_code in (403, 404)


async def test_jobs_are_off_without_the_service(client: httpx.AsyncClient, tenant: Tenant) -> None:
    response = await client.get(f"/orgs/{tenant.org}/jobs", headers=mia(tenant))
    assert response.status_code == 409
