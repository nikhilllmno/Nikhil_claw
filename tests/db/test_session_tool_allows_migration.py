"""Migration 0049: the approval scope ``session`` (docs/architecture.md, Section 16.1)."""

from __future__ import annotations

import asyncio
import uuid

import asyncpg
import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.db import as_user
from tests.db.conftest import alembic_config, migrated_database
from tests.db.world import World, seed

TABLE = "session_tool_allows"


async def _fetch(url: str, query: str, *args: object) -> list[asyncpg.Record]:
    connection = await asyncpg.connect(url)
    try:
        return await connection.fetch(query, *args)
    finally:
        await connection.close()


def _policies(url: str) -> list[str]:
    rows = asyncio.run(
        _fetch(url, "SELECT policyname FROM pg_policies WHERE tablename = $1", TABLE)
    )
    return sorted(row["policyname"] for row in rows)


def _human_origin(url: str) -> list[tuple[str, str, str]]:
    rows = asyncio.run(
        _fetch(
            url,
            "SELECT data_type, is_nullable, column_default FROM information_schema.columns"
            " WHERE table_schema = 'public' AND table_name = 'runs'"
            " AND column_name = 'human_origin'",
        )
    )
    return [(row["data_type"], row["is_nullable"], row["column_default"]) for row in rows]


def test_the_upgrade_adds_the_table_with_rls_and_the_downgrade_removes_it() -> None:
    with migrated_database() as url:
        asyncio.run(seed(url))
        assert _policies(url) == [f"{TABLE}_select"]
        assert _human_origin(url) == [("boolean", "NO", "false")]
        config = alembic_config(url)
        command.downgrade(config, "0048")
        assert _human_origin(url) == []
        assert (
            asyncio.run(_fetch(url, "SELECT to_regclass('public.session_tool_allows')"))[0][0]
            is None
        )
        command.upgrade(config, "head")
        assert _policies(url) == [f"{TABLE}_select"]


@pytest.mark.usefixtures("world")
async def test_a_member_reads_the_rules_of_a_visible_session_and_cannot_write(
    engine: AsyncEngine, world: World
) -> None:
    async with as_user(engine, world.auth["alice"]) as connection:
        rows = (
            await connection.execute(text("SELECT session_id FROM session_tool_allows"))
        ).scalars()
        assert {world.label(value) for value in rows} == {"a1-shared", "a1-alice"}
    for statement in (
        "INSERT INTO session_tool_allows (org_id, session_id, user_id, tool)"
        " SELECT org_id, id, owner_user_id, 'bash' FROM sessions WHERE owner_user_id IS NOT NULL",
        "UPDATE session_tool_allows SET tool = 'x'",
        "DELETE FROM session_tool_allows",
    ):
        with pytest.raises(DBAPIError) as caught:
            async with as_user(engine, world.auth["alice"]) as connection:
                await connection.execute(text(statement))
        assert "permission denied" in str(caught.value.orig)


@pytest.mark.usefixtures("world")
async def test_a_rule_cannot_link_a_session_of_another_org(
    engine: AsyncEngine, world: World
) -> None:
    with pytest.raises(IntegrityError, match="foreign key"):
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO session_tool_allows (org_id, session_id, user_id, tool)"
                    " VALUES (:org, :session, :user, 'bash')"
                ),
                {
                    "org": world.ids["org-b"],
                    "session": world.ids["a1-shared"],
                    "user": world.ids["bob"],
                },
            )
    with pytest.raises(IntegrityError, match="foreign key"):
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO session_tool_allows (org_id, session_id, user_id, tool)"
                    " VALUES (:org, :session, :user, 'bash')"
                ),
                {
                    "org": world.ids["org-a"],
                    "session": world.ids["a1-shared"],
                    "user": world.ids["bob"],
                },
            )


@pytest.mark.usefixtures("world")
async def test_one_rule_for_each_session_user_and_tool(engine: AsyncEngine, world: World) -> None:
    with pytest.raises(IntegrityError, match="uq_session_tool_allows"):
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO session_tool_allows (org_id, session_id, user_id, tool)"
                    " VALUES (:org, :session, :user, 'web_search')"
                ),
                {
                    "org": world.ids["org-a"],
                    "session": world.ids["a1-shared"],
                    "user": world.ids["alice"],
                },
            )


@pytest.mark.usefixtures("world")
async def test_the_rules_go_with_the_session(engine: AsyncEngine, world: World) -> None:
    session = uuid.uuid4()
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO sessions (id, org_id, workspace_id, agent_profile_id, audience)"
                " VALUES (:id, :org, :ws, :profile, 'shared')"
            ),
            {
                "id": session,
                "org": world.ids["org-a"],
                "ws": world.ids["a1"],
                "profile": world.ids["profile-a"],
            },
        )
        await connection.execute(
            text(
                "INSERT INTO session_tool_allows (org_id, session_id, user_id, tool)"
                " VALUES (:org, :id, :user, 'bash')"
            ),
            {"org": world.ids["org-a"], "id": session, "user": world.ids["alice"]},
        )
        await connection.execute(text("DELETE FROM sessions WHERE id = :id"), {"id": session})
        count = await connection.execute(
            text("SELECT count(*) FROM session_tool_allows WHERE session_id = :id"),
            {"id": session},
        )
        assert count.scalar_one() == 0
