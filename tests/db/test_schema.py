"""Schema tests: the models match the migrations, and the constraints keep orgs apart."""

from __future__ import annotations

import uuid

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from sama.db.models import Base
from tests.db.world import World

pytestmark = pytest.mark.usefixtures("world")


async def execute_all(engine: AsyncEngine, *statements: str) -> None:
    async with engine.begin() as connection:
        for statement in statements:
            await connection.execute(text(statement))


def _diff(connection: Connection) -> list[object]:
    context = MigrationContext.configure(connection, opts={"compare_server_default": True})
    return list(compare_metadata(context, Base.metadata))


async def test_the_models_match_the_migrations(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        assert await connection.run_sync(_diff) == []


async def _insert_session(
    connection: AsyncConnection, world: World, *, org: str, workspace: str, profile: str
) -> None:
    await connection.execute(
        text(
            "INSERT INTO sessions (org_id, workspace_id, agent_profile_id, audience)"
            " VALUES (:org, :workspace, :profile, 'shared')"
        ),
        {"org": world.ids[org], "workspace": world.ids[workspace], "profile": world.ids[profile]},
    )


@pytest.mark.parametrize(
    ("workspace", "profile"),
    [("a1", "profile-b"), ("b1", "profile-a")],
)
async def test_a_row_cannot_link_to_another_org(
    engine: AsyncEngine, world: World, workspace: str, profile: str
) -> None:
    with pytest.raises(IntegrityError, match="foreign key"):
        async with engine.begin() as connection:
            await _insert_session(
                connection, world, org="org-b", workspace=workspace, profile=profile
            )


async def test_a_message_cannot_point_to_a_session_of_another_org(
    engine: AsyncEngine, world: World
) -> None:
    with pytest.raises(IntegrityError, match="foreign key"):
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO messages (org_id, session_id, role)"
                    " VALUES (:org, :session, 'user')"
                ),
                {"org": world.ids["org-b"], "session": world.ids["a1-shared"]},
            )


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO messages (org_id, session_id, run_id, role)"
        " VALUES (:org, :shared, :private_run, 'user')",
        "INSERT INTO run_events (org_id, session_id, run_id, seq, type)"
        " VALUES (:org, :shared, :private_run, 99, 'text_delta')",
        "INSERT INTO tool_calls (org_id, session_id, run_id, call_id, tool_name)"
        " VALUES (:org, :shared, :private_run, 'call-x', 'web_search')",
    ],
)
async def test_a_child_row_cannot_pair_a_run_with_another_session(
    engine: AsyncEngine, world: World, statement: str
) -> None:
    # The run of Alice's private session with a shared session of her workspace: the RLS
    # policy of the shared session would show the row to the whole workspace.
    params = {
        "org": world.ids["org-a"],
        "shared": world.ids["a1-shared"],
        "private_run": world.ids["run:a1-alice"],
    }
    with pytest.raises(IntegrityError, match="foreign key"):
        async with engine.begin() as connection:
            await connection.execute(text(statement), params)


async def test_a_binding_workspace_must_be_the_session_workspace(
    engine: AsyncEngine, world: World
) -> None:
    with pytest.raises(IntegrityError, match="foreign key"):
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO session_bindings (org_id, workspace_id, session_id, channel,"
                    " external_key) VALUES (:org, :workspace, :session, 'web', 'web:x')"
                ),
                {
                    "org": world.ids["org-a"],
                    "workspace": world.ids["a2"],
                    "session": world.ids["a1-shared"],
                },
            )


async def test_a_private_session_needs_an_owner(engine: AsyncEngine, world: World) -> None:
    with pytest.raises(IntegrityError, match="private_has_owner"):
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO sessions (org_id, workspace_id, agent_profile_id, audience)"
                    " VALUES (:org, :workspace, :profile, 'private')"
                ),
                {
                    "org": world.ids["org-a"],
                    "workspace": world.ids["a1"],
                    "profile": world.ids["profile-a"],
                },
            )


@pytest.mark.parametrize(
    "statement",
    ["UPDATE audit_log SET action = 'x'", "DELETE FROM audit_log", "TRUNCATE audit_log"],
)
async def test_the_audit_log_is_append_only(engine: AsyncEngine, statement: str) -> None:
    with pytest.raises(DBAPIError, match="append-only"):
        async with engine.begin() as connection:
            await connection.execute(text(statement))


@pytest.mark.parametrize("statement", ["UPDATE audit_log SET action = 'x'", "TRUNCATE audit_log"])
async def test_the_audit_purge_allows_only_row_deletes(engine: AsyncEngine, statement: str) -> None:
    with pytest.raises(DBAPIError, match="append-only"):
        await execute_all(engine, "SET LOCAL sama.audit_purge = 'on'", statement)


async def test_an_org_delete_removes_its_rows_with_the_audit_purge(
    engine: AsyncEngine, world: World
) -> None:
    org = world.ids["org-b"]
    async with engine.connect() as connection, connection.begin() as transaction:
        await connection.execute(text("SET LOCAL sama.audit_purge = 'on'"))
        await connection.execute(text("DELETE FROM orgs WHERE id = :org"), {"org": org})
        left = await connection.execute(
            text("SELECT count(*) FROM sessions WHERE org_id = :org"), {"org": org}
        )
        assert left.scalar_one() == 0
        await transaction.rollback()


async def test_an_org_delete_without_the_purge_fails(engine: AsyncEngine, world: World) -> None:
    with pytest.raises(DBAPIError, match="append-only"):
        async with engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM orgs WHERE id = :org"), {"org": world.ids["org-b"]}
            )


async def test_a_new_session_gets_defaults(engine: AsyncEngine, world: World) -> None:
    async with engine.connect() as connection, connection.begin() as transaction:
        await _insert_session(connection, world, org="org-a", workspace="a1", profile="profile-a")
        row = (
            await connection.execute(
                text("SELECT id, status, created_at FROM sessions ORDER BY created_at DESC LIMIT 1")
            )
        ).one()
        await transaction.rollback()
    assert isinstance(row.id, uuid.UUID)
    assert row.status == "active"
