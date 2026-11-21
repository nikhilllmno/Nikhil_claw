"""The permissions of the roles of an org: the catalog, the routes, ``/me``, and the checks of the
routes that they limit (docs/architecture.md, Section 17.2; roadmap M7.10)."""

from __future__ import annotations

import uuid
from dataclasses import replace
from typing import Any

import httpx
import pytest
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.db.models import AuditEntry, OrgInvite, OrgRolePermission, Skill
from sama.orgs.permissions import BY_KEY, CATALOG, KEYS, allowed_keys, matrix
from tests.api.conftest import bearer
from tests.api.test_app import add_user, mia
from tests.orchestrator.conftest import Tenant

pytestmark = pytest.mark.usefixtures("tenant")

MEMBER_KEYS = [p.key for p in CATALOG if "member" in p.roles]


def url(tenant: Tenant, role: str | None = None) -> str:
    base = f"/orgs/{tenant.org}/role-permissions"
    return base if role is None else f"{base}/{role}"


async def user(engine: AsyncEngine, tenant: Tenant, role: str) -> dict[str, str]:
    return bearer(await add_user(engine, tenant, workspace_member=True, role=role))


async def turn_off(
    client: httpx.AsyncClient, tenant: Tenant, owner: dict[str, str], role: str, *keys: str
) -> None:
    response = await client.put(
        url(tenant, role), headers=owner, json={"permissions": {key: False for key in keys}}
    )
    assert response.status_code == 200, response.text


def test_the_catalog_has_the_ten_keys_of_the_contract() -> None:
    assert KEYS == (
        "jobs.create",
        "workflows.create",
        "triggers.create",
        "connections.add",
        "skills.write",
        "browser.logins",
        "sessions.shared",
        "members.invite",
        "agents.manage",
        "chat_apps.manage",
    )
    assert BY_KEY["members.invite"].roles == ("admin",)
    assert BY_KEY["jobs.create"].roles == ("admin", "member")
    assert BY_KEY["agents.manage"].label == "Create and change agents and their tool policy"


def test_every_permission_is_allowed_without_a_row() -> None:
    rows = matrix({})
    assert set(rows["admin"]) == set(KEYS)
    assert set(rows["member"]) == set(MEMBER_KEYS)
    assert all(all(role.values()) for role in rows.values())
    assert allowed_keys("owner", {}) == list(KEYS)
    assert allowed_keys("member", {("member", "jobs.create"): False}) == [
        key for key in MEMBER_KEYS if key != "jobs.create"
    ]
    assert allowed_keys("stranger", {}) == []


async def test_a_member_reads_the_permissions_but_cannot_edit(
    client: httpx.AsyncClient, tenant: Tenant
) -> None:
    response = await client.get(url(tenant), headers=mia(tenant))

    assert response.status_code == 200
    body = response.json()
    assert [p["key"] for p in body["permissions"]] == list(KEYS)
    first = body["permissions"][0]
    assert set(first) == {"key", "label", "description", "roles"}
    assert (first["label"], first["roles"]) == ("Create scheduled jobs", ["admin", "member"])
    assert set(body["roles"]["admin"]) == set(KEYS)
    assert set(body["roles"]["member"]) == set(MEMBER_KEYS)
    assert body["editable"] == {"admin": False, "member": False}


async def test_a_user_outside_the_org_gets_404(client: httpx.AsyncClient, tenant: Tenant) -> None:
    other = bearer("11111111-1111-1111-1111-111111111111")
    assert (await client.get(url(tenant), headers=other)).status_code == 404
    body = {"permissions": {"jobs.create": False}}
    response = await client.put(url(tenant, "member"), headers=other, json=body)
    assert response.status_code == 404


