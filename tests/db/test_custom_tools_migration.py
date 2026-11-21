"""The custom tools migration (docs/architecture.md, Section 10.8; roadmap M7.3)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import asyncpg
from alembic import command

from tests.db.conftest import alembic_config, migrated_database
from tests.db.test_coding_agents_migration import CUSTOM, OLD, _set, _toolsets
from tests.db.world import seed

REVISION = next(
    path.name.split("_", 1)[0]
    for path in (Path(__file__).resolve().parents[2] / "migrations" / "versions").glob(
        "*_custom_tools.py"
    )
)


async def _fetch(url: str, query: str) -> list[asyncpg.Record]:
    connection = await asyncpg.connect(url)
    try:
        return await connection.fetch(query)
    finally:
        await connection.close()


def _vault_check(url: str) -> str:
    [row] = asyncio.run(
        _fetch(
            url,
            "SELECT pg_get_constraintdef(oid) AS d FROM pg_constraint"
            " WHERE conname = 'ck_vault_secrets_name'",
        )
    )
    return str(row["d"])


def _tables(url: str) -> set[str]:
    rows = asyncio.run(
        _fetch(url, "SELECT tablename FROM pg_tables WHERE tablename LIKE 'custom_tool%'")
    )
    return {row["tablename"] for row in rows}


def test_the_migration_adds_and_removes_only_its_own_vault_names() -> None:
    with migrated_database() as url:
        config = alembic_config(url)
        after = _vault_check(url)
        assert after.count("custom_tool:") == 1
        assert _tables(url) == {"custom_tools", "custom_tool_versions"}
        previous = f"{int(REVISION) - 1:04d}"
        command.downgrade(config, previous)
        before = _vault_check(url)
        assert "custom_tool:" not in before
        assert "browser_login:" in before
        assert _tables(url) == set()
        command.upgrade(config, "head")
        assert _vault_check(url).count("custom_tool:") == 1


def test_the_upgrade_adds_the_toolset_only_to_a_default_profile() -> None:
    before = [*OLD, "coding_agents"]
    with migrated_database() as url:
        asyncio.run(seed(url))
        config = alembic_config(url)
        previous = f"{int(REVISION) - 1:04d}"
        command.downgrade(config, previous)
        _set(url, "profile-a", before)
        _set(url, "profile-a2", CUSTOM)
        command.upgrade(config, REVISION)
        toolsets = _toolsets(url)
        assert toolsets["profile-a"] == [*before, "custom_tools"]
        assert toolsets["profile-a2"] == CUSTOM  # a person changed it
        command.downgrade(config, previous)
        assert _toolsets(url)["profile-a"] == before
        command.upgrade(config, "head")
