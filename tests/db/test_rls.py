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
from sqlalchemy.pool import AsyncAdaptedQueuePool

from sama.db import as_user, create_engine
from sama.db.models import Base
from tests.db.world import World

pytestmark = pytest.mark.usefixtures("world")

TABLES = sorted(Base.metadata.tables)
COLUMNS = "attachments"
"""The table whose grant is on columns: the bytes of an image are not readable."""
SESSION_CHILDREN = (
    "runs",
    "messages",
    "message_embeddings",
    "attachments",
    "run_events",
    "tool_calls",
    "session_bindings",
    "model_calls",
    "session_todos",
    "approvals",
    "review_proposals",
    "session_compactions",
    "context_pins",
)
INSUFFICIENT_PRIVILEGE = "42501"

# What each user can read. Session children are listed by their session.
VISIBLE: dict[str, dict[str, set[str]]] = {
    "alice": {
        "orgs": {"org-a"},
        "users": {"alice", "carol", "dave"},
        "workspaces": {"a1"},
        "agent_profiles": {"profile-a"},
        "sessions": {"a1-shared", "a1-alice"},
        "context_files": {"file:org-a"},
        "channel_identities": {"tg:alice"},
        "channel_groups": {"group:a1"},
        "worker_bots": {"bot:a1"},
        "pairing_codes": {"code:alice"},
        "integrations": {"int:org-a"},
        "connections": {"conn:a1", "conn:a1-alice"},
        "memories": {"mem:alice", "mem:a1", "mem:a1-agent"},
        "memory_revisions": {"rev:mem:alice", "rev:mem:a1", "rev:mem:a1-agent"},
    },
    "carol": {
        "orgs": {"org-a"},
        "users": {"alice", "carol", "dave"},
        "workspaces": {"a1", "a2"},
        "agent_profiles": {"profile-a", "profile-a2"},
        "sessions": {"a1-shared", "a2-shared"},
        "context_files": {"file:org-a", "file:a2", "file:profile-a2"},
        "channel_identities": set(),
        "channel_groups": {"group:a1"},
        "worker_bots": {"bot:a1"},
        "pairing_codes": set(),
        "integrations": {"int:org-a"},
        "connections": {"conn:a1", "conn:a2"},
        "memories": {"mem:a1", "mem:a2", "mem:a1-agent"},
        "memory_revisions": {"rev:mem:a1", "rev:mem:a2", "rev:mem:a1-agent"},
    },
    "dave": {
        "orgs": {"org-a"},
        "users": {"alice", "carol", "dave"},
        "workspaces": set(),
        "agent_profiles": {"profile-a"},
        "sessions": set(),
        "context_files": {"file:org-a"},
        "channel_identities": set(),
        "channel_groups": set(),
        "worker_bots": set(),
        "pairing_codes": set(),
        "integrations": {"int:org-a"},
        "connections": set(),
        "memories": {"mem:dave"},
        "memory_revisions": {"rev:mem:dave"},
    },
    "bob": {
        "orgs": {"org-b"},
        "users": {"bob"},
        "workspaces": {"b1"},
        "agent_profiles": {"profile-b"},
        "sessions": {"b1-shared", "b1-bob"},
        "context_files": {"file:org-b"},
        "channel_identities": {"tg:bob"},
        "channel_groups": {"group:b1"},
        "worker_bots": {"bot:b1"},
        "pairing_codes": {"code:bob"},
        "integrations": {"int:org-b"},
        "connections": {"conn:b1", "conn:b1-bob"},
        "memories": {"mem:bob", "mem:b1"},
        "memory_revisions": {"rev:mem:bob", "rev:mem:b1"},
    },
    "stranger": {
        "orgs": set(),
        "users": set(),
        "workspaces": set(),
        "agent_profiles": set(),
        "sessions": set(),
        "context_files": set(),
        "channel_identities": set(),
        "channel_groups": set(),
        "worker_bots": set(),
        "pairing_codes": set(),
        "integrations": set(),
        "connections": set(),
        "memories": set(),
        "memory_revisions": set(),
    },
}
# Org rules for every member; a personal rule for its user and the admins.
TOOL_POLICIES = {
    "alice": {"rule:org-a", "rule:alice"},
    "carol": {"rule:org-a", "rule:alice", "rule:dave"},
    "dave": {"rule:org-a", "rule:dave"},
    "bob": {"rule:org-b"},
    "stranger": set(),
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
        # Only org admins see which vault secrets exist, like the audit log.
        assert await count(connection, "vault_secrets") == AUDIT_ROWS[user]
        assert await labels(connection, world, "tool_policies", "id") == TOOL_POLICIES[user]
        # A task card follows the visibility of a session with the same audience and owner.
        cards = {f"task:{session}" for session in expected["sessions"]}
        assert await labels(connection, world, "tasks", "id") == cards
        assert await labels(connection, world, "task_comments", "task_id") == cards


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


async def read_after(
    engine: AsyncEngine, world: World, user: str, *changes: str
) -> dict[str, set[str] | int]:
    """What ``user`` reads after ``changes`` (run as the owner). Rolls everything back."""
    async with engine.connect() as connection, connection.begin() as transaction:
        for change in changes:
            await connection.execute(
                text(change),
                {"alice": world.ids["alice"], "carol": world.ids["carol"], "a1": world.ids["a1"]},
            )
        claims = f'{{"sub": "{world.auth[user]}", "role": "authenticated"}}'
        await connection.execute(
            text("SELECT set_config('request.jwt.claims', :claims, true)"), {"claims": claims}
        )
        await connection.execute(text("SET LOCAL ROLE authenticated"))
        seen: dict[str, set[str] | int] = {
            table: await labels(connection, world, table, "id")
            for table in ("orgs", "workspaces", "sessions")
        }
        seen["messages"] = await labels(connection, world, "messages", "session_id")
        seen["audit_log"] = await count(connection, "audit_log")
        seen["vault_secrets"] = await count(connection, "vault_secrets")
        await transaction.rollback()
    return seen


async def test_losing_the_workspace_hides_its_sessions(engine: AsyncEngine, world: World) -> None:
    seen = await read_after(
        engine,
        world,
        "alice",
        "DELETE FROM memberships WHERE user_id = :alice AND workspace_id = :a1",
    )
    # Also her own private session: it is in a workspace she can no longer see.
    assert seen["workspaces"] == set()
    assert seen["sessions"] == set()
    assert seen["messages"] == set()
    assert seen["orgs"] == {"org-a"}


async def test_losing_the_org_hides_everything(engine: AsyncEngine, world: World) -> None:
    seen = await read_after(
        engine,
        world,
        "alice",
        "DELETE FROM memberships WHERE user_id = :alice AND workspace_id IS NULL",
    )
    assert seen == {
        "orgs": set(),
        "workspaces": set(),
        "sessions": set(),
        "messages": set(),
        "audit_log": 0,
        "vault_secrets": 0,
    }


async def test_losing_the_admin_role_hides_other_workspaces_and_the_audit_log(
    engine: AsyncEngine, world: World
) -> None:
    seen = await read_after(
        engine, world, "carol", "UPDATE memberships SET role = 'member' WHERE user_id = :carol"
    )
    assert seen["workspaces"] == set()
    assert seen["sessions"] == set()
    assert seen["audit_log"] == 0
    assert seen["vault_secrets"] == 0


async def test_a_pooled_connection_does_not_keep_the_user(database_url: str, world: World) -> None:
    engine = create_engine(
        database_url, poolclass=AsyncAdaptedQueuePool, pool_size=1, max_overflow=0
    )
    try:
        async with as_user(engine, world.auth["alice"]) as connection:
            assert await labels(connection, world, "orgs", "id") == {"org-a"}
        with pytest.raises(DBAPIError):
            async with as_user(engine, world.auth["bob"]) as connection:
                await connection.execute(text("SELECT 1 / 0"))
        async with engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        "SELECT current_user, session_user,"
                        " coalesce(current_setting('request.jwt.claims', true), '')"
                    )
                )
            ).one()
            assert await labels(connection, world, "orgs", "id") >= {"org-a", "org-b"}
        assert row[0] == row[1]
        assert row[2] == ""
        async with as_user(engine, world.auth["bob"]) as connection:
            assert await labels(connection, world, "orgs", "id") == {"org-b"}
    finally:
        await engine.dispose()


