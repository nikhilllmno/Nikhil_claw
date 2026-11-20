"""Migration 0025: the trigger tables, the run toolsets, and the vault names of triggers
(docs/architecture.md, Section 12.2)."""

from __future__ import annotations

import asyncio
import uuid

import asyncpg
import pytest
from alembic import command

from tests.db.conftest import alembic_config, migrated_database
from tests.db.world import seed


async def _state(url: str) -> tuple[bool, bool, bool, int]:
    connection = await asyncpg.connect(url)
    try:
        triggers = bool(await connection.fetchval("SELECT to_regclass('public.triggers')"))
        events = bool(await connection.fetchval("SELECT to_regclass('public.trigger_events')"))
        column = bool(
            await connection.fetchval(
                "SELECT 1 FROM information_schema.columns"
                " WHERE table_name = 'runs' AND column_name = 'toolsets'"
            )
        )
        secrets = await connection.fetchval(
            "SELECT count(*) FROM vault_secrets"
            " WHERE name LIKE 'trigger:%' OR name = 'composio_webhook_secret'"
        )
    finally:
        await connection.close()
    return triggers, events, column, int(secrets)


async def _put_secret(url: str, name: str) -> None:
    connection = await asyncpg.connect(url)
    try:
        org_id = await connection.fetchval("SELECT id FROM orgs LIMIT 1")
        await connection.execute(
            "INSERT INTO vault_secrets (org_id, name, key_id, wrapped_key, ciphertext)"
            " VALUES ($1, $2, 'k', 'w'::bytea, 'c'::bytea)",
            org_id,
            name,
        )
    finally:
        await connection.close()


def test_the_upgrade_adds_the_triggers_and_the_downgrade_removes_them() -> None:
    with migrated_database() as url:
        asyncio.run(seed(url))
        assert asyncio.run(_state(url)) == (True, True, True, 0)
        # The vault takes the secret of a trigger and the Composio webhook secret of an org.
        asyncio.run(_put_secret(url, "composio_webhook_secret"))
        asyncio.run(_put_secret(url, f"trigger:{uuid.uuid4()}"))
        with pytest.raises(asyncpg.CheckViolationError):
            asyncio.run(_put_secret(url, "trigger:not-a-uuid"))
        config = alembic_config(url)
        command.downgrade(config, "0024")
        assert asyncio.run(_state(url)) == (False, False, False, 0)
        with pytest.raises(asyncpg.CheckViolationError):
            asyncio.run(_put_secret(url, f"trigger:{uuid.uuid4()}"))
        command.upgrade(config, "head")
        assert asyncio.run(_state(url)) == (True, True, True, 0)
