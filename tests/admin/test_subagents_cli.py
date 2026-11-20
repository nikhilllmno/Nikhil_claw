"""Tests of ``python -m sama.admin subagents`` (docs/architecture.md, Section 13.1)."""

from __future__ import annotations

import asyncio
import io
import json
import uuid
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import update
from sqlalchemy.pool import NullPool

from sama.admin.__main__ import main
from sama.db import create_engine
from sama.db.models import AgentProfile
from tests.orchestrator.conftest import add_tenant

SPEC = {
    "name": "researcher",
    "description": "Looks things up.",
    "prompt": "You look things up.",
    "toolsets": ["web"],
}


async def _new_profile(url: str) -> tuple[uuid.UUID, uuid.UUID]:
    engine = create_engine(url, poolclass=NullPool)
    try:
        tenant = await add_tenant(engine)
        async with engine.begin() as connection:
            await connection.execute(
                update(AgentProfile)
                .where(AgentProfile.id == tenant.ids["profile"])
                .values(runtime="claude_agent_sdk", toolsets=["web", "subagents"])
            )
        return tenant.org, tenant.ids["profile"]
    finally:
        await engine.dispose()


def test_set_from_a_file_and_show(
    queue_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    org, profile = asyncio.run(_new_profile(queue_database_url))
    monkeypatch.setenv("SAMA_DATABASE_URL", queue_database_url)
    target = ("--org", str(org), "--profile", str(profile))
    assert main(["subagents", "show", *target]) == 0
    assert json.loads(capsys.readouterr().out)["subagents"] == []
    path = tmp_path / "subagents.json"
    path.write_text(json.dumps([SPEC]), "utf-8")
    assert main(["subagents", "set", *target, "--file", str(path)]) == 0
    out: dict[str, Any] = json.loads(capsys.readouterr().out)
    assert out["org_id"] == str(org)
    assert [item["name"] for item in out["subagents"]] == ["researcher"]
    assert main(["subagents", "show", *target]) == 0
    assert json.loads(capsys.readouterr().out)["subagents"] == out["subagents"]


def test_set_from_stdin(
    queue_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    org, profile = asyncio.run(_new_profile(queue_database_url))
    monkeypatch.setenv("SAMA_DATABASE_URL", queue_database_url)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps([SPEC])))
    assert main(["subagents", "set", "--org", str(org), "--profile", str(profile)]) == 0
    assert json.loads(capsys.readouterr().out)["subagents"][0]["name"] == "researcher"


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("{not json", "error:"),
        ("{}", "must be a list"),
        (json.dumps([{**SPEC, "toolsets": ["memory"]}]), "does not have the toolset memory"),
        (json.dumps([{**SPEC, "max_turns": 500, "prompt": "secret-text"}]), "max_turns"),
    ],
)
def test_an_error_gives_exit_code_1_and_no_spec_text(
    queue_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    text: str,
    message: str,
) -> None:
    org, profile = asyncio.run(_new_profile(queue_database_url))
    monkeypatch.setenv("SAMA_DATABASE_URL", queue_database_url)
    monkeypatch.setattr("sys.stdin", io.StringIO(text))
    assert main(["subagents", "set", "--org", str(org), "--profile", str(profile)]) == 1
    captured = capsys.readouterr()
    assert message in captured.err
    assert "secret-text" not in captured.err
    assert captured.out == ""


def test_a_missing_file_or_profile_gives_exit_code_1(
    queue_database_url: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SAMA_DATABASE_URL", queue_database_url)
    ids = ("--org", str(uuid.uuid4()), "--profile", str(uuid.uuid4()))
    assert main(["subagents", "set", *ids, "--file", str(tmp_path / "none.json")]) == 1
    assert main(["subagents", "show", *ids]) == 1
