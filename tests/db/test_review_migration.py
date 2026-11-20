"""Migration 0016: the kind of a run, the review counters, and the review proposals
(docs/architecture.md, Section 9.4)."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any

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


async def _add_reviews(url: str) -> None:
    """Review runs of different sessions, as the worker makes them: waiting, running, paused
    (with its approval), and completed. Each one has a model call with a cost."""
    connection = await asyncpg.connect(url)
    try:
        sessions = await connection.fetch(
            "SELECT org_id, id, workspace_id, agent_profile_id FROM sessions ORDER BY id LIMIT 4"
        )
        assert len(sessions) == 4
        states = ("queued", "running", "paused", "completed")
        for session, status in zip(sessions, states, strict=True):
            run_id = await connection.fetchval(
                "INSERT INTO runs (org_id, session_id, agent_profile_id, runtime, unattended, kind,"
                " review_from_seq, review_to_seq, status, lease_owner, lease_expires_at,"
                " pending_approval_id, finished_at, runtime_state)"
                " VALUES ($1, $2, $3, 'fake', true, 'review', 0, 1, $4,"
                " CASE WHEN $4 = 'running' THEN 'w-1' END,"
                " CASE WHEN $4 = 'running' THEN now() + interval '1 hour' END,"
                " CASE WHEN $4 = 'paused' THEN 'review:1' END,"
                " CASE WHEN $4 = 'completed' THEN now() END, 'review-state') RETURNING id",
                session["org_id"],
                session["id"],
                session["agent_profile_id"],
                status,
            )
            if status == "paused":
                await connection.execute(
                    "INSERT INTO approvals (org_id, session_id, run_id, call_id, tool_name,"
                    " arguments, arguments_digest, expires_at)"
                    " VALUES ($1, $2, $3, 'review:1', 'memory', '{}'::jsonb, 'd',"
                    " now() + interval '1 day')",
                    session["org_id"],
                    session["id"],
                    run_id,
                )
            await connection.execute(
                "INSERT INTO model_calls (call_id, org_id, workspace_id, session_id, run_id,"
                " provider, model, endpoint, status_code, cost_usd, duration_ms)"
                " VALUES (gen_random_uuid(), $1, $2, $3, $4, 'anthropic', 'm', 'messages', 200,"
                " 1.25, 1)",
                session["org_id"],
                session["workspace_id"],
                session["id"],
                run_id,
            )
    finally:
        await connection.close()


async def _spend(url: str) -> dict[str, Any]:
    """The cost that the budgets of the orgs add up (Section 4.3)."""
    connection = await asyncpg.connect(url)
    try:
        return {
            "total": await connection.fetchval("SELECT sum(cost_usd) FROM model_calls"),
            "per_org": {
                str(row["org_id"]): row["cost"]
                for row in await connection.fetch(
                    "SELECT org_id, sum(cost_usd) AS cost FROM model_calls GROUP BY org_id"
                )
            },
            "runs": await connection.fetchval("SELECT count(*) FROM runs"),
            "calls": await connection.fetchval("SELECT count(*) FROM model_calls"),
        }
    finally:
        await connection.close()


async def _review_state(url: str) -> list[tuple[str, int, int]]:
    """``(status, runs, running-or-leased)`` of the runs that were review runs."""
    connection = await asyncpg.connect(url)
    try:
        rows = await connection.fetch(
            "SELECT status, count(*) AS runs,"
            " count(*) FILTER (WHERE lease_owner IS NOT NULL OR pending_approval_id IS NOT NULL)"
            " AS active FROM runs WHERE failure_message LIKE 'the background review was removed%'"
            " OR status = 'completed' GROUP BY status ORDER BY status"
        )
    finally:
        await connection.close()
    return [(row["status"], row["runs"], row["active"]) for row in rows]


async def _pending_review_approvals(url: str) -> int:
    connection = await asyncpg.connect(url)
    try:
        return int(
            await connection.fetchval(
                "SELECT count(*) FROM approvals WHERE call_id LIKE 'review:%'"
            )
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


def test_the_downgrade_keeps_the_spend_and_stops_the_waiting_reviews() -> None:
    with migrated_database() as url:
        asyncio.run(seed(url))
        asyncio.run(_add_reviews(url))
        assert asyncio.run(_kinds(url)) == ["chat", "review"]
        before = asyncio.run(_spend(url))
        assert before["total"] >= Decimal("5.00")  # the four reviews and the seed data
        assert asyncio.run(_pending_review_approvals(url)) == 1
        config = alembic_config(url)
        command.downgrade(config, "0015")
        old = asyncio.run(_shape(url))
        assert old["table"] is False
        assert old["columns"] == set()
        # No run and no model call is lost, so no budget changes.
        assert asyncio.run(_spend(url)) == before
        # No review stays active: old code would run a waiting review as a chat run.
        assert asyncio.run(_review_state(url)) == [("cancelled", 3, 0), ("completed", 1, 0)]
        assert asyncio.run(_pending_review_approvals(url)) == 0
        command.upgrade(config, "0016")
        assert asyncio.run(_spend(url)) == before
        assert asyncio.run(_kinds(url)) == ["chat"]


async def _add_chat_state(url: str) -> None:
    """An older completed chat run with a runtime state, in the session of the completed
    review (the fourth session). The review is newer."""
    connection = await asyncpg.connect(url)
    try:
        row = await connection.fetchrow(
            "SELECT org_id, id, agent_profile_id FROM sessions ORDER BY id OFFSET 3 LIMIT 1"
        )
        assert row is not None
        await connection.execute(
            "INSERT INTO runs (org_id, session_id, agent_profile_id, runtime, status,"
            " runtime_state, finished_at) VALUES ($1, $2, $3, 'fake', 'completed', 'chat-state',"
            " now() - interval '1 hour')",
            row["org_id"],
            row["id"],
            row["agent_profile_id"],
        )
    finally:
        await connection.close()


async def _legacy_continuation(url: str) -> str | None:
    """The query of the queue of migration 0015: the newest completed run of the session
    that has a runtime state. It has no run kind."""
    connection = await asyncpg.connect(url)
    try:
        value = await connection.fetchval(
            "SELECT runtime_state FROM runs WHERE session_id ="
            " (SELECT id FROM sessions ORDER BY id OFFSET 3 LIMIT 1)"
            " AND runtime = 'fake' AND status = 'completed' AND runtime_state IS NOT NULL"
            " ORDER BY finished_at DESC LIMIT 1"
        )
    finally:
        await connection.close()
    return None if value is None else str(value)


def test_the_downgrade_leaves_no_runtime_state_on_a_review_run() -> None:
    with migrated_database() as url:
        asyncio.run(seed(url))
        asyncio.run(_add_chat_state(url))
        asyncio.run(_add_reviews(url))
        before = asyncio.run(_spend(url))
        # With the kind, the queue skips the review. Without it, the newest state would win.
        assert asyncio.run(_legacy_continuation(url)) == "review-state"
        config = alembic_config(url)
        command.downgrade(config, "0015")
        assert asyncio.run(_legacy_continuation(url)) == "chat-state"
        assert asyncio.run(_spend(url)) == before, "the runs and the spend stay"
        command.upgrade(config, "0016")
        assert asyncio.run(_legacy_continuation(url)) == "chat-state"
        assert asyncio.run(_spend(url)) == before
