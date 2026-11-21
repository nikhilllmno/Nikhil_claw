"""Migration 0037: billing and quotas (docs/architecture.md, Section 17.5)."""

from __future__ import annotations

import asyncio
import json

import asyncpg
import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.db.conftest import alembic_config, migrated_database
from tests.db.world import World, seed


async def _fetch(url: str, query: str, *args: object) -> list[asyncpg.Record]:
    connection = await asyncpg.connect(url)
    try:
        return await connection.fetch(query, *args)
    finally:
        await connection.close()


def _policies(url: str, table: str) -> list[str]:
    rows = asyncio.run(
        _fetch(url, "SELECT policyname FROM pg_policies WHERE tablename = $1", table)
    )
    return sorted(row["policyname"] for row in rows)


def _limits(url: str, slug: str) -> dict[str, object]:
    rows = asyncio.run(_fetch(url, "SELECT limits FROM orgs WHERE slug = $1", slug))
    value = rows[0]["limits"]
    return dict(json.loads(value) if isinstance(value, str) else value)


def test_the_upgrade_adds_the_billing_tables_and_keeps_the_limits() -> None:
    with migrated_database() as url:
        asyncio.run(seed(url))
        assert _policies(url, "org_billing") == ["org_billing_select"]
        assert _policies(url, "billing_events") == ["billing_events_select"]
        config = alembic_config(url)
        command.downgrade(config, "0036")
        assert _policies(url, "org_billing") == []
        # The model quotas of a plan come from the control plane only while billing is on.
        asyncio.run(
            _fetch(
                url,
                "INSERT INTO orgs (name, slug, plan, limits) VALUES"
                " ('F1', 'f1', 'free', '{\"max_running_runs\": 2}'),"
                " ('F2', 'f2', 'free', '{\"model_usd_per_month\": \"1.00\"}'),"
                " ('T1', 't1', 'team', '{}')",
            )
        )
        command.upgrade(config, "head")
        assert _limits(url, "f1") == {"max_running_runs": 2}
        assert _limits(url, "f2") == {"model_usd_per_month": "1.00"}
        assert _limits(url, "t1") == {}
        assert _policies(url, "org_billing") == ["org_billing_select"]


@pytest.mark.parametrize(
    "values",
    [
        "usage_reported_cents = -1",
        "usage_pending_cents = usage_reported_cents",
    ],
)
async def test_the_usage_checks_hold(engine: AsyncEngine, world: World, values: str) -> None:
    with pytest.raises(IntegrityError):
        async with engine.begin() as connection:
            await connection.execute(
                text(f"UPDATE org_billing SET {values} WHERE org_id = :org"),  # noqa: S608
                {"org": world.ids["org-a"]},
            )


async def test_an_org_has_one_billing_row(engine: AsyncEngine, world: World) -> None:
    with pytest.raises(IntegrityError):
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO org_billing (org_id, stripe_customer_id)"
                    " VALUES (:org, 'cus_second')"
                ),
                {"org": world.ids["org-a"]},
            )
