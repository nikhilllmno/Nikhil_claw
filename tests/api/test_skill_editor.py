"""The skill editor routes of the web API (docs/architecture.md, Sections 9.2 and 11.4)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.api import ApiSettings, JwtVerifier, SamaApi
from sama.db.models import AuditEntry, Membership, SkillVersion, User
from sama.skills import SkillLibrary
from tests.api.conftest import ISSUER, SECRET, bearer
from tests.api.test_app import mia
from tests.orchestrator.conftest import Tenant, add_tenant
from tests.skills.helpers import doc, md

pytestmark = pytest.mark.usefixtures("tenant")


@pytest.fixture
async def client(engine: AsyncEngine) -> AsyncIterator[httpx.AsyncClient]:
    library = SkillLibrary(
        engine, {"summarize": doc("summarize", description="Summarize a text", files={"a.md": "A"})}
    )
    api = SamaApi(
        engine,
        verifier=JwtVerifier(secret=SecretStr(SECRET), issuer=ISSUER),
        settings=ApiSettings(supabase_url="https://project.example.test", supabase_anon_key="k"),
        skills=library,
    )
    transport = httpx.ASGITransport(app=api.app())
    async with httpx.AsyncClient(transport=transport, base_url="http://sama.test") as client:
        yield client


async def add_member(
    engine: AsyncEngine, tenant: Tenant, *, org_role: str, workspace_role: str | None
) -> dict[str, str]:
    """Adds a user with the roles and gives the headers of the user."""
    user_id, auth_id = uuid.uuid4(), uuid.uuid4()
    async with engine.begin() as connection:
        await connection.execute(
            insert(User).values(
                id=user_id, org_id=tenant.org, auth_user_id=auth_id, display_name="zed"
            )
        )
        await connection.execute(
            insert(Membership).values(org_id=tenant.org, user_id=user_id, role=org_role)
        )
        if workspace_role is not None:
            await connection.execute(
                insert(Membership).values(
                    org_id=tenant.org,
                    user_id=user_id,
                    workspace_id=tenant.ids["ws"],
                    role=workspace_role,
                )
            )
    return bearer(auth_id)


def base(tenant: Tenant) -> str:
    return f"/workspaces/{tenant.ids['ws']}/skills"


async def test_the_catalog_names_the_writable_scopes(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    member = await client.get(base(tenant), headers=mia(tenant))
    assert member.status_code == 200
    assert member.json()["writable_scopes"] == ["user"]
    [builtin] = member.json()["skills"]
    assert builtin["name"] == "summarize"
    assert builtin["scope"] == "builtin"
    assert builtin["can_edit"] is False

    admin = await add_member(engine, tenant, org_role="member", workspace_role="admin")
    assert (await client.get(base(tenant), headers=admin)).json()["writable_scopes"] == [
        "user",
        "workspace",
    ]
    owner = await add_member(engine, tenant, org_role="owner", workspace_role=None)
    assert (await client.get(base(tenant), headers=owner)).json()["writable_scopes"] == [
        "user",
        "workspace",
        "org",
    ]


async def test_a_builtin_skill_reads_in_full_and_is_read_only(
    client: httpx.AsyncClient, tenant: Tenant
) -> None:
    response = await client.get(f"{base(tenant)}/summarize", headers=mia(tenant))
    assert response.status_code == 200
    skill = response.json()
    assert skill["scope"] == "builtin"
    assert skill["skill_md"].startswith("---\nname: summarize")
    assert skill["files"] == {"a.md": "A"}
    assert skill["can_edit"] is False
    missing = await client.get(f"{base(tenant)}/nope", headers=mia(tenant))
    assert missing.status_code == 404
    refused = await client.post(
        base(tenant),
        headers=mia(tenant),
        json={"action": "edit", "name": "summarize", "scope": "builtin", "content": "x"},
    )
    assert refused.status_code == 422


async def test_a_member_creates_edits_and_archives_an_own_skill(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    created = await client.post(
        base(tenant),
        headers=mia(tenant),
        json={
            "action": "create",
            "name": "my-notes",
            "scope": "user",
            "content": md("my-notes", "My notes"),
            "summary": "first",
        },
    )
    assert created.status_code == 200, created.text
    assert created.json() == {"name": "my-notes", "scope": "user", "version": 1, "change": "create"}

    edited = await client.post(
        base(tenant),
        headers=mia(tenant),
        json={
            "action": "edit",
            "name": "my-notes",
            "scope": "user",
            "content": md("my-notes", "My better notes", body="Do it well."),
        },
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["version"] == 2

    added = await client.post(
        base(tenant),
        headers=mia(tenant),
        json={
            "action": "write_file",
            "name": "my-notes",
            "scope": "user",
            "file_path": "references/more.md",
            "file_content": "More.",
        },
    )
    assert added.status_code == 200, added.text

    detail = (await client.get(f"{base(tenant)}/my-notes", headers=mia(tenant))).json()
    assert detail["scope"] == "user"
    assert detail["description"] == "My better notes"
    assert detail["files"] == {"references/more.md": "More."}
    assert detail["can_edit"] is True

    old = await client.get(f"/skills/{detail['id']}/versions/1", headers=mia(tenant))
    assert old.status_code == 200
    assert old.json()["description"] == "My notes"
    assert old.json()["author_kind"] == "user"
    assert old.json()["summary"] == "first"
    gone = await client.get(f"/skills/{detail['id']}/versions/9", headers=mia(tenant))
    assert gone.status_code == 404

    async with engine.connect() as connection:
        kinds = set(
            (
                await connection.execute(
                    select(SkillVersion.author_kind).where(
                        SkillVersion.skill_id == uuid.UUID(detail["id"])
                    )
                )
            ).scalars()
        )
        audit = set(
            (
                await connection.execute(
                    select(AuditEntry.actor_kind).where(
                        AuditEntry.org_id == tenant.org, AuditEntry.action.like("skill.%")
                    )
                )
            ).scalars()
        )
    assert kinds == {"user"}
    assert audit == {"user"}

    archived = await client.post(
        base(tenant),
        headers=mia(tenant),
        json={"action": "archive", "name": "my-notes", "scope": "user"},
    )
    assert archived.status_code == 200, archived.text
    names = {
        s["name"] for s in (await client.get(base(tenant), headers=mia(tenant))).json()["skills"]
    }
    assert "my-notes" not in names


async def test_a_member_cannot_write_a_workspace_or_org_skill(
    client: httpx.AsyncClient, tenant: Tenant
) -> None:
    for scope in ("workspace", "org"):
        response = await client.post(
            base(tenant),
            headers=mia(tenant),
            json={"action": "create", "name": "team", "scope": scope, "content": md("team")},
        )
        assert response.status_code == 403
        assert "admin" in response.json()["detail"] or "owner" in response.json()["detail"]


async def test_a_workspace_admin_writes_a_workspace_skill_that_others_read(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    admin = await add_member(engine, tenant, org_role="member", workspace_role="admin")
    created = await client.post(
        base(tenant),
        headers=admin,
        json={"action": "create", "name": "team", "scope": "workspace", "content": md("team")},
    )
    assert created.status_code == 200, created.text
    assert (
        await client.post(
            base(tenant),
            headers=admin,
            json={"action": "create", "name": "all", "scope": "org", "content": md("all")},
        )
    ).status_code == 403

    seen = (await client.get(f"{base(tenant)}/team", headers=mia(tenant))).json()
    assert seen["scope"] == "workspace"
    assert seen["can_edit"] is False

    # A user skill with the same name wins for its owner. The workspace skill stays readable.
    await client.post(
        base(tenant),
        headers=mia(tenant),
        json={"action": "create", "name": "team", "scope": "user", "content": md("team", "Mine")},
    )
    mine = (await client.get(f"{base(tenant)}/team", headers=mia(tenant))).json()
    assert mine["scope"] == "user"
    assert mine["other_scopes"] == ["workspace"]
    shared = await client.get(
        f"{base(tenant)}/team", headers=mia(tenant), params={"scope": "workspace"}
    )
    assert shared.json()["scope"] == "workspace"


async def test_the_editor_answers_404_outside_the_workspace(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    outsider = await add_member(engine, tenant, org_role="member", workspace_role=None)
    assert (await client.get(base(tenant), headers=outsider)).status_code == 404
    other = await add_tenant(engine)
    stranger = bearer(other.ids["mia_auth"])
    assert (await client.get(base(tenant), headers=stranger)).status_code == 404
    write = await client.post(
        base(tenant),
        headers=stranger,
        json={"action": "create", "name": "x", "scope": "user", "content": md("x")},
    )
    assert write.status_code == 404


async def test_a_user_skill_of_another_user_stays_hidden(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    created = await client.post(
        base(tenant),
        headers=mia(tenant),
        json={"action": "create", "name": "secret", "scope": "user", "content": md("secret")},
    )
    assert created.status_code == 200
    skill_id = (await client.get(f"{base(tenant)}/secret", headers=mia(tenant))).json()["id"]
    zed = await add_member(engine, tenant, org_role="member", workspace_role="member")
    assert (await client.get(f"{base(tenant)}/secret", headers=zed)).status_code == 404
    assert (await client.get(f"/skills/{skill_id}/versions/1", headers=zed)).status_code == 404


async def test_the_editor_needs_a_library(tenant: Tenant, api: SamaApi) -> None:
    transport = httpx.ASGITransport(app=api.app())
    async with httpx.AsyncClient(transport=transport, base_url="http://sama.test") as plain:
        response = await plain.get(base(tenant), headers=mia(tenant))
    assert response.status_code == 409
