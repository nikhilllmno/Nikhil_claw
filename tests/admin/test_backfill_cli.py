"""Tests of ``python -m sama.admin backfill-run-completed`` (docs/architecture.md, Section 18.4)."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import insert, select
from sqlalchemy.pool import NullPool

from sama.admin.__main__ import main
from sama.db import create_engine
from sama.db.models import AuditEntry, Run
from sama.observability.activation import backfill_run_completed
from tests.orchestrator.conftest import Tenant, add_tenant

ENDED = datetime(2026, 11, 20, 10, tzinfo=UTC)


async def _old_runs(url: str) -> tuple[Tenant, list[uuid.UUID]]:
    """A tenant with runs that ended before the change: no ``run.completed`` row."""
    engine = create_engine(url, poolclass=NullPool)
    try:
        tenant = await add_tenant(engine)
        runs = [uuid.uuid4() for _ in range(3)]
        async with engine.begin() as connection:
            for index, run_id in enumerate(runs):
                await connection.execute(
                    insert(Run).values(
                        id=run_id,
                        org_id=tenant.org,
                        session_id=tenant.ids["s1"],
                        agent_profile_id=tenant.ids["profile"],
                        runtime="fake",
                        status=("completed", "completed", "failed")[index],
                        triggered_by_user_id=tenant.ids["mia"] if index == 0 else None,
                        unattended=index == 1,
                        finished_at=ENDED,
                    )
                )
        return tenant, runs
    finally:
        await engine.dispose()


async def _rows(url: str, org: uuid.UUID) -> list[Any]:
    engine = create_engine(url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            return list(
                (
                    await connection.execute(
                        select(AuditEntry)
                        .where(AuditEntry.org_id == org, AuditEntry.action == "run.completed")
                        .order_by(AuditEntry.id)
                    )
                ).all()
            )
    finally:
        await engine.dispose()


def test_the_backfill_writes_one_row_per_completed_run_without_message_text(
    queue_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tenant, (by_user, automated, failed) = asyncio.run(_old_runs(queue_database_url))
    monkeypatch.setenv("SAMA_DATABASE_URL", queue_database_url)
    # A batch of 1 proves that the loop goes on until no run is left.
    assert main(["backfill-run-completed", "--batch", "1"]) == 0
    assert json.loads(capsys.readouterr().out)["written"] >= 2

    rows = asyncio.run(_rows(queue_database_url, tenant.org))
    assert {row.run_id for row in rows} == {by_user, automated}
    first = next(row for row in rows if row.run_id == by_user)
    assert (first.actor_kind, first.actor_user_id, first.workspace_id) == (
        "user",
        tenant.ids["mia"],
        tenant.ids["ws"],
    )
    assert first.created_at == ENDED
    assert first.details == {
        "session_id": str(tenant.ids["s1"]),
        "kind": "chat",
        "compact": False,
        "side": False,
        "runtime": "fake",
        "human_origin": False,
        "unattended": False,
        "via": "backfill",
    }
    second = next(row for row in rows if row.run_id == automated)
    assert (second.actor_kind, second.details["unattended"]) == ("system", True)
    assert failed not in {row.run_id for row in rows}

    # A second run adds no copy.
    assert main(["backfill-run-completed"]) == 0
    assert json.loads(capsys.readouterr().out) == {"written": 0}
    assert len(asyncio.run(_rows(queue_database_url, tenant.org))) == 2


def test_the_batch_must_be_positive(
    queue_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("SAMA_DATABASE_URL", queue_database_url)
    with pytest.raises(SystemExit):
        main(["backfill-run-completed", "--batch", "0"])
    assert "--batch must be 1 or more" in capsys.readouterr().err
    engine = create_engine(queue_database_url, poolclass=NullPool)
    with pytest.raises(ValueError, match="1 or more"):
        asyncio.run(backfill_run_completed(engine, batch=0))
    asyncio.run(engine.dispose())
