"""Tests of the model of a session (Sections 5.4 and 7, roadmap M8.2)."""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.db.models import AgentProfile, AuditEntry, Membership, Session
from sama.runtime.contract import RuntimeKind
from sama.subagents import known_models
from tests.api.conftest import bearer
from tests.orchestrator.conftest import Tenant, add_tenant

pytestmark = pytest.mark.usefixtures("tenant")

CLAUDE = "claude-sonnet-5"
OTHER_CLAUDE = "claude-haiku-5-5"
OPENAI = "gpt-6.2-sol"
OTHER_OPENAI = "gpt-6-luna"


def mia(tenant: Tenant) -> dict[str, str]:
    return bearer(tenant.ids["mia_auth"])


async def use_profile(engine: AsyncEngine, tenant: Tenant, runtime: str, model: str) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            update(AgentProfile)
            .where(AgentProfile.id == tenant.ids["profile"])
            .values(runtime=runtime, model=model)
        )


async def make_admin(engine: AsyncEngine, tenant: Tenant) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            update(Membership)
            .where(Membership.org_id == tenant.org, Membership.user_id == tenant.ids["mia"])
            .values(role="admin")
        )


async def audit(engine: AsyncEngine, tenant: Tenant) -> list[dict[str, object]]:
    async with engine.connect() as connection:
        rows = await connection.execute(
            select(AuditEntry.details, AuditEntry.actor_user_id, AuditEntry.target_id)
            .where(AuditEntry.org_id == tenant.org, AuditEntry.action == "session.model_changed")
            .order_by(AuditEntry.created_at)
        )
        return [
            {"details": row.details, "actor": row.actor_user_id, "target": row.target_id}
            for row in rows
        ]


async def patch(
    client: httpx.AsyncClient, tenant: Tenant, body: dict[str, object], label: str = "mine"
) -> httpx.Response:
    return await client.patch(f"/sessions/{tenant.ids[label]}", headers=mia(tenant), json=body)


