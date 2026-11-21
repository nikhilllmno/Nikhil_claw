"""The commands, test runs, and pull requests of a session (Section 15.5)."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.coding_mode.activity import (
    is_test_command,
    list_commands,
    list_pull_requests,
    result_summary,
)
from sama.db.models import RunEvent
from sama.orchestrator.queue import RunQueue
from tests.orchestrator.conftest import Tenant


@pytest.mark.parametrize(
    "command",
    [
        "pytest -q",
        "uv run pytest tests/x.py",
        "cd web && npm test",
        "npm run test -- --watch=false",
        "go test ./...",
        "cargo test",
        "python -m pytest",
        "make test",
        "npx vitest run",
    ],
)
def test_test_commands(command: str) -> None:
    assert is_test_command(command)


@pytest.mark.parametrize("command", ["ls", "cat pytest.ini", "npm install", "git status"])
def test_other_commands(command: str) -> None:
    assert not is_test_command(command)


def test_the_summary_is_the_last_result_line() -> None:
    output = "collected 3 items\n...\n==== 2 passed, 1 failed in 0.12s ====\n"
    assert result_summary(output) == "2 passed, 1 failed in 0.12s"
    assert result_summary("Tests  12 passed (12)\n") == "Tests  12 passed (12)"
    assert result_summary("nothing here") is None


async def _events(
    engine: AsyncEngine, tenant: Tenant, run_id: uuid.UUID, events: list[tuple[str, Any]]
) -> None:
    async with engine.begin() as connection:
        for seq, (kind, payload) in enumerate(events):
            await connection.execute(
                insert(RunEvent).values(
                    org_id=tenant.org,
                    session_id=tenant.ids["mine"],
                    run_id=run_id,
                    seq=1000 + seq,
                    type=kind,
                    payload=payload,
                )
            )


async def test_the_commands_and_pull_requests_come_from_the_run_events(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    session = tenant.ids["mine"]
    run_id = await RunQueue(engine).enqueue(
        org_id=tenant.org, session_id=session, text="go", user_id=None
    )
    started = "tool_call_started"
    finished = "tool_call_finished"
    await _events(
        engine,
        tenant,
        run_id,
        [
            (started, {"call_id": "c1", "name": "Bash", "arguments": {"command": "ls"}}),
            (finished, {"call_id": "c1", "is_error": False, "output_preview": "a.py"}),
            (started, {"call_id": "c2", "name": "Bash", "arguments": {"command": "pytest -q"}}),
            (
                finished,
                {"call_id": "c2", "is_error": True, "output_preview": "== 1 failed in 0.1s =="},
            ),
            (started, {"call_id": "c3", "name": "Read", "arguments": {"file_path": "a"}}),
            (started, {"call_id": "c4", "name": "Bash", "arguments": {"summary": "npm test"}}),
            (
                started,
                {
                    "call_id": "c5",
                    "name": "mcp__sama__pr_create",
                    "arguments": {"repo": "o/n", "head": "sama/x", "base": "main", "title": "T"},
                },
            ),
            (
                finished,
                {
                    "call_id": "c5",
                    "is_error": False,
                    "output_preview": "Opened https://github.com/o/n/pull/7 (draft: no).",
                },
            ),
            (
                started,
                {
                    "call_id": "c6",
                    "name": "mcp__sama__pr_create",
                    "arguments": {"repo": "o/n", "head": "sama/y", "base": "main", "title": "U"},
                },
            ),
            (finished, {"call_id": "c6", "is_error": True, "output_preview": "not allowed"}),
            (
                started,
                {"call_id": "c7", "name": "mcp__sama__sama_pr", "arguments": {"action": "start"}},
            ),
        ],
    )
    async with engine.connect() as connection:
        every = await list_commands(connection, org_id=tenant.org, session_id=session)
        tests = await list_commands(
            connection, org_id=tenant.org, session_id=session, tests_only=True, limit=1
        )
        prs = await list_pull_requests(connection, org_id=tenant.org, session_id=session)
    assert [(c.call_id, c.status, c.test) for c in every] == [
        ("c4", "running", True),
        ("c2", "failed", True),
        ("c1", "ok", False),
    ]
    assert every[1].summary == "1 failed in 0.1s"
    assert every[2].output == "a.py"
    assert every[2].summary is None
    assert [c.call_id for c in tests] == ["c4"]
    assert [(p.call_id, p.status, p.url, p.head) for p in prs] == [
        ("c6", "failed", None, "sama/y"),
        ("c5", "opened", "https://github.com/o/n/pull/7", "sama/x"),
    ]
