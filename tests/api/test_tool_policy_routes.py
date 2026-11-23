"""The tool policy routes of the web app (docs/architecture.md, Section 16.1; roadmap M7.10)."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.db.models import AgentProfile, AuditEntry, ProfileChange, ToolPolicy
from sama.gate.decide import DEFAULT_ALLOW, TOOLSETS
from tests.api.conftest import bearer
from tests.api.test_app import add_user, mia
from tests.orchestrator.conftest import Tenant

pytestmark = pytest.mark.usefixtures("tenant")


def profile_url(tenant: Tenant) -> str:
    return f"/orgs/{tenant.org}/agent-profiles/{tenant.ids['profile']}/tool-policy"


def org_url(tenant: Tenant) -> str:
    return f"/orgs/{tenant.org}/tool-policy"


async def admin(engine: AsyncEngine, tenant: Tenant, role: str = "admin") -> dict[str, str]:
    return bearer(await add_user(engine, tenant, workspace_member=False, role=role))


def rules(*pairs: tuple[str, str | None]) -> dict[str, Any]:
    return {"rules": [{"tool": tool, "action": action} for tool, action in pairs]}


async def test_a_member_reads_the_policy_of_a_profile(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            update(AgentProfile)
            .where(AgentProfile.id == tenant.ids["profile"])
            .values(toolsets=["web", "core"])
        )
        for profile, tool, action in (
            (tenant.ids["profile"], "web_search", "ask"),
            (None, "Bash", "deny"),
        ):
            await connection.execute(
                insert(ToolPolicy).values(
                    org_id=tenant.org, agent_profile_id=profile, tool=tool, action=action
                )
            )

    response = await client.get(profile_url(tenant), headers=mia(tenant))

    assert response.status_code == 200
    body = response.json()
    assert body["editable"] is False
    assert body["max_rules"] == 20
    assert body["rules"] == [{"tool": "web_search", "action": "ask"}]
    assert body["org_rules"] == [{"tool": "Bash", "action": "deny"}]
    tools = body["tools"]
    expected = [
        (toolset, name) for toolset, names in sorted(TOOLSETS.items()) for name in sorted(names)
    ]
    assert [(t["toolset"], t["name"]) for t in tools] == expected
    by_name = {t["name"]: t for t in tools}
    assert by_name["web_search"] == {
        "name": "web_search",
        "toolset": "web",
        "enabled": True,
        "default": "allow",
        "shared_default": "allow",
        "unattended_default": "allow",
        "always_ask": False,
    }
    assert "web_search" in DEFAULT_ALLOW
    # The sandbox tools allow in a private chat only (M8.5).
    assert {k: by_name["Bash"][k] for k in ("default", "shared_default", "unattended_default")} == {
        "default": "allow",
        "shared_default": "ask",
        "unattended_default": "ask",
    }
    assert (by_name["memory"]["shared_default"], by_name["memory"]["unattended_default"]) == (
        "ask",
        "allow",
    )
    assert by_name["repo_push_approval"]["always_ask"] is True
    assert by_name["repo_push_approval"]["default"] == "ask"
    assert all(t["enabled"] == (t["toolset"] in ("web", "core")) for t in tools)
    assert all(t["default"] in ("allow", "ask") for t in tools)


async def test_an_admin_sees_the_policy_as_editable(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    headers = await admin(engine, tenant)
    assert (await client.get(profile_url(tenant), headers=headers)).json()["editable"] is True
    assert (await client.get(org_url(tenant), headers=headers)).json()["editable"] is True
    assert (await client.get(org_url(tenant), headers=mia(tenant))).json()["editable"] is False


async def test_a_user_outside_the_org_gets_404(client: httpx.AsyncClient, tenant: Tenant) -> None:
    other = bearer("22222222-2222-2222-2222-222222222222")
    for url in (profile_url(tenant), org_url(tenant)):
        assert (await client.get(url, headers=other)).status_code == 404
    unknown = f"/orgs/{tenant.org}/agent-profiles/22222222-2222-2222-2222-222222222222/tool-policy"
    assert (await client.get(unknown, headers=mia(tenant))).status_code == 404


async def test_an_admin_sets_and_removes_profile_rules_through_the_history(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    headers = await admin(engine, tenant)

    response = await client.put(
        profile_url(tenant),
        headers=headers,
        json=rules(("web_search", "ask"), ("web_*", "deny"), ("Read", "allow")),
    )

    assert response.status_code == 200, response.text
    assert response.json()["rules"] == [
        {"tool": "Read", "action": "allow"},
        {"tool": "web_*", "action": "deny"},
        {"tool": "web_search", "action": "ask"},
    ]
    async with engine.connect() as connection:
        changes = (await connection.execute(select(ProfileChange))).all()
        audit = (
            await connection.execute(
                select(AuditEntry).where(AuditEntry.action == "profile.updated")
            )
        ).all()
    assert [(c.kind, c.source) for c in changes] == [("policy", "edit")]
    assert changes[0].before == {"rules": {"web_search": None, "web_*": None, "Read": None}}
    assert changes[0].after["rules"]["web_search"] == "ask"
    assert len(audit) == 1
    assert audit[0].details["fields"] == ["rules"]

    removed = await client.put(
        profile_url(tenant), headers=headers, json=rules(("web_*", None), ("Read", "allow"))
    )
    assert removed.status_code == 200
    assert removed.json()["rules"] == [
        {"tool": "Read", "action": "allow"},
        {"tool": "web_search", "action": "ask"},
    ]
    history = (
        await client.get(profile_url(tenant).replace("tool-policy", "changes"), headers=headers)
    ).json()
    assert len(history) == 2, "the rule that did not change is not a change"
    assert history[0]["after"] == {"rules": {"web_*": None}}


async def test_a_change_to_nothing_writes_no_history(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    headers = await admin(engine, tenant)
    await client.put(profile_url(tenant), headers=headers, json=rules(("web_search", "ask")))

    response = await client.put(
        profile_url(tenant), headers=headers, json=rules(("web_search", "ask"), ("none_yet", None))
    )

    assert response.status_code == 200
    async with engine.connect() as connection:
        assert len((await connection.execute(select(ProfileChange))).all()) == 1


@pytest.mark.parametrize(
    "body",
    [
        {"rules": []},
        {"rules": [{"tool": "x", "action": "maybe"}]},
        {"rules": [{"tool": "", "action": "ask"}]},
        {"rules": [{"tool": "a" * 201, "action": "ask"}]},
        {"rules": [{"tool": "a b", "action": "ask"}]},
        {"rules": [{"tool": "a*b", "action": "ask"}]},
        {"rules": [{"tool": "x", "action": "ask"}, {"tool": "x", "action": "deny"}]},
        {"rules": [{"tool": f"t{i}", "action": "ask"} for i in range(21)]},
        {"rules": [{"tool": "x"}]},
    ],
)
async def test_a_bad_rule_gets_422(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant, body: dict[str, Any]
) -> None:
    headers = await admin(engine, tenant)
    for url in (profile_url(tenant), org_url(tenant)):
        assert (await client.put(url, headers=headers, json=body)).status_code == 422


async def test_a_member_cannot_change_a_policy(client: httpx.AsyncClient, tenant: Tenant) -> None:
    for url in (profile_url(tenant), org_url(tenant)):
        response = await client.put(url, headers=mia(tenant), json=rules(("x", "ask")))
        assert response.status_code == 403


async def test_an_admin_sets_org_rules_with_an_audit_entry(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    headers = await admin(engine, tenant)

    response = await client.put(
        org_url(tenant), headers=headers, json=rules(("Bash", "deny"), ("web_*", "allow"))
    )

    assert response.status_code == 200
    assert response.json() == {
        "editable": True,
        "max_rules": 20,
        "rules": [{"tool": "Bash", "action": "deny"}, {"tool": "web_*", "action": "allow"}],
    }
    again = await client.put(org_url(tenant), headers=headers, json=rules(("Bash", None)))
    assert again.json()["rules"] == [{"tool": "web_*", "action": "allow"}]
    await client.put(org_url(tenant), headers=headers, json=rules(("web_*", "allow")))
    async with engine.connect() as connection:
        audits = (
            await connection.execute(
                select(AuditEntry)
                .where(AuditEntry.action == "tool_policy.updated")
                .order_by(AuditEntry.id)
            )
        ).all()
        stored = (
            await connection.execute(
                select(ToolPolicy.tool, ToolPolicy.agent_profile_id).where(
                    ToolPolicy.org_id == tenant.org
                )
            )
        ).all()
    assert [a.details for a in audits] == [
        {"tools": {"Bash": "deny", "web_*": "allow"}},
        {"tools": {"Bash": None}},
    ], "the last request changed nothing"
    assert stored == [("web_*", None)]


async def test_the_permission_agents_manage_turns_the_edit_off(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    owner = await admin(engine, tenant, "owner")
    admin_headers = await admin(engine, tenant)
    turned = await client.put(
        f"/orgs/{tenant.org}/role-permissions/admin",
        headers=owner,
        json={"permissions": {"agents.manage": False}},
    )
    assert turned.status_code == 200

    assert (await client.get(org_url(tenant), headers=admin_headers)).json()["editable"] is False
    response = await client.put(org_url(tenant), headers=admin_headers, json=rules(("x", "ask")))
    assert response.status_code == 403
    assert (await client.get(org_url(tenant), headers=owner)).json()["editable"] is True
