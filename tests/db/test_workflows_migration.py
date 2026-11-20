"""Migration 0029: durable workflows (docs/architecture.md, Section 12.3; roadmap M4.5)."""

from __future__ import annotations

import asyncio
import json

import asyncpg
from alembic import command

from tests.db.conftest import alembic_config, migrated_database
from tests.db.world import seed

OLD = [
    "core",
    "todo",
    "web",
    "clarify",
    "github",
    "memory",
    "skills",
    "sessions",
    "media",
    "tool_search",
    "subagents",
    "connections",
    "integrations",
    "delegation",
    "tasks",
    "cron",
    "code_exec",
]
CUSTOM = ["core", "web"]
TABLES = (
    "workflows",
    "workflow_versions",
    "workflow_instances",
    "workflow_steps",
    "workflow_receipts",
)


async def _fetch(url: str, query: str, *args: object) -> list[asyncpg.Record]:
    connection = await asyncpg.connect(url)
    try:
        return await connection.fetch(query, *args)
    finally:
        await connection.close()


async def _execute(url: str, query: str, *args: object) -> None:
    connection = await asyncpg.connect(url)
    try:
        await connection.execute(query, *args)
    finally:
        await connection.close()


def _toolsets(url: str) -> dict[str, list[str]]:
    rows = asyncio.run(_fetch(url, "SELECT name, toolsets::text AS t FROM agent_profiles"))
    return {row["name"]: json.loads(row["t"]) for row in rows}


def _tables(url: str) -> dict[str, tuple[bool, bool | None, int]]:
    result = {}
    for table in TABLES:
        [row] = asyncio.run(
            _fetch(
                url,
                "SELECT to_regclass($1) IS NOT NULL AS exists,"
                " (SELECT relrowsecurity FROM pg_class WHERE oid = to_regclass($1)) AS rls,"
                " (SELECT count(*) FROM pg_policies WHERE tablename = $2) AS policies",
                f"public.{table}",
                table,
            )
        )
        result[table] = (row["exists"], row["rls"], row["policies"])
    return result


def _set(url: str, name: str, toolsets: list[str]) -> None:
    asyncio.run(
        _execute(
            url,
            "UPDATE agent_profiles SET toolsets = $1::jsonb WHERE name = $2",
            json.dumps(toolsets),
            name,
        )
    )


async def _duplicates(url: str) -> list[str]:
    """Tries rows that break a unique rule, in transactions that are rolled back."""
    [row] = await _fetch(
        url,
        "SELECT org_id, id, workflow_id FROM workflow_instances ORDER BY id LIMIT 1",
    )
    fire = (
        "INSERT INTO workflow_instances (org_id, workflow_id, version, owner_user_id,"
        " workspace_id, agent_profile_id, trigger, scheduled_for)"
        " SELECT org_id, id, 1, owner_user_id, workspace_id, agent_profile_id, 'schedule',"
        " '2026-01-01T00:00:00Z' FROM workflows WHERE id = $1"
    )
    connection = await asyncpg.connect(url)
    failed: list[str] = []
    try:
        for label, queries in (
            (
                "step",
                [
                    (
                        "INSERT INTO workflow_steps (org_id, instance_id, step_id, kind)"
                        " VALUES ($1, $2, 'post', 'tool')",
                        (row["org_id"], row["id"]),
                    )
                ],
            ),
            ("fire", [(fire, (row["workflow_id"],))] * 2),
        ):
            try:
                async with connection.transaction():
                    for query, args in queries:
                        await connection.execute(query, *args)
            except asyncpg.UniqueViolationError:
                failed.append(label)
    finally:
        await connection.close()
    return failed


WORKFLOW_TRIGGER = (
    "WITH w AS (SELECT org_id, id, workspace_id, owner_user_id, agent_profile_id FROM workflows"
    " ORDER BY id LIMIT 1), t AS (INSERT INTO triggers (org_id, workspace_id, owner_user_id,"
    " agent_profile_id, kind, name, prompt, target, workflow_id, created_by_user_id)"
    " SELECT org_id, workspace_id, owner_user_id, agent_profile_id, 'webhook', 'w', 'p',"
    " 'workflow', id, owner_user_id FROM w RETURNING org_id, id)"
    " INSERT INTO trigger_events (org_id, trigger_id, delivery_id, workflow_instance_id)"
    " SELECT t.org_id, t.id, 'msg_w', i.id FROM t JOIN workflow_instances i"
    " ON i.org_id = t.org_id ORDER BY i.id LIMIT 1"
)
TRIGGER_COUNTS = (
    "SELECT (SELECT count(*) FROM triggers) AS triggers,"
    " (SELECT count(*) FROM trigger_events) AS events"
)


def _trigger_counts(url: str) -> tuple[int, int]:
    [row] = asyncio.run(_fetch(url, TRIGGER_COUNTS))
    return row["triggers"], row["events"]


def test_the_upgrade_adds_the_tables_and_the_toolset() -> None:
    with migrated_database() as url:
        asyncio.run(seed(url))
        assert asyncio.run(_duplicates(url)) == ["step", "fire"]
        config = alembic_config(url)
        run_triggers = _trigger_counts(url)
        asyncio.run(_execute(url, WORKFLOW_TRIGGER))
        assert _trigger_counts(url) == (run_triggers[0] + 1, run_triggers[1] + 1)
        command.downgrade(config, "0028")
        assert all(not exists for exists, _, _ in _tables(url).values())
        # The old schema cannot hold a workflow trigger: it goes, and the run triggers stay.
        assert _trigger_counts(url) == run_triggers
        _set(url, "profile-a", OLD)
        _set(url, "profile-a2", CUSTOM)
        command.upgrade(config, "0029")
        toolsets = _toolsets(url)
        assert toolsets["profile-a"] == [*OLD, "workflows"]
        assert toolsets["profile-a2"] == CUSTOM  # a person changed it
        assert _tables(url) == dict.fromkeys(TABLES, (True, True, 1))
        command.downgrade(config, "0028")
        assert _toolsets(url)["profile-a"] == OLD
        assert _toolsets(url)["profile-a2"] == CUSTOM
        command.upgrade(config, "head")
