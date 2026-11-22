"""A review through the worker, and what the API shows of it (docs/architecture.md,
Section 9.4)."""

from __future__ import annotations

import json

import httpx
import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.api import SamaApi
from sama.db.models import Message, Run
from sama.gateway.sessions import approval_text, read_run
from sama.memory import PostgresMemoryStore, Target
from sama.review.apply import ReviewApplier
from sama.runtime.contract import RunCompleted, RunRequest, TextDelta, Usage
from sama.runtime.fake import FakeScript
from sama.skills import ManageRequest
from sama.vault.redact import default_redactor
from tests.api.conftest import bearer
from tests.api.test_app import sse
from tests.orchestrator.conftest import Tenant
from tests.orchestrator.test_worker import Harness
from tests.review.support import add_member, reviews
from tests.review.test_writes import add, start_review
from tests.skills.helpers import md

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
    # A proposal has only the scope "once". The scope "session" is refused and changes nothing.
    assert pending[0]["scopes"] == ["once"]
    session_scope = await client.post(
        f"/approvals/{pending[0]['id']}/approve", headers=admin, json={"scope": "session"}
    )
    assert session_scope.status_code == 422
    assert len((await client.get(f"/sessions/{s1}/approvals", headers=admin)).json()) == 1
    decided = await client.post(f"/approvals/{pending[0]['id']}/approve", headers=admin)
    assert decided.status_code == 200
    assert decided.json() == {"status": "approved", "scope": "once", "resumed": False}
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


# -- a proposal of a private session, decided by a person who cannot read the session ----------


