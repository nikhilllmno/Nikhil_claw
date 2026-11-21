"""Migration 0040: the role permissions, the delivery log of the triggers, and the reasoning
effort of the profiles, with their RLS (docs/architecture.md, Sections 12.2 and 17.2; roadmap
M7.10)."""

from __future__ import annotations

import asyncio
import uuid

import asyncpg
import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.db.engine import as_user
from tests.db.conftest import alembic_config, migrated_database
from tests.db.world import World

TABLES = ("org_role_permissions", "trigger_deliveries")


async def _state(url: str) -> tuple[bool, bool, bool]:
    connection = await asyncpg.connect(url)
    try:
        permissions = bool(
            await connection.fetchval("SELECT to_regclass('public.org_role_permissions')")
        )
        deliveries = bool(
            await connection.fetchval("SELECT to_regclass('public.trigger_deliveries')")
        )
        column = bool(
            await connection.fetchval(
                "SELECT 1 FROM information_schema.columns"
                " WHERE table_name = 'agent_profiles' AND column_name = 'reasoning_effort'"
            )
        )
    finally:
        await connection.close()
    return permissions, deliveries, column


def test_the_upgrade_adds_the_tables_and_the_downgrade_removes_them() -> None:
    with migrated_database() as url:
        assert asyncio.run(_state(url)) == (True, True, True)
        config = alembic_config(url)
        command.downgrade(config, "0039")
        assert asyncio.run(_state(url)) == (False, False, False)
        command.upgrade(config, "head")
        assert asyncio.run(_state(url)) == (True, True, True)


async def _add_permission(
    engine: AsyncEngine, world: World, org: str, role: str, permission: str, allowed: bool
) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO org_role_permissions (org_id, role, permission, allowed)"
                " VALUES (:org, :role, :permission, :allowed)"
            ),
            {"org": world.ids[org], "role": role, "permission": permission, "allowed": allowed},
        )


async def _add_delivery(
    engine: AsyncEngine, world: World, trigger: str, outcome: str = "accepted", **extra: object
) -> uuid.UUID:
    row_id = uuid.uuid4()
    org = "org-b" if trigger.startswith("trig:b") else "org-a"
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO trigger_deliveries (id, org_id, trigger_id, outcome, http_status)"
                " VALUES (:id, :org, :trigger, :outcome, 202)"
            ),
            {
                "id": row_id,
                "org": world.ids[org],
                "trigger": world.ids[trigger],
                "outcome": outcome,
            },
        )
    return row_id


async def _ids(engine: AsyncEngine, world: World, user: str, table: str) -> int:
    async with as_user(engine, world.auth[user]) as connection:
        return len((await connection.execute(text(f"SELECT id FROM {table}"))).all())  # noqa: S608


async def test_a_member_reads_the_permissions_of_the_org_and_no_one_writes(
    engine: AsyncEngine, fresh: World
) -> None:
    await _add_permission(engine, fresh, "org-a", "member", "jobs.create", False)
    await _add_permission(engine, fresh, "org-b", "admin", "agents.manage", False)

    assert await _ids(engine, fresh, "alice", "org_role_permissions") == 1
    assert await _ids(engine, fresh, "dave", "org_role_permissions") == 1
    assert await _ids(engine, fresh, "carol", "org_role_permissions") == 1
    assert await _ids(engine, fresh, "bob", "org_role_permissions") == 1
    assert await _ids(engine, fresh, "stranger", "org_role_permissions") == 0
    params = {"org": fresh.ids["org-a"]}
    for statement in (
        "INSERT INTO org_role_permissions (org_id, role, permission, allowed)"
        " VALUES (:org, 'member', 'skills.write', false)",
        "UPDATE org_role_permissions SET allowed = true",
        "DELETE FROM org_role_permissions",
    ):
        async with as_user(engine, fresh.auth["carol"]) as connection:
            with pytest.raises(DBAPIError, match="permission denied"):
                await connection.execute(text(statement), params if ":org" in statement else {})


async def test_the_permission_rows_are_unique_and_checked(
    engine: AsyncEngine, fresh: World
) -> None:
    await _add_permission(engine, fresh, "org-a", "member", "jobs.create", False)
    with pytest.raises(DBAPIError, match="uq_org_role_permissions"):
        await _add_permission(engine, fresh, "org-a", "member", "jobs.create", True)
    with pytest.raises(DBAPIError, match="ck_org_role_permissions_role"):
        await _add_permission(engine, fresh, "org-a", "owner", "jobs.create", True)
    with pytest.raises(DBAPIError, match="ck_org_role_permissions_permission_length"):
        await _add_permission(engine, fresh, "org-a", "admin", "", True)
    # The same key for the other role and the other org is fine.
    await _add_permission(engine, fresh, "org-a", "admin", "jobs.create", False)
    await _add_permission(engine, fresh, "org-b", "member", "jobs.create", False)


