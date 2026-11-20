"""A review through the worker, and what the API shows of it (docs/architecture.md,
Section 9.4)."""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.api import SamaApi
from sama.db.models import Message, Run
from sama.memory import PostgresMemoryStore, Target
from sama.review.apply import ReviewApplier
from sama.runtime.contract import RunCompleted, RunRequest, TextDelta, Usage
from sama.runtime.fake import FakeScript
from tests.api.conftest import bearer
from tests.api.test_app import sse
from tests.orchestrator.conftest import Tenant
from tests.orchestrator.test_worker import Harness
from tests.review.support import reviews
from tests.review.test_writes import add, start_review

pytestmark = pytest.mark.usefixtures("tenant")


async def test_the_worker_runs_the_review_after_the_tenth_turn(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    harness = Harness(engine)
    for turn in range(10):
        await harness.send(tenant, "s1", f"turn {turn}")
        assert await harness.worker.run_once()
    (queued,) = await reviews(engine, tenant)
    assert queued.status == "queued"
    harness.runtime.add_script(
        FakeScript(
            events=(
                TextDelta(text="nothing to save", usage=Usage(input_tokens=1)),
                RunCompleted(output="nothing to save", usage=Usage(input_tokens=1)),
            )
        )
    )
    assert await harness.worker.run_once()
    assert not await harness.worker.run_once()
    (done,) = await reviews(engine, tenant)
    assert (done.status, done.kind) == ("completed", "review")
    # The review wrote no chat message, and it started no second review.
    async with engine.connect() as connection:
        texts = (
            (
                await connection.execute(
                    select(Message.content["text"].as_string()).where(
                        Message.session_id == tenant.ids["s1"]
                    )
                )
            )
            .scalars()
            .all()
        )
    assert "nothing to save" not in texts
    assert len(texts) == 20
    request = harness.runtime.requests[-1]
    assert isinstance(request, RunRequest)
    assert request.config.model == "fake-review"
    assert request.config.system.startswith("You are the background reviewer")
    assert "turn 9" in request.input
    assert request.scope.user_id is None


async def test_the_api_hides_the_review_run(
    api: SamaApi, client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    review = await start_review(engine, tenant)
    token = bearer(tenant.ids["mia_auth"])
    detail = (await client.get(f"/sessions/{tenant.ids['s1']}", headers=token)).json()
    assert str(review.run.run_id) not in {run["id"] for run in detail["runs"]}
    assert len(detail["runs"]) == 1, "the chat run shows"
    stream = sse((await client.get(f"/sessions/{tenant.ids['s1']}/events", headers=token)).text)
    runs = stream[0][1]["runs"]
    assert str(review.run.run_id) not in {run["run_id"] for run in runs}
    assert len(runs) == 1


async def test_a_workspace_admin_approves_a_proposal_in_the_api(
    api: SamaApi, client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    review = await start_review(engine, tenant)
    api._reviews = ReviewApplier(engine, PostgresMemoryStore(engine), review.skills)
    await review.memory.write(review.claims, Target.WORKSPACE, [add("builds with make")], "why")
    s1 = tenant.ids["s1"]
    admin = bearer(tenant.ids["admin_auth"])
    pending = (await client.get(f"/sessions/{s1}/approvals", headers=admin)).json()
    assert [(p["tool_name"], p["run_id"]) for p in pending] == [("memory", str(review.run.run_id))]
    assert pending[0]["reason"].startswith("the background review proposes")
    assert pending[0]["arguments"]["target"] == "workspace"
    # A member sees it, and cannot decide it.
    member = bearer(tenant.ids["mia_auth"])
    refused = await client.post(f"/approvals/{pending[0]['id']}/approve", headers=member)
    assert refused.status_code == 409
    assert "workspace admin" in refused.json()["detail"]
    decided = await client.post(f"/approvals/{pending[0]['id']}/approve", headers=admin)
    assert decided.status_code == 200
    assert decided.json() == {"status": "approved", "resumed": False}
    assert await review.entries(Target.WORKSPACE) == ("builds with make",), "applied at once"
    assert (await review.proposal_rows())[0].status == "applied"
    assert (await client.get(f"/sessions/{s1}/approvals", headers=admin)).json() == []


async def test_a_rejection_in_the_api_discards_the_proposal(
    api: SamaApi, client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    review = await start_review(engine, tenant)
    api._reviews = review.applier
    await review.memory.write(review.claims, Target.AGENT, [add("no")], None)
    admin = bearer(tenant.ids["admin_auth"])
    (pending,) = (await client.get(f"/sessions/{tenant.ids['s1']}/approvals", headers=admin)).json()
    response = await client.post(f"/approvals/{pending['id']}/deny", headers=admin)
    assert response.status_code == 200
    assert (await review.proposal_rows())[0].status == "discarded"
    assert await review.entries(Target.AGENT) == ()


async def test_the_worker_applies_a_decision_that_the_api_did_not_apply(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    review = await start_review(engine, tenant)
    await review.memory.write(review.claims, Target.WORKSPACE, [add("from the sweep")], None)
    await review.decide("admin")
    assert await review.entries(Target.WORKSPACE) == ()
    harness = Harness(engine, reviews=review.applier)
    await harness.worker.maintain()
    assert await review.entries(Target.WORKSPACE) == ("from the sweep",)


async def test_a_review_run_is_a_run_row_with_a_kind(engine: AsyncEngine, tenant: Tenant) -> None:
    review = await start_review(engine, tenant)
    async with engine.connect() as connection:
        row = (await connection.execute(select(Run).where(Run.id == review.run.run_id))).one()
    assert (row.kind, row.unattended, row.triggered_by_user_id) == ("review", True, None)
