"""``/undo`` and ``/rollback``: restore a checkpoint of the session files (Section 15.3).

The runner takes the checkpoints in the session sandbox (``sama.runtime.checkpoints``). This
service runs the checkpoint command in the sandbox for the channels and the web API. It reads
the list, and it restores a checkpoint. The rules:

- The caller checks that the user can see the session.
- The session must have no open run (queued, running, or paused): a restore must not change
  the files under a run. The command in the sandbox also waits for the runner lock.
- A session without a sandbox, or with an outdated sandbox, has no checkpoints.
- Each restore writes an audit entry.
"""

from __future__ import annotations

import json
import logging
import shlex
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.db.models import ENDED_RUN_STATUSES, AuditEntry, Run, Session
from sama.sandbox.base import Sandbox, SandboxError

log: Final = logging.getLogger("sama.orchestrator.checkpoints")

CHECKPOINT_ARGV: Final = ("/opt/sama/.venv/bin/python", "-m", "sama.runtime.checkpoints")
"""The checkpoint command in the sama image (``sandbox/Dockerfile``)."""
COMMAND_TIMEOUT_S: Final = 120.0
LIST_LIMIT: Final = 10
MAX_LIST_LIMIT: Final = 50

NO_CHECKPOINTS: Final = "This session has no checkpoints yet."
BUSY: Final = "A run is open in this session. Wait until it ends or stop it, then try again."
UNAVAILABLE: Final = "sama cannot read the checkpoints now. Try again later."

SessionSandbox = Callable[[str, str], Awaitable[Sandbox | None]]


class CheckpointServiceError(Exception):
    """The message is for the user."""


@dataclass(frozen=True, slots=True)
class CheckpointView:
    number: int
    """1 is the newest checkpoint."""
    created_at: datetime
    tool: str
    """The tool call that came after the checkpoint, or ``rollback``."""
    kind: str
    """``tool``: before a tool call. ``rollback``: the files before a restore."""


@dataclass(frozen=True, slots=True)
class RestoreView:
    created_at: datetime
    """The time of the checkpoint that the files are now equal to."""
    files: int
    deleted: int


class CheckpointService:
    def __init__(
        self,
        engine: AsyncEngine,
        session_sandbox: SessionSandbox,
        *,
        argv: Sequence[str] = CHECKPOINT_ARGV,
        timeout_s: float = COMMAND_TIMEOUT_S,
    ) -> None:
        self._engine = engine
        self._session_sandbox = session_sandbox
        self._argv = tuple(argv)
        self._timeout_s = timeout_s

    async def listing(
        self, *, org_id: uuid.UUID, session_id: uuid.UUID, limit: int = LIST_LIMIT
    ) -> list[CheckpointView]:
        limit = max(1, min(limit, MAX_LIST_LIMIT))
        result = await self._run(org_id, session_id, ["list", "--limit", str(limit)])
        if result is None:
            return []
        return [
            CheckpointView(
                number=int(item["number"]),
                created_at=_time(item["created_at"]),
                tool=str(item.get("tool", "")),
                kind=str(item.get("kind", "tool")),
            )
            for item in result.get("checkpoints", [])
        ]

    async def undo(
        self, *, org_id: uuid.UUID, user_id: uuid.UUID | None, session_id: uuid.UUID
    ) -> RestoreView:
        """Restores the files before the latest run that changed files."""
        return await self._restore(org_id, user_id, session_id, ["undo"], "session.undo")

    async def rollback(
        self, *, org_id: uuid.UUID, user_id: uuid.UUID | None, session_id: uuid.UUID, number: int
    ) -> RestoreView:
        """Restores checkpoint ``number`` of ``list``."""
        if number < 1:
            raise CheckpointServiceError("The checkpoint number must be 1 or more.")
        args = ["restore", "--number", str(number)]
        return await self._restore(org_id, user_id, session_id, args, "session.rollback")

    async def _restore(
        self,
        org_id: uuid.UUID,
        user_id: uuid.UUID | None,
        session_id: uuid.UUID,
        args: list[str],
        action: str,
    ) -> RestoreView:
        workspace_id = await self._check_idle(org_id, session_id)
        result = await self._run(org_id, session_id, args)
        if result is None:
            raise CheckpointServiceError(NO_CHECKPOINTS)
        restored: dict[str, Any] = result["restored"]
        async with self._engine.begin() as connection:
            await connection.execute(
                insert(AuditEntry).values(
                    org_id=org_id,
                    workspace_id=workspace_id,
                    actor_kind="user" if user_id is not None else "system",
                    actor_user_id=user_id,
                    action=action,
                    target_type="session",
                    target_id=str(session_id),
                    details={
                        "commit": str(restored.get("commit", "")),
                        "saved": str(restored.get("saved", "")),
                        "number": int(args[-1]) if args[0] == "restore" else None,
                        "files": int(restored.get("written", 0)),
                        "deleted": int(restored.get("deleted", 0)),
                    },
                )
            )
        return RestoreView(
            created_at=_time(restored["created_at"]),
            files=int(restored.get("written", 0)),
            deleted=int(restored.get("deleted", 0)),
        )

    async def _check_idle(self, org_id: uuid.UUID, session_id: uuid.UUID) -> uuid.UUID:
        """Returns the workspace of the session. Raises when the session has an open run."""
        async with self._engine.connect() as connection:
            workspace_id = (
                await connection.execute(
                    select(Session.workspace_id).where(
                        Session.org_id == org_id, Session.id == session_id
                    )
                )
            ).scalar_one_or_none()
            if workspace_id is None:
                raise CheckpointServiceError("The session does not exist.")
            busy = (
                await connection.execute(
                    select(Run.id)
                    .where(
                        Run.org_id == org_id,
                        Run.session_id == session_id,
                        Run.status.not_in(ENDED_RUN_STATUSES),
                    )
                    .limit(1)
                )
            ).first()
        if busy is not None:
            raise CheckpointServiceError(BUSY)
        return workspace_id

    async def _run(
        self, org_id: uuid.UUID, session_id: uuid.UUID, args: list[str]
    ) -> dict[str, Any] | None:
        """Runs the checkpoint command in the session sandbox. None when the session has no
        sandbox. Raises ``CheckpointServiceError`` with the message of a failed command."""
        try:
            sandbox = await self._session_sandbox(str(org_id), str(session_id))
            if sandbox is None:
                return None
            argv = [*self._argv, "--workdir", sandbox.workdir, *args]
            done = await sandbox.exec(argv, cwd=sandbox.workdir, timeout_s=self._timeout_s)
        except SandboxError as error:
            log.warning("the checkpoint command did not run session=%s: %r", session_id, error)
            raise CheckpointServiceError(UNAVAILABLE) from None
        lines = done.stdout.strip().splitlines()
        try:
            result: dict[str, Any] = json.loads(lines[-1]) if lines else {}
        except ValueError:
            result = {}
        if done.exit_code != 0 or "ok" not in result:
            log.warning(
                "the checkpoint command failed session=%s argv=%s code=%s stderr=%s",
                session_id,
                shlex.join(args),
                done.exit_code,
                done.stderr[-500:],
            )
            raise CheckpointServiceError(UNAVAILABLE)
        if not result["ok"]:
            raise CheckpointServiceError(str(result.get("error") or UNAVAILABLE))
        return result


