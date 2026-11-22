"""Event stream polls share a few database slots, so they cannot use the whole pool
(docs/architecture.md, Section 4.4)."""

from __future__ import annotations

import asyncio

import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.api import ApiSettings, JwtVerifier, SamaApi
from sama.api.auth import Principal
from sama.db.trips import count_trips
from tests.api.conftest import ISSUER, SECRET
from tests.orchestrator.conftest import Tenant

pytestmark = pytest.mark.usefixtures("tenant")


def make_api(engine: AsyncEngine, slots: int) -> SamaApi:
    return SamaApi(
        engine,
        verifier=JwtVerifier(secret=SecretStr(SECRET), issuer=ISSUER),
        settings=ApiSettings(
            supabase_url="https://project.example.test",
            supabase_anon_key="anon-key",
            poll_s=0.02,
            stream_db_slots=slots,
        ),
    )


async def first_event(api: SamaApi, tenant: Tenant) -> str:
    principal = Principal(auth_user_id=tenant.ids["mia_auth"], email=None)
    loop = asyncio.get_running_loop()
    stream = api.session_events(principal, tenant.ids["s1"], 0, lambda: loop.time() + 5)
    try:
        return (await anext(stream)).event
    finally:
        await stream.aclose()  # type: ignore[attr-defined]


async def test_a_poll_waits_for_a_free_slot_and_not_for_a_connection(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    api = make_api(engine, slots=1)
    await api._poll_slots.acquire()
    task = asyncio.create_task(first_event(api, tenant))
    await asyncio.sleep(0.2)
    assert not task.done(), "the poll waits while the only slot is taken"
    api._poll_slots.release()
    assert await asyncio.wait_for(task, 5) == "runs"


async def test_a_poll_of_a_session_with_few_runs_skips_the_older_run_query(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    api = make_api(engine, slots=2)
    with count_trips() as trips:
        assert await first_event(api, tenant) == "runs"
    # BEGIN, the role, the access, the events, the recent runs, COMMIT.
    assert trips.total <= 6, trips.show()
