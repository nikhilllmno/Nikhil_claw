"""Migration 0031: change proposals and the profile history (docs/architecture.md, Sections
9.4.2 and 11.4)."""

from __future__ import annotations

import asyncio

import asyncpg
from alembic import command

from tests.db.conftest import alembic_config, migrated_database
from tests.db.world import seed


async def _fetch(url: str, query: str, *args: object) -> list[asyncpg.Record]:
    connection = await asyncpg.connect(url)
    try:
        return await connection.fetch(query, *args)
    finally:
        await connection.close()


def _state(url: str) -> tuple[bool, list[str], bool]:
    """Whether ``profile_changes`` exists, its policies, and whether the snapshot column
    exists."""
    table = asyncio.run(_fetch(url, "SELECT to_regclass('public.profile_changes') AS t"))
    policies = asyncio.run(
        _fetch(url, "SELECT policyname FROM pg_policies WHERE tablename = 'profile_changes'")
    )
    column = asyncio.run(
        _fetch(
            url,
            "SELECT 1 FROM information_schema.columns WHERE table_schema = 'public'"
            " AND table_name = 'sessions' AND column_name = 'profile_snapshot'",
        )
    )
    return (
        table[0]["t"] is not None,
        sorted(row["policyname"] for row in policies),
        bool(column),
    )


def test_the_upgrade_adds_the_history_and_the_downgrade_removes_it() -> None:
    with migrated_database() as url:
        asyncio.run(seed(url))
        assert _state(url) == (True, ["profile_changes_select"], True)
        rls = asyncio.run(
            _fetch(
                url,
                "SELECT relrowsecurity, relforcerowsecurity FROM pg_class"
                " WHERE oid = to_regclass('public.profile_changes')",
            )
        )
        assert rls[0]["relrowsecurity"]
        config = alembic_config(url)
        command.downgrade(config, "0030")
        assert _state(url) == (False, [], False)
        command.upgrade(config, "head")
        assert _state(url) == (True, ["profile_changes_select"], True)
