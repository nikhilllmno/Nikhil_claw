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
_TEST_SCRIPT: Final = re.compile(
    r"(?:^[ \t]*|[;&|(]\s*)(?:(?:ba|z|da)?sh\s+(?:-\S+\s+)*)?[\"']?"
    r"(?:\.?/)?(?:[\w.-]+/)*(?:[\w.-]*[_.-])?(?:run[_-]?)?tests?(?:[_.-][\w.-]*)?\.(?:sh|bash)"
    r"[\"']?(?=$|[\s;&|)])",
    re.IGNORECASE | re.MULTILINE,
)
"""A test script in the place of a command: ``sh test.sh``, ``bash "scripts/run_tests.sh"``,
``./test.sh``, ``cd repo && ./test.sh``. A script name as an argument (``cat ./test.sh``,
``git diff -- test.sh``) is not a test run."""
_FAILED_LINE: Final = re.compile(
    # A positive count: ``1 failed``, ``2 errors``, ``3 tests failed`` (not ``0 tests failed``).
    r"(?im)^.*(?:\b[1-9]\d*\s+(?:tests?\s+)?(?:failed|failures?|errors?|errored)\b"
    r"|test result:\s*failed).*$"
    # A status word in a status place only: at the start of the line (pytest ``FAILED x``,
    # ``ERROR x``, jest ``FAIL src/a.test.ts``, go ``--- FAIL:``), at the end of a verbose
    # pytest line (``tests/a.py::t FAILED [ 50%]``), or a TAP ``not ok``. A parameter id such
    # as ``test_x[FAIL] PASSED`` does not count.
    r"|^[ \t]*(?:---[ \t]+)?(?-i:FAIL|FAILED|ERROR)\b.*$"
    r"|^.*[ \t](?-i:FAILED|ERROR)(?:[ \t]+\[[ \t]*\d+%\])?[ \t]*$"
    r"|^[ \t]*not ok\b.*$"
)
"""A line that reports at least one failed test or error. ``0 failed`` does not count."""
_SUMMARY: Final = re.compile(
    r"(?im)^.*\b(?:\d+\s+(?:passed|failed|errors?|skipped|tests?|examples?|failures?)\b"
    r"|tests?\s+(?:passed|failed)|ok\s+\S+\s+\d|FAIL\b|PASS\b|test result:).*$"
    r"|^\s*not ok\b.*$"
)
_PARTS: Final = re.compile(r"&&|\|\||[;\n]")
"""Splits a shell line into its commands. A pipe stays in one part: ``pytest | tail`` is one
test run."""
_INFO: Final = re.compile(r"(?:^|\s)(?:--version|--help|-h|-V)(?=$|\s)")
"""``pytest --version`` and ``npm test --help`` only show information; they run no test."""
_INSTALL: Final = re.compile(
    r"(?:^|[\s(])(?:python3?\s+-m\s+)?(?:pip3?|uv\s+pip|uv|npm|pnpm|yarn|bun|poetry|conda"
    r"|apt(?:-get)?|brew|gem|cargo)\s+(?:install|add|i)\b"
)
"""``pip install pytest`` installs a test tool; it runs no test."""
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
    ``npm test``, or a test script such as ``sh test.sh``."""
    return any(
        (_TEST_COMMAND.search(part) or _TEST_SCRIPT.search(part))
        and not _INFO.search(part)
        and not _INSTALL.search(part)
        for part in _PARTS.split(command)
    )


def result_summary(output: str) -> str | None:
    """The last result line of a test run in the output, for example ``3 passed in 0.1s``."""
    found = _SUMMARY.findall(output)
    if not found:
        return None
    line: str = found[-1].strip().strip("=").strip()
    return line[:300] or None


def failed_summary(text: str) -> bool:
    """True when the text has a line that reports at least one failed test or error, for
    example ``1 failed``, ``2 errors``, ``FAILED``, or ``not ok``. It reads the whole text,
    not only the summary line."""
    return _FAILED_LINE.search(text) is not None


def _failed_line(text: str) -> str | None:
    found = [match.group(0) for match in _FAILED_LINE.finditer(text)]
    return found[-1].strip()[:300] if found else None


def _command_of(tool: str, arguments: Mapping[str, Any]) -> str:
    if tool == "Bash":
        # ``summary``: the one-line command of a Claude Code or Codex run (Section 15.4).
        value = arguments.get("command") or arguments.get("cmd") or arguments.get("summary")
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
        summary = result_summary(output) if test else None
        status = _status(end)
        if test and status == "ok" and failed_summary(output):
            # The exit status of a pipe is the one of its last command, so ``pytest | tail``
            # ends with 0 after a failed test. A line that reports a failure wins
            # (Section 15.5).
            status = "failed"
            if summary is None or not failed_summary(summary):
                summary = _failed_line(output)
        out.append(
            CommandView(
                run_id=row.run_id,
                call_id=call_id,
                tool=tool,
                command=redact(command),
                test=test,
                status=status,
                output=_tail(output, MAX_OUTPUT_CHARS),
                summary=summary,
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
