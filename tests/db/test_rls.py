"""Row-level security tests (docs/architecture.md, Section 17.1).

They run as the Supabase role ``authenticated`` with the JWT claims of one user, the same way
as a request to the Supabase Data API.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from sama.db import as_user
from sama.db.models import Base
from tests.db.world import World

pytestmark = pytest.mark.usefixtures("world")

TABLES = sorted(Base.metadata.tables)
SESSION_CHILDREN = ("runs", "messages", "run_events", "tool_calls", "session_bindings")
INSUFFICIENT_PRIVILEGE = "42501"

# What each user can read. Session children are listed by their session.
VISIBLE: dict[str, dict[str, set[str]]] = {
    "alice": {
        "orgs": {"org-a"},
        "users": {"alice", "carol", "dave"},
        "workspaces": {"a1"},
        "agent_profiles": {"profile-a"},
        "sessions": {"a1-shared", "a1-alice"},
    },
    "carol": {
        "orgs": {"org-a"},
        "users": {"alice", "carol", "dave"},
        "workspaces": {"a1", "a2"},
        "agent_profiles": {"profile-a", "profile-a2"},
        "sessions": {"a1-shared", "a2-shared"},
    },
    "dave": {
        "orgs": {"org-a"},
        "users": {"alice", "carol", "dave"},
        "workspaces": set(),
        "agent_profiles": {"profile-a"},
        "sessions": set(),
    },
    "bob": {
        "orgs": {"org-b"},
        "users": {"bob"},
        "workspaces": {"b1"},
        "agent_profiles": {"profile-b"},
        "sessions": {"b1-shared", "b1-bob"},
    },
    "stranger": {
        "orgs": set(),
        "users": set(),
        "workspaces": set(),
        "agent_profiles": set(),
        "sessions": set(),
    },
}
AUDIT_ROWS = {"alice": 0, "carol": 1, "dave": 0, "bob": 1, "stranger": 0}
MEMBERSHIP_ROWS = {"alice": 4, "carol": 4, "dave": 4, "bob": 2, "stranger": 0}


def sqlstate(error: DBAPIError) -> str | None:
    return getattr(error.orig, "sqlstate", None)


async def run_as_role(engine: AsyncEngine, role: str, statement: str) -> None:
    async with engine.begin() as connection:
        await connection.execute(text(f"SET LOCAL ROLE {role}"))
        await connection.execute(text(statement))


async def labels(connection: AsyncConnection, world: World, table: str, column: str) -> set[str]:
    rows = await connection.execute(text(f"SELECT DISTINCT {column} FROM {table}"))  # noqa: S608
    return {world.label(value) for (value,) in rows}


async def count(connection: AsyncConnection, table: str, where: str = "true", **params: Any) -> int:
    query = text(f"SELECT count(*) FROM {table} WHERE {where}")  # noqa: S608
    return int((await connection.execute(query, params)).scalar_one())


@pytest.mark.parametrize("user", sorted(VISIBLE))
async def test_each_user_reads_only_the_rows_they_may_see(
    engine: AsyncEngine, world: World, user: str
) -> None:
    expected = VISIBLE[user]
    async with as_user(engine, world.auth[user]) as connection:
        for table, wanted in expected.items():
            assert await labels(connection, world, table, "id") == wanted, table
        for table in SESSION_CHILDREN:
            seen = await labels(connection, world, table, "session_id")
            assert seen == expected["sessions"], table
        assert await count(connection, "memberships") == MEMBERSHIP_ROWS[user]
        assert await count(connection, "audit_log") == AUDIT_ROWS[user]


@pytest.mark.parametrize("table", TABLES)
@pytest.mark.parametrize(
    ("user", "other_org"), [("alice", "org-b"), ("carol", "org-b"), ("bob", "org-a")]
)
async def test_no_user_reads_a_row_of_another_org(
    engine: AsyncEngine, world: World, table: str, user: str, other_org: str
) -> None:
    column = "id" if table == "orgs" else "org_id"
    async with as_user(engine, world.auth[user]) as connection:
        rows = await count(connection, table, f"{column} = :org", org=world.ids[other_org])
    assert rows == 0


async def test_the_owner_connection_sees_both_orgs(engine: AsyncEngine, world: World) -> None:
    async with engine.connect() as connection:
        orgs = await labels(connection, world, "orgs", "id")
    assert {"org-a", "org-b"} <= orgs


async def test_a_user_in_two_orgs_reads_both(engine: AsyncEngine, world: World) -> None:
    # One person, one auth user, one user row per org.
    async with engine.connect() as connection, connection.begin() as transaction:
        await connection.execute(
            text(
                "INSERT INTO users (id, org_id, auth_user_id, display_name)"
                " VALUES (:id, :org, :auth, 'alice in b')"
            ),
            {"id": uuid.uuid4(), "org": world.ids["org-b"], "auth": world.auth["alice"]},
        )
        await connection.execute(
            text(
                "INSERT INTO memberships (org_id, user_id, role)"
                " SELECT org_id, id, 'member' FROM users WHERE display_name = 'alice in b'"
            )
        )
        claims = f'{{"sub": "{world.auth["alice"]}", "role": "authenticated"}}'
        await connection.execute(
            text("SELECT set_config('request.jwt.claims', :claims, true)"), {"claims": claims}
        )
        await connection.execute(text("SET LOCAL ROLE authenticated"))
        orgs = await labels(connection, world, "orgs", "id")
        sessions = await labels(connection, world, "sessions", "id")
        await transaction.rollback()
    assert orgs == {"org-a", "org-b"}
    # Alice has no workspace in org B and does not own b1-bob.
    assert sessions == {"a1-shared", "a1-alice"}


@pytest.mark.parametrize("table", TABLES)
async def test_anon_cannot_read(engine: AsyncEngine, table: str) -> None:
    with pytest.raises(DBAPIError) as error:
        await run_as_role(engine, "anon", f"SELECT 1 FROM {table} LIMIT 1")  # noqa: S608
    assert sqlstate(error.value) == INSUFFICIENT_PRIVILEGE


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO orgs (name, slug) VALUES ('x', 'x')",
        "UPDATE sessions SET title = 'changed'",
        "DELETE FROM messages",
        "INSERT INTO memberships (org_id, user_id, role)"
        " SELECT org_id, id, 'owner' FROM users LIMIT 1",
    ],
)
async def test_authenticated_cannot_write(
    engine: AsyncEngine, world: World, statement: str
) -> None:
    with pytest.raises(DBAPIError) as error:
        async with as_user(engine, world.auth["carol"]) as connection:
            await connection.execute(text(statement))
    assert sqlstate(error.value) == INSUFFICIENT_PRIVILEGE


@pytest.mark.parametrize("role", ["anon", "authenticated"])
async def test_the_migration_version_table_is_private(engine: AsyncEngine, role: str) -> None:
    with pytest.raises(DBAPIError) as error:
        await run_as_role(engine, role, "SELECT * FROM sama_private.alembic_version")
    assert sqlstate(error.value) == INSUFFICIENT_PRIVILEGE


async def test_anon_cannot_call_the_policy_functions(engine: AsyncEngine) -> None:
    with pytest.raises(DBAPIError) as error:
        await run_as_role(engine, "anon", "SELECT sama_private.is_org_member(gen_random_uuid())")
    assert sqlstate(error.value) == INSUFFICIENT_PRIVILEGE


async def test_every_public_table_has_rls_and_a_policy(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        rows = await connection.execute(
            text(
                "SELECT c.relname, c.relrowsecurity,"
                " (SELECT count(*) FROM pg_policies p"
                "  WHERE p.schemaname = 'public' AND p.tablename = c.relname)"
                " FROM pg_class c WHERE c.relnamespace = 'public'::regnamespace"
                " AND c.relkind IN ('r', 'p')"
            )
        )
        tables = {name: (rls, policies) for name, rls, policies in rows}
    assert set(tables) == set(TABLES)
    assert all(rls and policies > 0 for rls, policies in tables.values()), tables
