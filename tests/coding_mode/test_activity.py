"""The commands, test runs, and pull requests of a session (Section 15.5)."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.coding_mode.activity import (
    failed_summary,
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
        "sh test.sh",
        "bash scripts/run_tests.sh",
        "./test.sh",
        "cd repo && sh -e tests.sh",
        "./scripts/test-unit.sh",
        'bash "scripts/run_tests.sh"',
        "cd repo && ./test.sh",
        "cd repo\nsh test.sh",
        "pip install pytest && pytest -q",
        "pytest --version; pytest -q",
        "ctest -V",
        "mvn -V test",
        "pytest -q | grep -h failed",
        "  ./test.sh",
    ],
)
def test_test_commands(command: str) -> None:
    assert is_test_command(command)


@pytest.mark.parametrize(
    "command",
    [
        "ls",
        "cat pytest.ini",
        "npm install",
        "git status",
        "cat test.sh",
        "vi test.sh",
        "sh build.sh",
        "sh latest.sh",
        "bash deploy.sh",
        "cat ./test.sh",
        "git diff -- ./test.sh",
        "python3 -m pytest --version 2>&1 || pip install pytest",
        "pytest -h",
        "python3 -m pytest -V",
        "npm test --help",
        "pip install pytest",
        "uv add --dev pytest",
        "npm i -D vitest",
        "cargo install cargo-nextest",
    ],
)
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


@pytest.mark.parametrize(
    ("line", "failed"),
    [
        ("1 failed, 1 passed in 0.01s", True),
        ("2 errors in 0.3s", True),
        ("Tests: 1 failed, 3 passed", True),
        ("test result: FAILED. 1 passed; 1 failed", True),
        ("not ok 2 - adds", True),
        ("FAILED tests/test_a.py::test_x - assert 1 == 2", True),
        ("1 Failed", True),
        ("2 tests failed", True),
        ("collected 3 items\n...\nFAILED tests/a.py::t\n== 1 passed ==", True),
        ("test_errors.py PASSED", False),
        ("100% tests passed, 0 tests failed out of 4", False),
        ("tests/test_labels.py::test_label[FAIL] PASSED [100%]", False),
        ("tests/t.py::test_x[FAILED] PASSED", False),
        ("FAILSAFE mode is on", False),
        ("3 tests failed out of 4", True),
        ("tests/a.py::t FAILED [ 50%]", True),
        ("ERROR tests/a.py - ImportError", True),
        ("--- FAIL: TestX (0.00s)", True),
        ("FAIL src/a.test.ts", True),
        ("3 passed in 0.1s", False),
        ("0 failed, 2 passed", False),
        ("Tests  12 passed (12)", False),
        ("PASS src/a.test.ts", False),
    ],
)
def test_a_failed_summary(line: str, failed: bool) -> None:
    assert failed_summary(line) is failed


async def test_a_piped_test_with_a_failed_summary_is_failed(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    run_id = await RunQueue(engine).enqueue(
        org_id=tenant.org, session_id=tenant.ids["mine"], text="go", user_id=None
    )
    started, finished = "tool_call_started", "tool_call_finished"
    piped = {"command": "pytest -q 2>&1 | tail -15"}
    await _events(
        engine,
        tenant,
        run_id,
        [
            (started, {"call_id": "p1", "name": "Bash", "arguments": piped}),
            (
                finished,
                {
                    "call_id": "p1",
                    "is_error": False,
                    "output_preview": "1 failed, 1 passed in 0.01s",
                },
            ),
            (started, {"call_id": "p2", "name": "Bash", "arguments": {"summary": "sh test.sh"}}),
            (finished, {"call_id": "p2", "is_error": False, "output_preview": "2 passed"}),
            (started, {"call_id": "p3", "name": "Bash", "arguments": {"command": "pytest"}}),
            (finished, {"call_id": "p3", "is_error": True, "output_preview": "3 passed"}),
            (started, {"call_id": "p4", "name": "Bash", "arguments": {"summary": "prove | cat"}}),
            (
                started,
                {"call_id": "p5", "name": "Bash", "arguments": {"command": "sh test.sh | tail"}},
            ),
            (
                finished,
                {"call_id": "p5", "is_error": False, "output_preview": "ok 1 - a\nnot ok 2 - b"},
            ),
            (started, {"call_id": "p6", "name": "Bash", "arguments": {"command": "pytest | tail"}}),
            (
                finished,
                {"call_id": "p6", "is_error": False, "output_preview": "FAILED tests/a.py::t"},
            ),
        ],
    )
    async with engine.connect() as connection:
        listed = await list_commands(
            connection, org_id=tenant.org, session_id=tenant.ids["mine"], tests_only=True
        )
    assert [(c.call_id, c.status, c.summary) for c in listed] == [
        ("p6", "failed", "FAILED tests/a.py::t"),
        ("p5", "failed", "not ok 2 - b"),
        ("p3", "failed", "3 passed"),
        ("p2", "ok", "2 passed"),
        ("p1", "failed", "1 failed, 1 passed in 0.01s"),
    ]
