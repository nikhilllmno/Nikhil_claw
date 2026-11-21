"""The API and the chat cards of a change of the profile (M6.1, docs/architecture.md, Sections
9.4.2 and 15.2): the approval shows the diff, and the history lists and reverts changes."""

from __future__ import annotations

import uuid

import httpx
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.api import SamaApi
from sama.gateway.sessions import approval_text, read_run
from sama.review.change import ChangeProposer, ChangeRequest
from tests.api.conftest import bearer
from tests.orchestrator.conftest import Tenant
from tests.review.support import add_member, set_profile
from tests.review.test_change_propose import profile
from tests.review.test_chat_proposals import Chat, start_chat


def changes_path(tenant: Tenant) -> str:
    return f"/orgs/{tenant.org}/agent-profiles/{tenant.ids['profile']}/changes"


def auth(tenant: Tenant, user: str) -> dict[str, str]:
    return bearer(tenant.ids[f"{user}_auth"])


async def propose_web(chat: Chat) -> None:
    await ChangeProposer(chat.engine, chat.proposals).propose(
        chat.claims,
        ChangeRequest(
            target="toolsets", summary="add web", toolsets=["core", "memory", "skills", "web"]
        ),
    )


async def test_the_card_shows_the_diff_and_the_history_lists_the_change(
    api: SamaApi, client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    chat = await start_chat(engine, tenant)
    api._reviews = chat.applier
    await propose_web(chat)
    s1 = tenant.ids["s1"]

    (pending,) = (
        await client.get(f"/sessions/{s1}/approvals", headers=auth(tenant, "owner"))
    ).json()
    assert pending["tool_name"] == "change_propose"
    assert pending["proposal"] is True
    assert "+ web" in pending["diff"]
    assert "- self" in pending["diff"]
    read = await read_run(engine, tenant.org, chat.run.run_id, 0)
    (row,) = read.pending
    card = approval_text(row)
    assert "Change:\ntoolsets:\n+ web\n- self" in card
    assert '"toolsets"' not in card, "the diff replaces the arguments"

    decided = await client.post(
        f"/approvals/{pending['id']}/approve", headers=auth(tenant, "owner")
    )
    assert decided.status_code == 200
    (change,) = (await client.get(changes_path(tenant), headers=auth(tenant, "mia"))).json()
    assert (change["kind"], change["source"], change["actor_name"]) == (
        "toolsets",
        "proposal",
        "owner",
    )
    assert change["summary"] == "add web"
    assert "+ web" in change["diff"]


async def test_an_org_admin_reverts_a_change(
    api: SamaApi, client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    await start_chat(engine, tenant)
    owner = auth(tenant, "owner")
    path = f"/orgs/{tenant.org}/agent-profiles/{tenant.ids['profile']}"
    assert (await client.patch(path, headers=owner, json={"name": "Helper"})).status_code == 200
    (change,) = (await client.get(changes_path(tenant), headers=owner)).json()
    assert (change["kind"], change["source"]) == ("profile", "edit")
    revert = f"{changes_path(tenant)}/{change['id']}/revert"

    for user in ("mia", "admin"):
        refused = await client.post(revert, headers=auth(tenant, user))
        assert refused.status_code == 403, user
    assert (await profile(engine, tenant)).name == "Helper"

    response = await client.post(revert, headers=auth(tenant, "lead"))
    assert response.status_code == 200
    assert response.json()["profile"]["name"] == "p"
    assert (await profile(engine, tenant)).name == "p"
    back, original = (await client.get(changes_path(tenant), headers=owner)).json()
    assert back["id"] == response.json()["id"]
    assert (back["source"], back["reverts_change_id"]) == ("revert", change["id"])
    assert original["reverted_by_change_id"] == back["id"]

    again = await client.post(revert, headers=owner)
    assert again.status_code == 409
    assert "already reverted" in again.json()["detail"]
    missing = await client.post(f"{changes_path(tenant)}/{uuid.uuid4()}/revert", headers=owner)
    assert missing.status_code == 404


async def test_a_revert_after_a_later_change_gets_409(
    api: SamaApi, client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    await start_chat(engine, tenant)
    owner = auth(tenant, "owner")
    path = f"/orgs/{tenant.org}/agent-profiles/{tenant.ids['profile']}"
    await client.patch(path, headers=owner, json={"name": "Helper"})
    await client.patch(path, headers=owner, json={"name": "Other"})
    _, first = (await client.get(changes_path(tenant), headers=owner)).json()
    response = await client.post(f"{changes_path(tenant)}/{first['id']}/revert", headers=owner)
    assert response.status_code == 409
    assert "changed after" in response.json()["detail"]
    assert (await profile(engine, tenant)).name == "Other"


async def test_the_history_of_a_hidden_workspace_profile_gets_404(
    api: SamaApi, client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    await start_chat(engine, tenant)
    await set_profile(engine, tenant, workspace_id=tenant.ids["ws"])
    await add_member(engine, tenant, "outside", workspace_role=None)
    response = await client.get(changes_path(tenant), headers=auth(tenant, "outside"))
    assert response.status_code == 404
    assert (await client.get(changes_path(tenant), headers=auth(tenant, "mia"))).json() == []
