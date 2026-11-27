"""Migration 0053: internal orgs (docs/architecture.md, Section 18.4).

The backfill marks each org without ``created_by_auth_user_id`` (an org of ``bootstrap``) as
internal. A sign-up org stays a customer org. The downgrade removes the column.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime

import asyncpg
import pytest
from alembic import command

from tests.db.conftest import alembic_config, migrated_database
from tests.db.world import seed

pytestmark = pytest.mark.filterwarnings("ignore::ResourceWarning")


async def _fetch(url: str, query: str, *args: object) -> list[asyncpg.Record]:
    connection = await asyncpg.connect(url)
    try:
        return await connection.fetch(query, *args)
    finally:
        await connection.close()


def _has_column(url: str) -> bool:
    rows = asyncio.run(
        _fetch(
            url,
            "SELECT 1 FROM information_schema.columns WHERE table_schema = 'public'"
            " AND table_name = 'orgs' AND column_name = 'is_internal'",
        )
    )
    return bool(rows)


def test_the_backfill_marks_only_the_bootstrap_orgs_as_internal() -> None:
    with migrated_database() as url:
        config = alembic_config(url)
        command.downgrade(config, "0052")
        assert not _has_column(url)
        operator, customer = uuid.uuid4(), uuid.uuid4()
        insert = (
            "INSERT INTO orgs (id, name, slug, created_by_auth_user_id) VALUES ($1, $2, $3, $4)"
        )
        asyncio.run(_fetch(url, insert, operator, "Ops", f"ops-{operator.hex[:8]}", None))
        asyncio.run(_fetch(url, insert, customer, "Acme", f"acme-{customer.hex[:8]}", uuid.uuid4()))

        command.upgrade(config, "head")
        rows = asyncio.run(
            _fetch(url, "SELECT id, is_internal FROM orgs WHERE id = ANY($1)", [operator, customer])
        )
        assert {row["id"]: row["is_internal"] for row in rows} == {
            operator: True,
            customer: False,
        }
        # A new org is a customer org by default.
        later = uuid.uuid4()
        asyncio.run(_fetch(url, insert, later, "New", f"new-{later.hex[:8]}", uuid.uuid4()))
        [row] = asyncio.run(_fetch(url, "SELECT is_internal FROM orgs WHERE id = $1", later))
        assert row["is_internal"] is False

        command.downgrade(config, "0052")
        assert not _has_column(url)
        command.upgrade(config, "head")
        assert _has_column(url)


def test_the_upgrade_backfills_run_completed_once_without_message_text() -> None:
    with migrated_database() as url:
        config = alembic_config(url)
        command.downgrade(config, "0052")
        world = asyncio.run(seed(url))
        done = world.ids["run:a1-alice"]
        asyncio.run(
            _fetch(
                url,
                "UPDATE runs SET status = 'completed', triggered_by_user_id = $2,"
                " finished_at = '2026-11-20T10:00:00Z' WHERE id = $1",
                done,
                world.ids["alice"],
            )
        )
        command.upgrade(config, "head")
        query = (
            "SELECT run_id, actor_kind, actor_user_id, workspace_id, details, created_at"
            " FROM audit_log WHERE action = 'run.completed'"
        )
        [row] = asyncio.run(_fetch(url, query))
        assert (row["run_id"], row["actor_kind"], row["actor_user_id"]) == (
            done,
            "user",
            world.ids["alice"],
        )
        assert row["workspace_id"] == world.ids["a1"]
        assert row["created_at"] == datetime(2026, 11, 20, 10, tzinfo=UTC)
        details = json.loads(row["details"])
        assert details == {
            "session_id": str(world.ids["a1-alice"]),
            "kind": "chat",
            "compact": False,
            "side": False,
            "runtime": "fake",
            "human_origin": False,
            "unattended": False,
            "via": "migration 0053",
        }
        assert "secret" not in row["details"]

        # The downgrade keeps the entry (append-only), and a second upgrade adds no copy.
        command.downgrade(config, "0052")
        command.upgrade(config, "head")
        assert len(asyncio.run(_fetch(url, query))) == 1