async def test_a_new_session_shows_the_model_of_its_profile(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    await use_profile(engine, tenant, "claude_agent_sdk", CLAUDE)
    response = await client.get(f"/sessions/{tenant.ids['mine']}", headers=mia(tenant))
    body = response.json()
    assert (body["runtime"], body["model"], body["default_model"], body["model_is_override"]) == (
        "claude_agent_sdk",
        CLAUDE,
        CLAUDE,
        False,
    )


@pytest.mark.parametrize(
    ("runtime", "default", "chosen"),
    [
        ("claude_agent_sdk", CLAUDE, OTHER_CLAUDE),
        ("openai_agents_sdk", OPENAI, OTHER_OPENAI),
    ],
)
async def test_the_owner_chooses_a_model_and_goes_back_to_the_default(
    client: httpx.AsyncClient,
    engine: AsyncEngine,
    tenant: Tenant,
    runtime: str,
    default: str,
    chosen: str,
) -> None:
    await use_profile(engine, tenant, runtime, default)
    response = await patch(client, tenant, {"model": chosen})
    assert response.status_code == 200
    body = response.json()
    assert (body["model"], body["default_model"], body["model_is_override"]) == (
        chosen,
        default,
        True,
    )
    assert body["runtime"] == runtime
    detail = (await client.get(f"/sessions/{tenant.ids['mine']}", headers=mia(tenant))).json()
    assert (detail["model"], detail["model_is_override"]) == (chosen, True)
    async with engine.connect() as connection:
        stored = (
            await connection.execute(
                select(Session.model_override).where(Session.id == tenant.ids["mine"])
            )
        ).scalar_one()
    assert stored == chosen

    back = await patch(client, tenant, {"model": None})
    assert back.status_code == 200
    assert (back.json()["model"], back.json()["model_is_override"]) == (default, False)
    assert [entry["details"] for entry in await audit(engine, tenant)] == [
        {"from": None, "to": chosen},
        {"from": chosen, "to": None},
    ]
    first = (await audit(engine, tenant))[0]
    assert (first["actor"], first["target"]) == (tenant.ids["mia"], str(tenant.ids["mine"]))


async def test_the_default_model_chosen_again_is_not_an_override(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    await use_profile(engine, tenant, "claude_agent_sdk", CLAUDE)
    body = (await patch(client, tenant, {"model": CLAUDE})).json()
    assert (body["model"], body["model_is_override"]) == (CLAUDE, False)


async def test_the_same_model_twice_writes_one_audit_row(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    await use_profile(engine, tenant, "claude_agent_sdk", CLAUDE)
    await patch(client, tenant, {"model": OTHER_CLAUDE})
    assert (await patch(client, tenant, {"model": OTHER_CLAUDE})).status_code == 200
    assert len(await audit(engine, tenant)) == 1


@pytest.mark.parametrize("model", ["not-a-model", OPENAI, "", "x" * 101, "claude-sonnet-5-nope"])
async def test_a_model_that_the_runtime_does_not_know_is_refused(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant, model: str
) -> None:
    await use_profile(engine, tenant, "claude_agent_sdk", CLAUDE)
    response = await patch(client, tenant, {"model": model})
    assert response.status_code == 422
    assert await audit(engine, tenant) == []
    detail = (await client.get(f"/sessions/{tenant.ids['mine']}", headers=mia(tenant))).json()
    assert detail["model_is_override"] is False


async def test_an_openai_session_refuses_a_claude_model(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    await use_profile(engine, tenant, "openai_agents_sdk", OPENAI)
    assert (await patch(client, tenant, {"model": CLAUDE})).status_code == 422


async def test_the_model_can_come_with_a_title(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    await use_profile(engine, tenant, "claude_agent_sdk", CLAUDE)
    body = (await patch(client, tenant, {"title": "Report", "model": OTHER_CLAUDE})).json()
    assert (body["title"], body["model"]) == ("Report", OTHER_CLAUDE)
    # An empty body is still refused.
    assert (await patch(client, tenant, {})).status_code == 422


async def test_the_list_of_models_belongs_to_the_runtime(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    await use_profile(engine, tenant, "claude_agent_sdk", CLAUDE)
    body = (await client.get(f"/sessions/{tenant.ids['mine']}/models", headers=mia(tenant))).json()
    assert body == {
        "runtime": "claude_agent_sdk",
        "models": known_models(RuntimeKind.CLAUDE_AGENT_SDK),
        "model": CLAUDE,
        "default_model": CLAUDE,
        "is_override": False,
        "editable": True,
    }
    await use_profile(engine, tenant, "openai_agents_sdk", OPENAI)
    other = (await client.get(f"/sessions/{tenant.ids['s2']}/models", headers=mia(tenant))).json()
    assert other["models"] == known_models(RuntimeKind.OPENAI_AGENTS_SDK)
    assert OPENAI in other["models"]
    assert CLAUDE not in other["models"]


async def test_a_member_cannot_change_a_shared_session_but_reads_its_models(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    await use_profile(engine, tenant, "claude_agent_sdk", CLAUDE)
    refused = await patch(client, tenant, {"model": OTHER_CLAUDE}, label="s1")
    assert refused.status_code == 403
    assert await audit(engine, tenant) == []
    body = (await client.get(f"/sessions/{tenant.ids['s1']}/models", headers=mia(tenant))).json()
    assert (body["editable"], body["model"]) == (False, CLAUDE)
    detail = (await client.get(f"/sessions/{tenant.ids['s1']}", headers=mia(tenant))).json()
    assert (detail["can_manage"], detail["model"]) == (False, CLAUDE)


async def test_an_org_admin_can_change_a_shared_session(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    await use_profile(engine, tenant, "claude_agent_sdk", CLAUDE)
    await make_admin(engine, tenant)
    response = await patch(client, tenant, {"model": OTHER_CLAUDE}, label="s1")
    assert response.status_code == 200
    assert response.json()["model"] == OTHER_CLAUDE
    body = (await client.get(f"/sessions/{tenant.ids['s1']}/models", headers=mia(tenant))).json()
    assert body["editable"] is True


async def test_another_org_gets_404(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    other = await add_tenant(engine)
    eve = bearer(other.ids["mia_auth"])
    mine = tenant.ids["mine"]
    assert (
        await client.patch(f"/sessions/{mine}", headers=eve, json={"model": CLAUDE})
    ).status_code == 404
    assert (await client.get(f"/sessions/{mine}/models", headers=eve)).status_code == 404
    assert (
        await client.get(f"/sessions/{tenant.ids['s1']}/models", headers=eve)
    ).status_code == 404


async def test_a_coding_agent_profile_keeps_its_own_model(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    await use_profile(engine, tenant, "claude_code", CLAUDE)
    refused = await patch(client, tenant, {"model": OTHER_CLAUDE})
    assert refused.status_code == 409
    body = (await client.get(f"/sessions/{tenant.ids['mine']}/models", headers=mia(tenant))).json()
    assert (body["models"], body["editable"]) == ([], False)
    # Going back to the default needs no list and is always possible.
    assert (await patch(client, tenant, {"model": None})).status_code == 200


async def test_the_direct_mode_does_not_block_the_model_of_the_sama_runtime(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    await use_profile(engine, tenant, "claude_agent_sdk", CLAUDE)
    async with engine.begin() as connection:
        await connection.execute(
            update(Session).where(Session.id == tenant.ids["mine"]).values(coding_agent="codex")
        )
    body = (await patch(client, tenant, {"model": OTHER_CLAUDE})).json()
    assert (body["runtime"], body["model"], body["coding_agent"]) == (
        "claude_agent_sdk",
        OTHER_CLAUDE,
        "codex",
    )


async def test_the_session_list_does_not_fill_the_model_fields(
    client: httpx.AsyncClient, tenant: Tenant
) -> None:
    items = (await client.get("/sessions", headers=mia(tenant))).json()
    assert items
    assert all(item["model"] is None and item["model_is_override"] is False for item in items)
