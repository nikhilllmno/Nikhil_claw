"""The database round trips of the hot paths (docs/architecture.md, Section 4.4).

One round trip takes about 58 ms from the control plane on Fly to the database. These tests
count the statements and the transaction commands of each path and fail when a path needs more
than its limit, so a new query on a hot path is a decision and not an accident. Raise a limit
only with a reason in the pull request.

Before the change (M8.2 follow-up), the counts were: send to an idle session 18, send to a busy
session 11, approve 17, claim of a run 8, an idle claim 4, and 32 for the first run.
"""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.db.trips import count_trips
from tests.api.conftest import bearer
from tests.orchestrator.conftest import Tenant
from tests.orchestrator.test_worker import Harness, approval_script

pytestmark = pytest.mark.usefixtures("tenant")

SEND_IDLE = 9
SEND_BUSY = 9
APPROVE = 9
CLAIM = 7
CLAIM_IDLE = 1
FIRST_RUN_TO_PAUSE = 31
"""From the claim of the first run of a session to its pause for an approval, with the config
build, the events, and the pause (a worker with a fake runtime)."""


def mia(tenant: Tenant) -> dict[str, str]:
    return bearer(tenant.ids["mia_auth"])


async def test_a_message_to_an_idle_session_makes_few_round_trips(
    client: httpx.AsyncClient, tenant: Tenant
) -> None:
    with count_trips() as trips:
        response = await client.post(
            f"/sessions/{tenant.ids['s1']}/messages", headers=mia(tenant), json={"text": "hi"}
        )
    assert response.status_code == 202
    assert response.json()["queued"] is False
    assert trips.total <= SEND_IDLE, trips.show()


async def test_a_message_to_a_busy_session_makes_few_round_trips(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    harness = Harness(engine)
    await harness.send(tenant, "s1", "first")
    assert await harness.queue.claim("w9") is not None
    with count_trips() as trips:
        response = await client.post(
            f"/sessions/{tenant.ids['s1']}/messages", headers=mia(tenant), json={"text": "more"}
        )
    assert response.status_code == 202
    assert response.json()["steered"] is True
    assert trips.total <= SEND_BUSY, trips.show()


async def test_a_queued_message_behind_a_run_says_so(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    """A long text is not a note, so it queues behind the open run."""
    harness = Harness(engine)
    await harness.send(tenant, "s1", "first")
    assert await harness.queue.claim("w9") is not None
    response = await client.post(
        f"/sessions/{tenant.ids['s1']}/messages",
        headers=mia(tenant),
        json={"text": "more", "parallel": False},
    )
    assert response.status_code == 202
    again = await client.post(
        f"/sessions/{tenant.ids['s1']}/messages", headers=mia(tenant), json={"text": "x" * 5000}
    )
    assert again.status_code == 202
    body = again.json()
    assert body["steered"] is False
    assert body["queued"] is True


async def test_an_approval_makes_few_round_trips(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    harness = Harness(engine)
    harness.runtime.add_script(approval_script())
    await harness.send(tenant, "s1", "clean up")
    assert await harness.worker.run_once()
    pending = (
        await client.get(f"/sessions/{tenant.ids['s1']}/approvals", headers=mia(tenant))
    ).json()
    with count_trips() as trips:
        decided = await client.post(
            f"/approvals/{pending[0]['id']}/approve", headers=mia(tenant), json={}
        )
    assert decided.status_code == 200
    assert decided.json() == {"status": "approved", "resumed": True}
    assert trips.total <= APPROVE, trips.show()


async def test_the_claim_of_a_run_makes_few_round_trips(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    harness = Harness(engine)
    await harness.send(tenant, "s1", "first")
    with count_trips() as trips:
        claimed = await harness.queue.claim("w9", peek=False)
    assert claimed is not None
    assert trips.total <= CLAIM, trips.show()


async def test_an_idle_claim_is_one_round_trip(engine: AsyncEngine, tenant: Tenant) -> None:
    harness = Harness(engine)
    with count_trips() as trips:
        assert await harness.queue.claim("w9") is None
    assert trips.total <= CLAIM_IDLE, trips.show()


async def test_the_first_run_of_a_session_makes_few_round_trips(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    harness = Harness(engine)
    harness.runtime.add_script(approval_script())
    await harness.send(tenant, "s1", "clean up")
    with count_trips() as trips:
        assert await harness.worker.run_once()
    assert trips.total <= FIRST_RUN_TO_PAUSE, trips.show()
