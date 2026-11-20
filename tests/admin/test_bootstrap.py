from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.admin.__main__ import main
from sama.admin.bootstrap import BootstrapError, Owner, bootstrap
from sama.db.models import AgentProfile, Membership, Org, User, Workspace
from sama.tenancy.access import load_grants


@pytest.fixture
async def slug(engine: AsyncEngine) -> AsyncIterator[str]:
    slug = f"boot-{uuid.uuid4().hex[:8]}"
    yield slug
    async with engine.begin() as connection:
        await connection.execute(text("SET LOCAL sama.audit_purge = 'on'"))
        await connection.execute(delete(Org).where(Org.slug == slug))


async def test_a_second_run_adds_nothing(engine: AsyncEngine, slug: str) -> None:
    owner = Owner(email="Ana@example.test", display_name="Ana", auth_user_id=uuid.uuid4())
    first = await bootstrap(engine, org_slug=slug, org_name="Team", owner=owner)
    assert first.added == ["org", "user", "membership", "workspace", "agent_profile"]
    second = await bootstrap(engine, org_slug=slug, org_name="Team", owner=owner)
    assert second.added == []
    assert (second.org_id, second.user_id, second.workspace_id, second.agent_profile_id) == (
        first.org_id,
        first.user_id,
        first.workspace_id,
        first.agent_profile_id,
    )

    async with engine.connect() as connection:
        role = await connection.scalar(
            select(Membership.role).where(Membership.user_id == first.user_id)
        )
        profile = (
            await connection.execute(
                select(AgentProfile.workspace_id, AgentProfile.model, AgentProfile.toolsets).where(
                    AgentProfile.id == first.agent_profile_id
                )
            )
        ).one()
        grants = await load_grants(connection, first.org_id, first.user_id)
    assert role == "owner"
    assert profile == (
        None,
        "claude-sonnet-5-5",
        ["core", "todo", "web", "clarify", "github", "memory", "skills", "sessions", "media"],
    )
    assert grants.sees_workspace(first.workspace_id)


async def test_a_second_owner_joins_the_same_org(engine: AsyncEngine, slug: str) -> None:
    first = await bootstrap(
        engine, org_slug=slug, org_name="Team", owner=Owner("a@x.test", "A", uuid.uuid4())
    )
    second = await bootstrap(
        engine, org_slug=slug, org_name="Team", owner=Owner("b@x.test", "B", uuid.uuid4())
    )
    assert second.added == ["user", "membership"]
    assert second.org_id == first.org_id
    assert second.user_id != first.user_id
    async with engine.connect() as connection:
        users = await connection.scalar(
            select(func.count()).select_from(User).where(User.org_id == first.org_id)
        )
        workspaces = await connection.scalar(
            select(func.count()).select_from(Workspace).where(Workspace.org_id == first.org_id)
        )
    assert (users, workspaces) == (2, 1)


async def test_an_email_finds_the_auth_user(engine: AsyncEngine, slug: str) -> None:
    auth_id = uuid.uuid4()
    async with engine.begin() as connection:
        await connection.execute(
            text("CREATE TABLE IF NOT EXISTS auth.users (id uuid PRIMARY KEY, email text)")
        )
        await connection.execute(
            text("INSERT INTO auth.users (id, email) VALUES (:id, :email)"),
            {"id": auth_id, "email": f"{slug}@example.test"},
        )
    try:
        result = await bootstrap(
            engine,
            org_slug=slug,
            org_name="Team",
            owner=Owner(f"{slug.upper()}@example.test", "Ana"),
        )
        async with engine.connect() as connection:
            found = await connection.scalar(
                select(User.auth_user_id).where(User.id == result.user_id)
            )
        assert found == auth_id
        with pytest.raises(BootstrapError, match="no Supabase Auth user"):
            await bootstrap(
                engine, org_slug=slug, org_name="Team", owner=Owner("nobody@x.test", "N")
            )
    finally:
        async with engine.begin() as connection:
            await connection.execute(text("DELETE FROM auth.users WHERE id = :id"), {"id": auth_id})


def test_the_command_prints_the_ids(
    queue_database_url: str,
    slug: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("SAMA_DATABASE_URL", queue_database_url)
    code = main(
        [
            "bootstrap",
            "--org-slug",
            slug,
            "--email",
            "ana@example.test",
            "--auth-user-id",
            str(uuid.uuid4()),
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert '"added": ["org", "user", "membership", "workspace", "agent_profile"]' in out
