"""The skill routes and skill commands of the HTTP API (docs/architecture.md, Section 9.5)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.api import ApiSettings, JwtVerifier, SamaApi
from sama.db.models import AgentProfile, Membership, Message, SkillVersion
from sama.orchestrator.queue import RunQueue
from sama.skills import ManageRequest, PostgresSkillStore, SkillLibrary
from sama.tenancy.run_token import RunTokenClaims
from tests.api.conftest import ISSUER, SECRET, bearer
from tests.api.test_app import add_user, mia
from tests.orchestrator.conftest import Tenant, add_tenant
from tests.skills.helpers import doc, md

pytestmark = pytest.mark.usefixtures("tenant")


@pytest.fixture
def library(engine: AsyncEngine) -> SkillLibrary:
    return SkillLibrary(
        engine,
        {
            "summarize": doc("summarize", description="Summarize a text"),
            "needs-web": doc("needs-web", description="Needs the web", requires=("web",)),
        },
    )


@pytest.fixture
async def client(
    engine: AsyncEngine, tenant: Tenant, library: SkillLibrary
) -> AsyncIterator[httpx.AsyncClient]:
    async with engine.begin() as connection:
        await connection.execute(
            update(AgentProfile)
            .where(AgentProfile.id == tenant.ids["profile"])
            .values(toolsets=["core", "skills"], group_toolsets=["core", "skills"])
        )
    api = SamaApi(
        engine,
        verifier=JwtVerifier(secret=SecretStr(SECRET), issuer=ISSUER),
        settings=ApiSettings(
            supabase_url="https://project.example.test",
            supabase_anon_key="anon-key",
            poll_s=0.02,
            heartbeat_s=0.05,
            max_stream_s=0.3,
        ),
        skills=library,
    )
    transport = httpx.ASGITransport(app=api.app())
    async with httpx.AsyncClient(transport=transport, base_url="http://sama.test") as client:
        yield client


async def write(
    engine: AsyncEngine, tenant: Tenant, session: str, name: str, scope: str | None = None
) -> RunTokenClaims:
    """Creates a skill as the agent of a run of ``session``, and returns the claims."""
    run_id = await RunQueue(engine).enqueue(
        org_id=tenant.org, session_id=tenant.ids[session], text="hi", user_id=tenant.ids["mia"]
    )
    claims = RunTokenClaims(
        org_id=str(tenant.org),
        workspace_id=str(tenant.ids["ws"]),
        session_id=str(tenant.ids[session]),
        run_id=str(run_id),
        user_id=str(tenant.ids["mia"]),
        token_id="t-1",
        expires_at=2_000_000_000,
    )
    await PostgresSkillStore(engine, {}).manage(
        claims,
        ManageRequest(action="create", name=name, scope=scope, content=md(name, f"About {name}")),
    )
    return claims


async def test_the_skill_list_shows_what_the_user_can_use(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    await write(engine, tenant, "mine", "my-notes")
    response = await client.get(
        f"/orgs/{tenant.org}/skills",
        headers=mia(tenant),
        params={"workspace_id": str(tenant.ids["ws"])},
    )
    assert response.status_code == 200
    skills = {s["name"]: s for s in response.json()}
    assert set(skills) == {"summarize", "needs-web", "my-notes"}
    assert skills["summarize"]["scope"] == "builtin"
    assert skills["summarize"]["id"] is None
    assert skills["my-notes"]["scope"] == "user"
    assert skills["my-notes"]["version"] == "1"
    assert uuid.UUID(skills["my-notes"]["id"])


async def test_the_skill_list_hides_a_user_skill_of_another_user_and_a_closed_workspace(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    await write(engine, tenant, "mine", "my-notes")
    zed = bearer(await add_user(engine, tenant, workspace_member=True))
    params = {"workspace_id": str(tenant.ids["ws"])}
    seen = await client.get(f"/orgs/{tenant.org}/skills", headers=zed, params=params)
    assert "my-notes" not in {s["name"] for s in seen.json()}
    outsider = bearer(await add_user(engine, tenant, workspace_member=False))
    assert (
        await client.get(f"/orgs/{tenant.org}/skills", headers=outsider, params=params)
    ).status_code == 404
    other = await add_tenant(engine)
    stranger = await client.get(
        f"/orgs/{tenant.org}/skills", headers=bearer(other.ids["mia_auth"]), params=params
    )
    assert stranger.status_code == 404


async def test_the_skills_need_a_library(engine: AsyncEngine, tenant: Tenant, api: SamaApi) -> None:
    transport = httpx.ASGITransport(app=api.app())
    async with httpx.AsyncClient(transport=transport, base_url="http://sama.test") as plain:
        response = await plain.get(
            f"/orgs/{tenant.org}/skills",
            headers=mia(tenant),
            params={"workspace_id": str(tenant.ids["ws"])},
        )
    assert response.status_code == 409


async def test_the_versions_show_the_author_and_the_run(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    claims = await write(engine, tenant, "mine", "my-notes")
    await PostgresSkillStore(engine, {}).manage(
        claims,
        ManageRequest(
            action="patch",
            name="my-notes",
            old_string="Do it.",
            new_string="Do more.",
            summary="More",
        ),
    )
    listed = await client.get(
        f"/orgs/{tenant.org}/skills",
        headers=mia(tenant),
        params={"workspace_id": str(tenant.ids["ws"])},
    )
    skill_id = next(s["id"] for s in listed.json() if s["name"] == "my-notes")
    response = await client.get(f"/skills/{skill_id}/versions", headers=mia(tenant))
    assert response.status_code == 200
    versions = response.json()
    assert [(v["version"], v["change"]) for v in versions] == [(2, "patch"), (1, "create")]
    assert versions[0]["summary"] == "More"
    assert versions[0]["author_kind"] == "agent"
    assert versions[0]["author_user_id"] == str(tenant.ids["mia"])
    assert versions[0]["session_id"] == str(tenant.ids["mine"])
    assert versions[0]["run_id"] == claims.run_id


async def test_the_versions_of_a_hidden_skill_are_404(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    await write(engine, tenant, "mine", "my-notes")
    listed = await client.get(
        f"/orgs/{tenant.org}/skills",
        headers=mia(tenant),
        params={"workspace_id": str(tenant.ids["ws"])},
    )
    skill_id = next(s["id"] for s in listed.json() if s["name"] == "my-notes")
    zed = bearer(await add_user(engine, tenant, workspace_member=True))
    assert (await client.get(f"/skills/{skill_id}/versions", headers=zed)).status_code == 404
    other = await add_tenant(engine)
    eve = bearer(other.ids["mia_auth"])
    assert (await client.get(f"/skills/{skill_id}/versions", headers=eve)).status_code == 404
    unknown = await client.get(f"/skills/{uuid.uuid4()}/versions", headers=mia(tenant))
    assert unknown.status_code == 404


async def test_the_versions_are_bounded_by_the_org_of_the_skill(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    await write(engine, tenant, "mine", "my-notes")
    other = await add_tenant(engine)
    listed = await client.get(
        f"/orgs/{tenant.org}/skills",
        headers=mia(tenant),
        params={"workspace_id": str(tenant.ids["ws"])},
    )
    skill_id = next(s["id"] for s in listed.json() if s["name"] == "my-notes")
    # A version row of another org cannot point at this skill: the foreign key holds both ids.
    async with engine.begin() as connection:
        with pytest.raises(IntegrityError, match="skill_versions"):
            await connection.execute(
                insert(SkillVersion).values(
                    org_id=other.org,
                    skill_id=uuid.UUID(skill_id),
                    version_no=9,
                    name="my-notes",
                    description="x",
                    skill_md="x",
                    files={},
                    change="edit",
                    author_kind="agent",
                )
            )
    response = await client.get(f"/skills/{skill_id}/versions", headers=mia(tenant))
    assert [v["version"] for v in response.json()] == [1]


async def stored_texts(engine: AsyncEngine, session: uuid.UUID) -> list[str]:
    async with engine.connect() as connection:
        rows = await connection.execute(
            select(Message.content["text"].as_string())
            .where(Message.session_id == session, Message.role == "user")
            .order_by(Message.seq)
        )
        return list(rows.scalars())


async def test_a_skill_command_in_a_message_is_expanded(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    s1 = tenant.ids["s1"]
    for text in ("/summarize the report", "/not-a-skill hello", "/usr/bin/env", "hello"):
        sent = await client.post(
            f"/sessions/{s1}/messages", headers=mia(tenant), json={"text": text}
        )
        assert sent.status_code == 202
    texts = await stored_texts(engine, s1)
    assert texts[0].startswith("the report\n\n")
    assert 'load the skill "summarize"' in texts[0]
    assert texts[1:] == ["/not-a-skill hello", "/usr/bin/env", "hello"]


async def test_a_skill_command_needs_the_skills_toolset(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            update(AgentProfile)
            .where(AgentProfile.id == tenant.ids["profile"])
            .values(toolsets=["core"], group_toolsets=["core"])
        )
    s1 = tenant.ids["s1"]
    await client.post(
        f"/sessions/{s1}/messages", headers=mia(tenant), json={"text": "/summarize x"}
    )
    assert await stored_texts(engine, s1) == ["/summarize x"]


async def test_a_workspace_admin_skill_shows_in_the_list_for_members(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            update(Membership)
            .where(Membership.user_id == tenant.ids["mia"], Membership.workspace_id.is_not(None))
            .values(role="admin")
        )
    await write(engine, tenant, "s1", "team-guide", "workspace")
    zed = bearer(await add_user(engine, tenant, workspace_member=True))
    response = await client.get(
        f"/orgs/{tenant.org}/skills", headers=zed, params={"workspace_id": str(tenant.ids["ws"])}
    )
    skills = {s["name"]: s["scope"] for s in response.json()}
    assert skills["team-guide"] == "workspace"