async def test_who_can_edit_which_role(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    owner = await user(engine, tenant, "owner")
    admin = await user(engine, tenant, "admin")

    assert (await client.get(url(tenant), headers=owner)).json()["editable"] == {
        "admin": True,
        "member": True,
    }
    assert (await client.get(url(tenant), headers=admin)).json()["editable"] == {
        "admin": False,
        "member": True,
    }
    body = {"permissions": {"jobs.create": False}}
    assert (await client.put(url(tenant, "admin"), headers=admin, json=body)).status_code == 403
    assert (
        await client.put(url(tenant, "member"), headers=mia(tenant), json=body)
    ).status_code == 403
    assert (await client.put(url(tenant, "member"), headers=admin, json=body)).status_code == 200
    assert (await client.put(url(tenant, "admin"), headers=owner, json=body)).status_code == 200


async def test_a_change_returns_the_view_and_writes_one_audit_entry(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    owner = await user(engine, tenant, "owner")
    body = {"permissions": {"jobs.create": False, "skills.write": True}}

    response = await client.put(url(tenant, "member"), headers=owner, json=body)

    assert response.status_code == 200
    view = response.json()
    assert view["roles"]["member"]["jobs.create"] is False
    assert view["roles"]["member"]["skills.write"] is True
    assert view["roles"]["admin"]["jobs.create"] is True
    async with engine.connect() as connection:
        rows = (await connection.execute(select(OrgRolePermission))).all()
        audits = (
            await connection.execute(
                select(AuditEntry).where(AuditEntry.action == "role.permissions_changed")
            )
        ).all()
    assert [(r.role, r.permission, r.allowed) for r in rows] == [("member", "jobs.create", False)]
    assert len(audits) == 1
    assert audits[0].details == {"role": "member", "changed": {"jobs.create": False}}
    assert audits[0].target_type == "org"


async def test_a_change_to_the_same_values_writes_nothing(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    owner = await user(engine, tenant, "owner")
    await turn_off(client, tenant, owner, "member", "jobs.create")
    body = {"permissions": {"jobs.create": False, "skills.write": True}}

    response = await client.put(url(tenant, "member"), headers=owner, json=body)

    assert response.status_code == 200
    async with engine.connect() as connection:
        audits = (
            await connection.execute(
                select(AuditEntry).where(AuditEntry.action == "role.permissions_changed")
            )
        ).all()
    assert len(audits) == 1, "only the first change has an entry"
    # The value goes back to allowed with an update of the row.
    again = await client.put(
        url(tenant, "member"), headers=owner, json={"permissions": {"jobs.create": True}}
    )
    assert again.json()["roles"]["member"]["jobs.create"] is True


@pytest.mark.parametrize(
    ("role", "permissions"),
    [
        ("member", {"nothing.here": True}),
        ("member", {"members.invite": False}),
        ("member", {}),
        ("member", {"jobs.create": "maybe"}),
        ("owner", {"jobs.create": False}),
        ("admin", {f"k{i}": True for i in range(21)}),
    ],
)
async def test_a_bad_change_gets_422(
    client: httpx.AsyncClient,
    engine: AsyncEngine,
    tenant: Tenant,
    role: str,
    permissions: dict[str, Any],
) -> None:
    owner = await user(engine, tenant, "owner")
    response = await client.put(url(tenant, role), headers=owner, json={"permissions": permissions})
    assert response.status_code == 422


async def test_me_lists_the_allowed_keys_of_each_role(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    owner = await user(engine, tenant, "owner")
    admin = await user(engine, tenant, "admin")
    await turn_off(client, tenant, owner, "member", "jobs.create")
    await turn_off(client, tenant, owner, "admin", "agents.manage")

    member_org = (await client.get("/me", headers=mia(tenant))).json()["orgs"][0]
    admin_org = (await client.get("/me", headers=admin)).json()["orgs"][0]
    owner_org = (await client.get("/me", headers=owner)).json()["orgs"][0]

    assert member_org["permissions"] == [k for k in MEMBER_KEYS if k != "jobs.create"]
    assert admin_org["permissions"] == [k for k in KEYS if k != "agents.manage"]
    assert owner_org["permissions"] == list(KEYS), "the owner has every permission"


# -- the routes that the permissions limit ------------------------------------------------------

DENIED = "Your role in this org cannot do this: "


def member_calls(tenant: Tenant) -> list[tuple[str, str, str, dict[str, Any]]]:
    """``(key, method, url, body)`` of a call that a member makes. The bodies are valid, so the
    permission check is the only reason for the ``403``."""
    ws = tenant.ids["ws"]
    return [
        ("jobs.create", "POST", f"/orgs/{tenant.org}/jobs", {"name": "j", "schedule": "every 1h"}),
        ("workflows.create", "POST", f"/orgs/{tenant.org}/workflows", {"yaml": "name: w"}),
        (
            "triggers.create",
            "POST",
            f"/workspaces/{ws}/triggers",
            {"kind": "webhook", "name": "t", "prompt": "p"},
        ),
        ("connections.add", "POST", f"/workspaces/{ws}/connections", {"connector": "gmail"}),
        (
            "connections.add",
            "POST",
            f"/workspaces/{ws}/connections/mcp",
            {"name": "m", "url": "https://mcp.example.test/mcp", "auth": "none"},
        ),
        (
            "skills.write",
            "POST",
            f"/workspaces/{ws}/skills",
            {"action": "create", "name": "s", "scope": "user", "content": "x"},
        ),
        (
            "skills.write",
            "POST",
            f"/workspaces/{ws}/skills/s/pin",
            {},
        ),
        (
            "browser.logins",
            "POST",
            f"/orgs/{tenant.org}/browser/logins",
            {"name": "n", "site": "https://example.test", "username": "u", "password": "p"},
        ),
        (
            "sessions.shared",
            "POST",
            "/sessions",
            {
                "workspace_id": str(ws),
                "agent_profile_id": str(tenant.ids["profile"]),
                "audience": "shared",
            },
        ),
    ]


async def test_a_member_without_the_permission_gets_403_on_each_route(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    owner = await user(engine, tenant, "owner")
    await turn_off(client, tenant, owner, "member", *MEMBER_KEYS)

    for key, method, path, body in member_calls(tenant):
        kwargs: dict[str, Any] = {"json": body} if body else {}
        if path.endswith("/pin"):
            kwargs = {"params": {"scope": "user"}}
        response = await client.request(method, path, headers=mia(tenant), **kwargs)
        assert response.status_code == 403, (path, response.text)
        assert response.json()["detail"] == f"{DENIED}{BY_KEY[key].label}.", path


async def test_the_default_gives_a_member_the_shared_session(
    client: httpx.AsyncClient, tenant: Tenant
) -> None:
    key, method, path, body = member_calls(tenant)[-1]
    assert key == "sessions.shared"
    response = await client.request(method, path, headers=mia(tenant), json=body)
    assert response.status_code == 201


async def test_the_owner_of_the_org_keeps_every_permission(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    owner = await user(engine, tenant, "owner")
    await turn_off(client, tenant, owner, "member", *MEMBER_KEYS)
    await turn_off(client, tenant, owner, "admin", *KEYS)
    *_, (_, method, path, body) = member_calls(tenant)

    response = await client.request(method, path, headers=owner, json=body)

    assert response.status_code == 201


async def test_turning_a_permission_off_for_members_does_not_change_admins(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    owner = await user(engine, tenant, "owner")
    admin = await user(engine, tenant, "admin")
    await turn_off(client, tenant, owner, "member", "sessions.shared")
    *_, (_, method, path, body) = member_calls(tenant)

    assert (await client.request(method, path, headers=mia(tenant), json=body)).status_code == 403
    assert (await client.request(method, path, headers=admin, json=body)).status_code == 201


async def test_an_admin_without_the_permission_cannot_invite_or_manage_agents(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    owner = await user(engine, tenant, "owner")
    admin = await user(engine, tenant, "admin")
    profile = f"/orgs/{tenant.org}/agent-profiles/{tenant.ids['profile']}"
    allowed = await client.post(
        f"/orgs/{tenant.org}/invites", headers=admin, json={"email": "new@example.test"}
    )
    assert allowed.status_code == 201, allowed.text
    await turn_off(
        client, tenant, owner, "admin", "members.invite", "agents.manage", "chat_apps.manage"
    )

    invite = await client.post(
        f"/orgs/{tenant.org}/invites", headers=admin, json={"email": "next@example.test"}
    )
    assert invite.status_code == 403
    assert invite.json()["detail"] == f"{DENIED}Invite people."
    calls = [
        ("PATCH", profile, {"name": "renamed"}, "agents.manage"),
        ("POST", f"/orgs/{tenant.org}/agent-profiles", {"name": "new"}, "agents.manage"),
        (
            "PUT",
            f"{profile}/tool-policy",
            {"rules": [{"tool": "x", "action": "ask"}]},
            "agents.manage",
        ),
        (
            "PUT",
            f"/orgs/{tenant.org}/tool-policy",
            {"rules": [{"tool": "x", "action": "ask"}]},
            "agents.manage",
        ),
        (
            "POST",
            f"/orgs/{tenant.org}/integrations/telegram",
            {"bot_token": "1:" + "a" * 35},
            "chat_apps.manage",
        ),
        ("POST", f"/orgs/{tenant.org}/integrations/slack/setup", None, "chat_apps.manage"),
        ("DELETE", f"/orgs/{tenant.org}/integrations/telegram", None, "chat_apps.manage"),
        ("POST", f"/orgs/{tenant.org}/integrations/telegram/check", None, "chat_apps.manage"),
    ]
    for method, path, body, key in calls:
        response = await client.request(method, path, headers=admin, json=body)
        assert response.status_code == 403, (path, response.text)
        assert response.json()["detail"] == f"{DENIED}{BY_KEY[key].label}.", path
    async with engine.connect() as connection:
        assert (await connection.execute(select(OrgInvite))).all() != [], "the first invite stays"


async def test_the_agent_tools_refuse_in_the_same_way_as_the_api(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    from sama.db.models import Run
    from sama.skills import ManageRequest, PostgresSkillStore
    from sama.skills.errors import SkillError
    from sama.tenancy.run_token import RunTokenClaims
    from tests.skills.helpers import md

    run_id = uuid.uuid4()
    async with engine.begin() as connection:
        await connection.execute(
            insert(Run).values(
                id=run_id,
                org_id=tenant.org,
                session_id=tenant.ids["mine"],
                agent_profile_id=tenant.ids["profile"],
                triggered_by_user_id=tenant.ids["mia"],
                runtime="fake",
                status="queued",
            )
        )
    claims = RunTokenClaims(
        org_id=str(tenant.org),
        workspace_id=str(tenant.ids["ws"]),
        session_id=str(tenant.ids["mine"]),
        run_id=str(run_id),
        user_id=str(tenant.ids["mia"]),
        token_id="t-1",
        expires_at=2_000_000_000,
    )
    request = ManageRequest(action="create", name="notes", scope="user", content=md("notes", "x"))
    store = PostgresSkillStore(engine, {})
    written = await store.manage(claims, request)
    assert written.name == "notes"
    owner = await user(engine, tenant, "owner")
    await turn_off(client, tenant, owner, "member", "skills.write")

    with pytest.raises(SkillError) as raised:
        await store.manage(claims, replace(request, name="other"))

    assert str(raised.value) == f"{DENIED}{BY_KEY['skills.write'].label}."
    async with engine.connect() as connection:
        names = (await connection.execute(select(Skill.name))).scalars().all()
    assert names == ["notes"]


async def test_the_profile_is_editable_only_when_the_patch_would_pass(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    owner = await user(engine, tenant, "owner")
    admin = await user(engine, tenant, "admin")
    profile = f"/orgs/{tenant.org}/agent-profiles/{tenant.ids['profile']}"

    async def state(headers: dict[str, str]) -> tuple[bool, int]:
        editable = (await client.get(profile, headers=headers)).json()["editable"]
        patched = await client.patch(profile, headers=headers, json={"reasoning_effort": "low"})
        return editable, patched.status_code

    assert await state(admin) == (True, 200)
    assert await state(mia(tenant)) == (False, 403)
    await turn_off(client, tenant, owner, "admin", "agents.manage")
    assert await state(admin) == (False, 403)
    assert await state(owner) == (True, 200)
