"""The commands, test runs, and pull requests of a session (docs/architecture.md, Section 15.5).

The runtimes write a ``tool_call_started`` and a ``tool_call_finished`` run event for each tool
call (Section 5.3). This module reads them, so it works the same for every runtime and every
harness that writes these events. The events are stored redacted (Section 16.3); the module
redacts again, because the redactor of the process can know more secrets now.

The caller reads as the user (``sama.db.as_user``), so row-level security applies.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncConnection

from sama.db.models import RunEvent
from sama.gate.decide import canonical_name
from sama.vault.redact import redact

SHELL_TOOLS: Final = frozenset({"Bash", "execute_code"})
"""The tools whose calls are commands. The second runtime calls its shell ``Bash`` too."""
PR_TOOLS: Final = frozenset({"pr_create", "sama_pr"})
MAX_COMMANDS: Final = 200
SCAN_EVENTS: Final = 2_000
"""How many of the newest ``tool_call_started`` events a list reads at most."""
MAX_OUTPUT_CHARS: Final = 4_000
MAX_PR_OUTPUT_CHARS: Final = 2_000

_TEST_COMMAND: Final = re.compile(
    r"(?:^|[\s;&|(/])(?:"
    r"pytest|py\.test|tox|nox|unittest|"
    r"(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?test|npx\s+(?:jest|vitest|mocha|playwright)|"
    r"jest|vitest|mocha|playwright\s+test|"
    r"go\s+test|cargo\s+test|cargo\s+nextest|mvn\s+(?:-\S+\s+)*test|gradle\w*\s+test|"
    r"\./gradlew\s+test|make\s+(?:test|check)|rspec|bundle\s+exec\s+rspec|rake\s+test|"
    r"phpunit|dotnet\s+test|mix\s+test|ctest|deno\s+test|swift\s+test|uv\s+run\s+pytest"
    r")(?=$|[\s;&|)])",
    re.IGNORECASE,
)
_SUMMARY: Final = re.compile(
    r"(?im)^.*\b(?:\d+\s+(?:passed|failed|errors?|skipped|tests?|examples?|failures?)\b"
    r"|tests?\s+(?:passed|failed)|ok\s+\S+\s+\d|FAIL\b|PASS\b|test result:).*$"
)
_PR_URL: Final = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/pull/\d+")


@dataclass(frozen=True, slots=True)
class CommandView:
    run_id: uuid.UUID
    call_id: str
    tool: str
    command: str
    test: bool
    status: str
    """``running``, ``ok``, or ``failed``."""
    output: str
    summary: str | None
    started_at: datetime
    finished_at: datetime | None


@dataclass(frozen=True, slots=True)
class PullRequestView:
    run_id: uuid.UUID
    call_id: str
    repo: str | None
    title: str | None
    head: str | None
    base: str | None
    url: str | None
    status: str
    """``running``, ``opened``, or ``failed``."""
    output: str
    created_at: datetime


def is_test_command(command: str) -> bool:
    """True when the command runs the tests of a project, for example ``pytest`` or
    ``npm test``."""
    return _TEST_COMMAND.search(command) is not None


def result_summary(output: str) -> str | None:
    """The last result line of a test run in the output, for example ``3 passed in 0.1s``."""
    found = _SUMMARY.findall(output)
    if not found:
        return None
    line: str = found[-1].strip().strip("=").strip()
    return line[:300] or None


def _command_of(tool: str, arguments: Mapping[str, Any]) -> str:
    if tool == "Bash":
        value = arguments.get("command") or arguments.get("cmd")
        if isinstance(value, list):
            return " ".join(str(item) for item in value)
        return str(value or "")
    code = arguments.get("code")
    return f"python: {code}" if isinstance(code, str) else ""


def _tail(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[len(text) - limit :]


async def _started(
    connection: AsyncConnection, org_id: uuid.UUID, session_id: uuid.UUID, names: Sequence[str]
) -> list[Any]:
    rows = (
        await connection.execute(
            select(RunEvent.run_id, RunEvent.payload, RunEvent.created_at)
            .where(
                RunEvent.org_id == org_id,
                RunEvent.session_id == session_id,
                RunEvent.type == "tool_call_started",
                RunEvent.payload["name"].astext.in_(names),
            )
            .order_by(RunEvent.id.desc())
            .limit(SCAN_EVENTS)
        )
    ).all()
    return list(rows)


async def _finished(
    connection: AsyncConnection,
    org_id: uuid.UUID,
    session_id: uuid.UUID,
    calls: Sequence[tuple[uuid.UUID, str]],
) -> dict[tuple[uuid.UUID, str], Any]:
    if not calls:
        return {}
    rows = (
        await connection.execute(
            select(RunEvent.run_id, RunEvent.payload, RunEvent.created_at).where(
                RunEvent.org_id == org_id,
                RunEvent.session_id == session_id,
                RunEvent.type == "tool_call_finished",
                or_(
                    *(
                        and_(RunEvent.run_id == run_id, RunEvent.payload["call_id"].astext == call)
                        for run_id, call in calls
                    )
                ),
            )
        )
    ).all()
    return {(row.run_id, str(row.payload.get("call_id"))): row for row in rows}


def _names(tools: frozenset[str]) -> list[str]:
    return sorted({*tools, *(f"mcp__sama__{tool}" for tool in tools)})


async def list_commands(
    connection: AsyncConnection,
    *,
    org_id: uuid.UUID,
    session_id: uuid.UUID,
    tests_only: bool = False,
    limit: int = 50,
) -> list[CommandView]:
    """The shell and code calls of the session, newest first."""
    limit = max(1, min(limit, MAX_COMMANDS))
    picked: list[tuple[Any, str, str]] = []
    for row in await _started(connection, org_id, session_id, _names(SHELL_TOOLS)):
        payload = row.payload or {}
        tool = canonical_name(str(payload.get("name", "")))
        arguments = payload.get("arguments") or {}
        command = _command_of(tool, arguments if isinstance(arguments, Mapping) else {})
        if tests_only and not is_test_command(command):
            continue
        picked.append((row, tool, command))
        if len(picked) >= limit:
            break
    done = await _finished(
        connection,
        org_id,
        session_id,
        [(row.run_id, str(row.payload.get("call_id"))) for row, _tool, _command in picked],
    )
    out: list[CommandView] = []
    for row, tool, command in picked:
        call_id = str(row.payload.get("call_id"))
        end = done.get((row.run_id, call_id))
        output = "" if end is None else redact(str(end.payload.get("output_preview") or ""))
        test = is_test_command(command)
        status = _status(end)
        out.append(
            CommandView(
                run_id=row.run_id,
                call_id=call_id,
                tool=tool,
                command=redact(command),
                test=test,
                status=status,
                output=_tail(output, MAX_OUTPUT_CHARS),
                summary=result_summary(output) if test else None,
                started_at=row.created_at,
                finished_at=None if end is None else end.created_at,
            )
        )
    return out


async def list_pull_requests(
    connection: AsyncConnection, *, org_id: uuid.UUID, session_id: uuid.UUID, limit: int = 50
) -> list[PullRequestView]:
    """The ``pr_create`` calls and the ``sama_pr`` ``open`` calls of the session, newest
    first."""
    picked: list[tuple[Any, str, Mapping[str, Any]]] = []
    for row in await _started(connection, org_id, session_id, _names(PR_TOOLS)):
        payload = row.payload or {}
        tool = canonical_name(str(payload.get("name", "")))
        arguments = payload.get("arguments") or {}
        if not isinstance(arguments, Mapping):
            continue
        if tool == "sama_pr" and arguments.get("action") != "open":
            continue
        picked.append((row, tool, arguments))
        if len(picked) >= limit:
            break
    done = await _finished(
        connection,
        org_id,
        session_id,
        [(row.run_id, str(row.payload.get("call_id"))) for row, _tool, _args in picked],
    )
    out: list[PullRequestView] = []
    for row, _tool, arguments in picked:
        call_id = str(row.payload.get("call_id"))
        end = done.get((row.run_id, call_id))
        output = "" if end is None else redact(str(end.payload.get("output_preview") or ""))
        found = _PR_URL.search(output)
        if end is None:
            status = "running"
        elif end.payload.get("is_error") or found is None:
            status = "failed"
        else:
            status = "opened"
        out.append(
            PullRequestView(
                run_id=row.run_id,
                call_id=call_id,
                repo=_text(arguments.get("repo")),
                title=_text(arguments.get("title")),
                head=_text(arguments.get("head") or arguments.get("branch")),
                base=_text(arguments.get("base")),
                url=None if found is None else found.group(0),
                status=status,
                output=_tail(output, MAX_PR_OUTPUT_CHARS),
                created_at=row.created_at,
            )
        )
    return out


def _status(end: Any) -> str:
    if end is None:
        return "running"
    return "failed" if end.payload.get("is_error") else "ok"


def _text(value: Any) -> str | None:
    return redact(value)[:300] if isinstance(value, str) and value else None