def _time(value: Any) -> datetime:
    return datetime.fromtimestamp(int(value), tz=UTC)


def _stamp(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M UTC")


CHECKPOINT_COMMANDS: Final = frozenset({"undo", "rollback"})


async def checkpoint_reply(
    service: CheckpointService | None,
    *,
    org_id: uuid.UUID,
    user_id: uuid.UUID | None,
    session_id: uuid.UUID | None,
    name: str,
    rest: str,
    prefix: str = "/",
) -> str:
    """The answer to ``undo`` or ``rollback`` from a chat. ``prefix`` is how the channel writes
    a command: ``/`` in Telegram, ``/sama `` in Slack."""
    if service is None:
        return "Checkpoints are not available here."
    if session_id is None:
        return NO_CHECKPOINTS
    rollback = f"{prefix}rollback"
    try:
        if name == "undo":
            restored = await service.undo(org_id=org_id, user_id=user_id, session_id=session_id)
            return restored_text(restored, rollback)
        argument = rest.strip()
        if not argument:
            listed = await service.listing(org_id=org_id, session_id=session_id)
            return list_text(listed, rollback, f"{prefix}undo")
        if not argument.isdigit():
            return f"Write the number of a checkpoint, for example: {rollback} 2"
        restored = await service.rollback(
            org_id=org_id, user_id=user_id, session_id=session_id, number=int(argument)
        )
        return restored_text(restored, rollback)
    except CheckpointServiceError as error:
        return str(error)


def list_text(listed: Sequence[CheckpointView], rollback: str, undo: str) -> str:
    if not listed:
        return NO_CHECKPOINTS
    lines = ["Checkpoints of the session files, newest first:"]
    for checkpoint in listed:
        reason = (
            "the files before a restore"
            if checkpoint.kind == "rollback"
            else f"before {checkpoint.tool}"
        )
        lines.append(f"{checkpoint.number}. {_stamp(checkpoint.created_at)} - {reason}")
    lines.append(
        f"Send {rollback} NUMBER to restore one. {undo} restores the files before the last run."
    )
    return "\n".join(lines)


def restored_text(restored: RestoreView, rollback: str) -> str:
    return (
        f"The session files are back to the checkpoint of {_stamp(restored.created_at)}"
        f" ({restored.files} files, {restored.deleted} deleted). To go back, send {rollback} 1."
    )
