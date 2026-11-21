"""Migration 0032: the curator (docs/architecture.md, Section 9.7)."""

from __future__ import annotations

import asyncio
import uuid

import asyncpg
from alembic import command

from tests.db.conftest import alembic_config, migrated_database
from tests.db.world import World, seed

SCHEDULE = '{"kind": "cron", "expr": "0 0 * * 1", "tz": "UTC"}'
SKILL_COLUMNS = ("pinned", "view_count", "use_count", "patch_count", "last_used_at", "stale_since")


async def _run(url: str, *queries: tuple[str, tuple[object, ...]]) -> list[list[asyncpg.Record]]:
    connection = await asyncpg.connect(url)
    try:
        return [await connection.fetch(query, *args) for query, args in queries]
    finally:
        await connection.close()


def _columns(url: str) -> set[str]:
    [rows] = asyncio.run(
        _run(
            url,
            (
                "SELECT column_name FROM information_schema.columns"
                " WHERE table_schema = 'public' AND table_name = 'skills'",
                (),
            ),
        )
    )
    return {row["column_name"] for row in rows}


def _add_rows(url: str, world: World) -> uuid.UUID:
    skill = uuid.uuid4()
    org, ws = world.ids["org-a"], world.ids["a1"]
    asyncio.run(
        _run(
            url,
            (
                "INSERT INTO skills (id, org_id, scope, workspace_id, name, current_version,"
                " status, stale_since) VALUES ($1, $2, 'workspace', $3, 'tips', 2, 'stale', now())",
                (skill, org, ws),
            ),
            (
                "INSERT INTO skill_versions (org_id, skill_id, version_no, name, description,"
                " skill_md, change, author_kind) VALUES"
                " ($1, $2, 1, 'tips', 'Tips', 'x', 'create', 'agent'),"
                " ($1, $2, 2, 'tips', 'Tips', 'x', 'restore', 'user')",
                (org, skill),
            ),
            (
                "INSERT INTO jobs (org_id, workspace_id, owner_user_id, agent_profile_id, name,"
                " schedule, prompt, mode, created_by_kind)"
                " VALUES ($1, $2, $3, $4, 'Curator', $5::jsonb, '', 'curator', 'system')",
                (org, ws, world.ids["alice"], world.ids["profile-a"], SCHEDULE),
            ),
        )
    )
    return skill


def test_the_upgrade_adds_the_curator_columns_and_the_downgrade_keeps_the_rows() -> None:
    with migrated_database() as url:
        world = asyncio.run(seed(url))
        assert set(SKILL_COLUMNS) <= _columns(url)
        skill = _add_rows(url, world)
        [jobs_before] = asyncio.run(_run(url, ("SELECT mode FROM jobs", ())))
        config = alembic_config(url)
        command.downgrade(config, "0031")
        assert not set(SKILL_COLUMNS) & _columns(url)
        status, changes, jobs = asyncio.run(
            _run(
                url,
                ("SELECT status FROM skills WHERE id = $1", (skill,)),
                (
                    "SELECT change FROM skill_versions WHERE skill_id = $1 ORDER BY version_no",
                    (skill,),
                ),
                ("SELECT mode FROM jobs", ()),
            )
        )
        assert [row["status"] for row in status] == ["active"]
        assert [row["change"] for row in changes] == ["create", "create"]
        assert sorted(row["mode"] for row in jobs) == sorted(
            row["mode"] for row in jobs_before if row["mode"] != "curator"
        )
        command.upgrade(config, "head")
        [row] = asyncio.run(
            _run(
                url,
                (
                    "SELECT status, pinned, view_count, last_used_at = updated_at AS same"
                    " FROM skills WHERE id = $1",
                    (skill,),
                ),
            )
        )[0]
        assert (row["status"], row["pinned"], row["view_count"], row["same"]) == (
            "active",
            False,
            0,
            True,
        )