async def test_a_user_who_sees_the_workspace_reads_the_deliveries_of_its_trigger(
    engine: AsyncEngine, fresh: World
) -> None:
    await _add_delivery(engine, fresh, "trig:a1")
    await _add_delivery(engine, fresh, "trig:a1", "bad_signature")
    await _add_delivery(engine, fresh, "trig:a2")
    await _add_delivery(engine, fresh, "trig:b1")

    # alice is a member of the workspace a1, carol is an org admin, and dave is in no workspace.
    assert await _ids(engine, fresh, "alice", "trigger_deliveries") == 2
    assert await _ids(engine, fresh, "carol", "trigger_deliveries") == 3
    assert await _ids(engine, fresh, "dave", "trigger_deliveries") == 0
    assert await _ids(engine, fresh, "bob", "trigger_deliveries") == 1
    assert await _ids(engine, fresh, "stranger", "trigger_deliveries") == 0
    params = {"org": fresh.ids["org-a"], "trigger": fresh.ids["trig:a1"]}
    for statement in (
        "INSERT INTO trigger_deliveries (org_id, trigger_id, outcome, http_status)"
        " VALUES (:org, :trigger, 'accepted', 202)",
        "UPDATE trigger_deliveries SET error = 'x'",
        "DELETE FROM trigger_deliveries",
    ):
        async with as_user(engine, fresh.auth["carol"]) as connection:
            with pytest.raises(DBAPIError, match="permission denied"):
                await connection.execute(text(statement), params if ":org" in statement else {})


async def test_the_delivery_rows_are_checked_and_go_with_their_trigger(
    engine: AsyncEngine, fresh: World
) -> None:
    for outcome in (
        "accepted",
        "duplicate",
        "bad_signature",
        "disabled",
        "bad_body",
        "rate_limited",
        "rejected",
        "test_ok",
    ):
        await _add_delivery(engine, fresh, "trig:a1", outcome)
    with pytest.raises(DBAPIError, match="ck_trigger_deliveries_outcome"):
        await _add_delivery(engine, fresh, "trig:a1", "lost")
    long_id = "x" * 201
    async with engine.begin() as connection:
        with pytest.raises(DBAPIError, match="ck_trigger_deliveries_delivery_id_length"):
            await connection.execute(
                text(
                    "INSERT INTO trigger_deliveries (org_id, trigger_id, outcome, http_status,"
                    " delivery_id) VALUES (:org, :trigger, 'accepted', 202, :delivery)"
                ),
                {"org": fresh.ids["org-a"], "trigger": fresh.ids["trig:a1"], "delivery": long_id},
            )
    async with engine.begin() as connection:
        with pytest.raises(DBAPIError, match="ck_trigger_deliveries_error_length"):
            await connection.execute(
                text(
                    "INSERT INTO trigger_deliveries (org_id, trigger_id, outcome, http_status,"
                    " error) VALUES (:org, :trigger, 'rejected', 400, :error)"
                ),
                {"org": fresh.ids["org-a"], "trigger": fresh.ids["trig:a1"], "error": "e" * 501},
            )
    # A trigger of another org does not fit the row.
    async with engine.begin() as connection:
        with pytest.raises(DBAPIError, match="fk_trigger_deliveries_org_id_trigger_id_triggers"):
            await connection.execute(
                text(
                    "INSERT INTO trigger_deliveries (org_id, trigger_id, outcome, http_status)"
                    " VALUES (:org, :trigger, 'accepted', 202)"
                ),
                {"org": fresh.ids["org-b"], "trigger": fresh.ids["trig:a1"]},
            )
    async with engine.begin() as connection:
        await connection.execute(
            text("DELETE FROM triggers WHERE id = :id"), {"id": fresh.ids["trig:a1"]}
        )
        left = await connection.scalar(text("SELECT count(*) FROM trigger_deliveries"))
    assert left == 0


async def test_the_reasoning_effort_of_a_profile_is_low_medium_high_or_null(
    engine: AsyncEngine, fresh: World
) -> None:
    profile = fresh.ids["profile-a"]
    for value in ("low", "medium", "high", None):
        async with engine.begin() as connection:
            await connection.execute(
                text("UPDATE agent_profiles SET reasoning_effort = :value WHERE id = :id"),
                {"value": value, "id": profile},
            )
    async with engine.begin() as connection:
        with pytest.raises(DBAPIError, match="ck_agent_profiles_reasoning_effort"):
            await connection.execute(
                text("UPDATE agent_profiles SET reasoning_effort = 'max' WHERE id = :id"),
                {"id": profile},
            )