async def test_an_admin_decides_a_proposal_of_a_private_session_of_another_user(
    api: SamaApi, client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    review = await start_review(engine, tenant, "mine")
    api._reviews = ReviewApplier(engine, PostgresMemoryStore(engine), review.skills)
    await review.memory.write(review.claims, Target.WORKSPACE, [add("builds with make")], "why")
    admin = bearer(tenant.ids["admin_auth"])
    mine = tenant.ids["mine"]
    # The admin cannot read the private session of mia, and sees its proposal.
    assert (await client.get(f"/sessions/{mine}", headers=admin)).status_code == 404
    assert (await client.get(f"/sessions/{mine}/approvals", headers=admin)).status_code == 404
    listed = (
        await client.get(f"/workspaces/{tenant.ids['ws']}/review-proposals", headers=admin)
    ).json()
    assert len(listed) == 1
    (proposal,) = listed
    assert (proposal["kind"], proposal["scope"]) == ("memory", "workspace")
    assert proposal["summary"].startswith("the background review proposes")
    assert proposal["proposal"]["operations"][0]["content"] == "builds with make"
    assert not {"session_id", "run_id", "messages", "transcript"} & proposal.keys()
    assert str(mine) not in str(proposal)
    # A member who does not read the session cannot decide it. The owner of the session is
    # a plain member and cannot decide it either.
    member = bearer(tenant.ids["mia_auth"])
    assert (
        await client.get(f"/workspaces/{tenant.ids['ws']}/review-proposals", headers=member)
    ).json() == []
    refused = await client.post(f"/approvals/{proposal['approval_id']}/approve", headers=member)
    assert refused.status_code == 409
    assert "workspace admin" in refused.json()["detail"]
    await add_member(engine, tenant, "outsider", workspace_role=None)
    outsider = bearer(tenant.ids["outsider_auth"])
    assert (
        await client.get(f"/workspaces/{tenant.ids['ws']}/review-proposals", headers=outsider)
    ).status_code == 404
    assert (
        await client.post(f"/approvals/{proposal['approval_id']}/approve", headers=outsider)
    ).status_code == 404
    assert await review.entries(Target.WORKSPACE) == ()
    decided = await client.post(f"/approvals/{proposal['approval_id']}/approve", headers=admin)
    assert decided.status_code == 200
    assert decided.json()["status"] == "approved"
    assert await review.entries(Target.WORKSPACE) == ("builds with make",)
    assert (await review.proposal_rows())[0].status == "applied"
    assert (await client.get(f"/sessions/{mine}", headers=admin)).status_code == 404
    assert (
        await client.get(f"/workspaces/{tenant.ids['ws']}/review-proposals", headers=admin)
    ).json() == []


async def test_a_rejection_of_a_proposal_of_a_private_session_needs_the_role(
    api: SamaApi, client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    review = await start_review(engine, tenant, "mine")
    api._reviews = review.applier
    await review.memory.write(review.claims, Target.AGENT, [add("no")], None)
    (listed,) = (
        await client.get(
            f"/workspaces/{tenant.ids['ws']}/review-proposals",
            headers=bearer(tenant.ids["admin_auth"]),
        )
    ).json()
    denied = await client.post(
        f"/approvals/{listed['approval_id']}/deny", headers=bearer(tenant.ids["admin_auth"])
    )
    assert denied.status_code == 200
    assert (await review.proposal_rows())[0].status == "discarded"


async def test_an_org_proposal_is_for_the_org_owner_only(
    api: SamaApi, client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    review = await start_review(engine, tenant, "mine")
    api._reviews = ReviewApplier(engine, PostgresMemoryStore(engine), review.skills)
    await review.skills.manage(
        review.claims,
        ManageRequest(action="create", name="policy", scope="org", content=md("policy")),
    )
    url = f"/workspaces/{tenant.ids['ws']}/review-proposals"
    admin = bearer(tenant.ids["admin_auth"])
    owner = bearer(tenant.ids["owner_auth"])
    assert (await client.get(url, headers=admin)).json() == []
    (proposal,) = (await client.get(url, headers=owner)).json()
    assert (proposal["kind"], proposal["scope"]) == ("skill", "org")
    refused = await client.post(f"/approvals/{proposal['approval_id']}/approve", headers=admin)
    assert refused.status_code == 409
    assert "org owner" in refused.json()["detail"]
    approved = await client.post(f"/approvals/{proposal['approval_id']}/approve", headers=owner)
    assert approved.status_code == 200
    assert (await review.proposal_rows())[0].status == "applied"


async def test_a_secret_that_the_redactor_learns_later_is_not_shown_again(
    api: SamaApi, client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    # No pattern matches this value, so it is stored as it is. Later the vault stores it.
    secret = "plainvalue-" + "k7Qz" * 12
    review = await start_review(engine, tenant, "mine")
    await review.memory.write(
        review.claims, Target.WORKSPACE, [add(f"the staging key is {secret} for deploys")], "why"
    )
    await review.skills.manage(
        review.claims,
        ManageRequest(
            action="create",
            name="deploy",
            scope="workspace",
            summary=f"uses {secret}",
            content=md("deploy", body=f"Use the key {secret}."),
        ),
    )
    url = f"/workspaces/{tenant.ids['ws']}/review-proposals"
    admin = bearer(tenant.ids["admin_auth"])
    owner = bearer(tenant.ids["mia_auth"])
    stored = [a.reason + json.dumps(a.arguments) for a in await review.approvals()]
    assert all(secret[:12] in text for text in stored), "the stored text has the value"
    default_redactor().add(secret)
    try:
        listed = (await client.get(url, headers=admin)).json()
        assert len(listed) == 2
        text = json.dumps(listed)
        assert secret[:12] not in text
        assert secret[-12:] not in text
        assert all("REDACTED" in item["summary"] for item in listed)
        assert all(item["summary"].startswith("the background review") for item in listed)
        # The owner of the session reads the open approvals of the session.
        seen = (await client.get(f"/sessions/{tenant.ids['mine']}/approvals", headers=owner)).json()
        assert len(seen) == 2
        assert secret[:12] not in json.dumps(seen)
        assert all("REDACTED" in item["reason"] for item in seen)
        # The card of a chat gateway is made from the same view.
        async with engine.begin() as connection:
            await connection.execute(
                update(Run)
                .where(Run.id == review.run.run_id)
                .values(
                    status="paused",
                    pending_approval_id="review:x",
                    lease_owner=None,
                    lease_expires_at=None,
                )
            )
        read = await read_run(engine, tenant.org, review.run.run_id, 0)
        assert len(read.pending) == 2
        cards = "\n".join(approval_text(row) for row in read.pending)
        assert secret[:12] not in cards
        assert "REDACTED" in cards
    finally:
        default_redactor().forget(secret)
