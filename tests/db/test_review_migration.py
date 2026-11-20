"""Migration 0016: the kind of a run, the review counters, and the review proposals
(docs/architecture.md, Section 9.4)."""

from __future__ import annotations

import asyncio

import asyncpg
from alembic import command

from tests.db.conftest import alembic_config, migrated_database
from tests.db.world import seed

REVIEW_COLUMNS = {
    ("agent_profiles", "background_review"),
    ("agent_profiles", "review_model"),
    ("sessions", "review_user_turns"),
    ("sessions", "review_tool_iterations"),
    ("sessions", "review_cursor_seq"),
    ("runs", "kind"),
    ("runs", "review_from_seq"),
    ("runs", "review_to_seq"),
}


async def _shape(url: str) -> dict[str, object]:
    connection = await asyncpg.connect(url)
    try:
        columns = await connection.fetch(
            "SELECT table_name, column_name FROM information_schema.columns"
            " WHERE table_schema = 'public' AND (table_name, column_name) IN"
            " (SELECT * FROM unnest($1::text[], $2::text[]))",
            [table for table, _ in sorted(REVIEW_COLUMNS)],
            [column for _, column in sorted(REVIEW_COLUMNS)],
        )
        return {
            "columns": {(row["table_name"], row["column_name"]) for row in columns},
            "table": await connection.fetchval(
                "SELECT to_regclass('public.review_proposals') IS NOT NULL"
            ),
            "rls": await connection.fetchval(
                "SELECT relrowsecurity FROM pg_class"
                " WHERE oid = to_regclass('public.review_proposals')"
            ),
            "policies": await connection.fetchval(
                "SELECT count(*) FROM pg_policies WHERE tablename = 'review_proposals'"
            ),
            "index": await connection.fetchval(
                "SELECT to_regclass('public.ix_runs_one_queued_review_per_session') IS NOT NULL"
            ),
        }
    finally:
        await connection.close()


async def _grants(url: str) -> set[tuple[str, str]]:
    connection = await asyncpg.connect(url)
    try:
        rows = await connection.fetch(
            "SELECT grantee, privilege_type FROM information_schema.table_privileges"
            " WHERE table_schema = 'public' AND table_name = 'review_proposals'"
            " AND grantee IN ('anon', 'authenticated', 'PUBLIC')"
        )
    finally:
        await connection.close()
    return {(row["grantee"], row["privilege_type"]) for row in rows}


async def _kinds(url: str) -> list[str]:
    connection = await asyncpg.connect(url)
    try:
        rows = await connection.fetch("SELECT DISTINCT kind FROM runs")
    finally:
        await connection.close()
    return sorted(row["kind"] for row in rows)


async def _add_review(url: str) -> None:
    """A review run, as the worker makes it. The downgrade removes it."""
    connection = await asyncpg.connect(url)
    try:
        row = await connection.fetchrow(
            "SELECT org_id, id, agent_profile_id FROM sessions ORDER BY id LIMIT 1"
        )
        assert row is not None
        await connection.execute(
            "INSERT INTO runs (org_id, session_id, agent_profile_id, runtime, unattended, kind,"
            " review_from_seq, review_to_seq) VALUES ($1, $2, $3, 'fake', true, 'review', 0, 1)",
            row["org_id"],
            row["id"],
            row["agent_profile_id"],
        )
    finally:
        await connection.close()


async def _defaults(url: str) -> dict[str, object]:
    connection = await asyncpg.connect(url)
    try:
        profile = await connection.fetchrow(
            "SELECT bool_and(background_review) AS on, count(review_model) AS models"
            " FROM agent_profiles"
        )
        session = await connection.fetchrow(
            "SELECT max(review_user_turns) AS turns, max(review_tool_iterations) AS tools,"
            " max(review_cursor_seq) AS cursor FROM sessions"
        )
        assert profile is not None
        assert session is not None
        return {**dict(profile), **dict(session)}
    finally:
        await connection.close()


async def _checks(url: str) -> list[str]:
    """Tries bad rows in a transaction that is rolled back. Gives the failed constraints."""
    connection = await asyncpg.connect(url)
    failed: list[str] = []
    try:
        row = await connection.fetchrow(
            "SELECT org_id, id, agent_profile_id FROM sessions ORDER BY id LIMIT 1"
        )
        assert row is not None
        cases = (
            ("kind", "other", False, None, None),
            ("range", "review", True, None, None),
            ("range-chat", "chat", False, 0, 1),
            ("unattended", "review", False, 0, 1),
        )
        for label, kind, unattended, start, end in cases:
            try:
                async with connection.transaction():
                    await connection.execute(
                        "INSERT INTO runs (org_id, session_id, agent_profile_id, runtime,"
                        " unattended, kind, review_from_seq, review_to_seq)"
                        " VALUES ($1, $2, $3, 'fake', $4, $5, $6, $7)",
                        row["org_id"],
                        row["id"],
                        row["agent_profile_id"],
                        unattended,
                        kind,
                        start,
                        end,
                    )
            except asyncpg.CheckViolationError:
                failed.append(label)
        try:
            async with connection.transaction():
                for _ in range(2):
                    await connection.execute(
                        "INSERT INTO runs (org_id, session_id, agent_profile_id, runtime,"
                        " unattended, kind, review_from_seq, review_to_seq)"
                        " VALUES ($1, $2, $3, 'fake', true, 'review', 0, 1)",
                        row["org_id"],
                        row["id"],
                        row["agent_profile_id"],
                    )
        except asyncpg.UniqueViolationError:
            failed.append("one-waiting")
    finally:
        await connection.close()
    return failed


def test_the_upgrade_adds_the_review_columns_table_and_rules() -> None:
    with migrated_database() as url:
        asyncio.run(seed(url))
        config = alembic_config(url)
        command.downgrade(config, "0015")
        old = asyncio.run(_shape(url))
        assert old["columns"] == set()
        assert (old["table"], old["index"]) == (False, False)
        command.upgrade(config, "0016")
        new = asyncio.run(_shape(url))
        assert new["columns"] == REVIEW_COLUMNS
        assert new == {
            **new,
            "table": True,
            "rls": True,
            "policies": 1,
            "index": True,
        }
        assert asyncio.run(_grants(url)) == {("authenticated", "SELECT")}
        # Rows that exist before the upgrade are chat runs, are reviewed, and have no cursor.
        assert asyncio.run(_kinds(url)) == ["chat"]
        assert asyncio.run(_defaults(url)) == {
            "on": True,
            "models": 0,
            "turns": 0,
            "tools": 0,
            "cursor": 0,
        }
        assert asyncio.run(_checks(url)) == [
            "kind",
            "range",
            "range-chat",
            "unattended",
            "one-waiting",
        ]


def test_the_downgrade_removes_the_review_runs_and_the_new_objects() -> None:
    with migrated_database() as url:
        asyncio.run(seed(url))
        asyncio.run(_add_review(url))
        assert asyncio.run(_kinds(url)) == ["chat", "review"]
        config = alembic_config(url)
        command.downgrade(config, "0015")
        # The old code reads every run as a chat run, so no review run stays.
        old = asyncio.run(_shape(url))
        assert old["table"] is False
        assert old["columns"] == set()
        command.upgrade(config, "0016")
        assert asyncio.run(_kinds(url)) == ["chat"]
