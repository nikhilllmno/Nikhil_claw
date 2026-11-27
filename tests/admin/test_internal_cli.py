"""Tests of ``python -m sama.admin internal`` (docs/architecture.md, Section 18.4)."""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.pool import NullPool

from sama.admin.__main__ import main
from sama.db import create_engine
from sama.db.models import AuditEntry
from tests.orchestrator.conftest import add_tenant


def _run(capsys: pytest.CaptureFixture[str], *argv: str) -> Any:
    assert main(list(argv)) == 0
    return json.loads(capsys.readouterr().out)


async def _new_org(url: str) -> uuid.UUID:
    engine = create_engine(url, poolclass=NullPool)
    try:
        return (await add_tenant(engine)).org
    finally:
        await engine.dispose()


async def _audit(url: str, org: str) -> list[dict[str, Any]]:
    engine = create_engine(url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            rows = await connection.execute(
                select(AuditEntry.details).where(
                    AuditEntry.org_id == uuid.UUID(org),
                    AuditEntry.action == "org.internal_changed",
                )
            )
            return [row.details for row in rows]
    finally:
        await engine.dispose()


def test_an_operator_shows_and_sets_the_internal_flag_of_an_org(
    queue_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    org = str(asyncio.run(_new_org(queue_database_url)))
    monkeypatch.setenv("SAMA_DATABASE_URL", queue_database_url)
    assert _run(capsys, "internal", "show", "--org", org) == {"org_id": org, "is_internal": False}
    assert _run(capsys, "internal", "set", "--org", org, "--on") == {
        "org_id": org,
        "is_internal": True,
        "before": False,
    }
    # The same value again changes nothing and writes no second audit entry.
    assert _run(capsys, "internal", "set", "--org", org, "--on")["before"] is True
    assert _run(capsys, "internal", "show", "--org", org)["is_internal"] is True
    assert _run(capsys, "internal", "set", "--org", org, "--off")["is_internal"] is False
    assert asyncio.run(_audit(queue_database_url, org)) == [
        {"from": False, "to": True},
        {"from": True, "to": False},
    ]

    assert main(["internal", "show", "--org", str(uuid.uuid4())]) == 1
    assert "does not exist" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        main(["internal", "set", "--org", org])
    assert "needs --on or --off" in capsys.readouterr().err
