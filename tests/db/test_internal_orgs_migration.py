"""Migration 0053: internal orgs (docs/architecture.md, Section 18.4).

The backfill marks each org without ``created_by_auth_user_id`` (an org of ``bootstrap``) as
internal. A sign-up org stays a customer org. The downgrade removes the column.
"""

from __future__ import annotations

import asyncio
import uuid

import asyncpg
import pytest
from alembic import command

from tests.db.conftest import alembic_config, migrated_database

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
