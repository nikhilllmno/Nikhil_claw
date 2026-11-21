"""Tests of ``python -m sama.admin region`` (docs/architecture.md, Section 17.3, M6.7)."""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import pytest
from sqlalchemy.pool import NullPool

from sama.admin.__main__ import main
from sama.db import create_engine
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


def test_an_operator_shows_and_sets_the_region_of_an_org(
    queue_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    org = str(asyncio.run(_new_org(queue_database_url)))
    monkeypatch.setenv("SAMA_DATABASE_URL", queue_database_url)
    monkeypatch.delenv("SAMA_REGION", raising=False)
    monkeypatch.setenv("SAMA_REGION_URLS", "eu-central-1=https://eu.sama.test")
    assert _run(capsys, "region", "show", "--org", org) == {
        "org_id": org,
        "home": "ap-south-1",
        "region": "ap-south-1",
        "here": True,
    }
    assert _run(capsys, "region", "set", "--org", org, "--region", "eu-central-1") == {
        "org_id": org,
        "home": "ap-south-1",
        "region": "eu-central-1",
        "before": "ap-south-1",
    }
    assert _run(capsys, "region", "show", "--org", org)["here"] is False

    assert main(["region", "set", "--org", org, "--region", "us-east-1"]) == 1
    assert "not a known region" in capsys.readouterr().err
    assert main(["region", "show", "--org", str(uuid.uuid4())]) == 1
    assert "does not exist" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        main(["region", "set", "--org", org])
    assert "needs --region" in capsys.readouterr().err