async def test_only_authenticated_select_is_granted(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        rows = await connection.execute(
            text(
                "SELECT grantee, table_name, privilege_type"
                " FROM information_schema.role_table_grants"
                " WHERE table_schema = 'public' AND grantee IN ('anon', 'authenticated', 'PUBLIC')"
            )
        )
        grants = {(grantee, table, privilege) for grantee, table, privilege in rows}
    # ``attachments`` has a grant on columns only: the table grant is absent on purpose.
    assert grants == {("authenticated", table, "SELECT") for table in TABLES if table != COLUMNS}


async def test_nobody_reads_the_bytes_of_an_image_through_the_data_api(
    engine: AsyncEngine, world: World
) -> None:
    async with engine.connect() as connection:
        rows = await connection.execute(
            text(
                "SELECT grantee, column_name, privilege_type"
                " FROM information_schema.column_privileges"
                " WHERE table_schema = 'public' AND table_name = 'attachments'"
                " AND grantee IN ('anon', 'authenticated', 'PUBLIC')"
            )
        )
        columns = {(grantee, column, privilege) for grantee, column, privilege in rows}
    assert columns
    assert {grantee for grantee, _, _ in columns} == {"authenticated"}
    assert {privilege for _, _, privilege in columns} == {"SELECT"}
    assert "data" not in {column for _, column, _ in columns}
    async with as_user(engine, world.auth["alice"]) as connection:
        assert await count(connection, "attachments") >= 1
        with pytest.raises(DBAPIError) as error:
            await connection.execute(text("SELECT data FROM attachments"))
    assert sqlstate(error.value) == "42501"


async def test_new_objects_in_public_are_closed_by_default(engine: AsyncEngine) -> None:
    async with engine.connect() as connection, connection.begin() as transaction:
        await connection.execute(text("CREATE TABLE public.later_table (id int)"))
        await connection.execute(text("CREATE VIEW public.later_view AS SELECT 1 AS one"))
        await connection.execute(text("CREATE SEQUENCE public.later_sequence"))
        rows = await connection.execute(
            text(
                "SELECT r.rolname, o.name, has_table_privilege(r.rolname, o.name, 'SELECT'),"
                " has_table_privilege(r.rolname, o.name, 'INSERT')"
                " FROM (VALUES ('anon'), ('authenticated')) AS r(rolname),"
                " (VALUES ('public.later_table'), ('public.later_view')) AS o(name)"
            )
        )
        tables = [tuple(row) for row in rows]
        sequence = (
            await connection.execute(
                text(
                    "SELECT has_sequence_privilege('anon', 'public.later_sequence', 'USAGE'),"
                    " has_sequence_privilege('authenticated', 'public.later_sequence', 'USAGE')"
                )
            )
        ).one()
        await transaction.rollback()
    assert all(not select and not insert for _, _, select, insert in tables), tables
    assert tuple(sequence) == (False, False)


async def test_a_review_proposal_follows_the_visibility_of_its_session(
    engine: AsyncEngine, world: World
) -> None:
    """A proposal of a private session is for its owner only. A proposal of a shared session is
    for the members of the workspace. Nobody writes it through the Data API."""
    async with as_user(engine, world.auth["alice"]) as connection:
        assert await labels(connection, world, "review_proposals", "id") == {
            "proposal:a1-shared",
            "proposal:a1-alice",
        }
    async with as_user(engine, world.auth["dave"]) as connection:
        assert await labels(connection, world, "review_proposals", "id") == set(), (
            "dave is not in the workspace a1"
        )
    async with as_user(engine, world.auth["carol"]) as connection:
        assert await labels(connection, world, "review_proposals", "id") == {
            "proposal:a1-shared",
            "proposal:a2-shared",
        }, "an org admin does not read the private session of a member"
    async with as_user(engine, world.auth["alice"]) as connection:
        with pytest.raises(DBAPIError) as error:
            await connection.execute(text("UPDATE review_proposals SET status = 'applied'"))
    assert sqlstate(error.value) == INSUFFICIENT_PRIVILEGE
