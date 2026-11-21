"""``/undo`` and ``/rollback``: restore a checkpoint of the session files (Section 15.3).

The runner takes the checkpoints in the session sandbox (``sama.runtime.checkpoints``). This
service runs the checkpoint command in the sandbox for the channels and the web API. It reads
the list, and it restores a checkpoint. The rules:

- The caller checks that the user can see the session.
- The session must have no open run (queued, running, or paused): a restore must not change
  the files under a run. A restore holds a lock on the session row for the whole command, so
  no run can start during it. The command in the sandbox also waits for the runner lock.
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
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from sama.db.models import ENDED_RUN_STATUSES, AuditEntry, Run, Session
from sama.sandbox.base import Sandbox, SandboxError
from sama.vault.redact import redact

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
    commit: str = ""
    """The id of the checkpoint. It does not change when newer checkpoints come."""


@dataclass(frozen=True, slots=True)
class RestoreView:
    created_at: datetime
    """The time of the checkpoint that the files are now equal to."""
    files: int
    deleted: int
    kept: int = 0
    """Large or excluded items that the restore did not replace (Section 15.3)."""


@dataclass(frozen=True, slots=True)
class FileDiffView:
    path: str
    old_path: str | None
    status: str
    additions: int | None
    deletions: int | None
    binary: bool
    patch: str
    truncated: bool


@dataclass(frozen=True, slots=True)
class DiffView:
    """The changes from checkpoint ``base`` to checkpoint ``head``, or to the current files
    when ``head`` is None (Section 15.5)."""

    base: CheckpointView
    head: CheckpointView | None
    files: list[FileDiffView]
    truncated: bool


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
        return [_view(item) for item in result.get("checkpoints", [])]

    async def diff(
        self, *, org_id: uuid.UUID, session_id: uuid.UUID, base: int | None, head: int | None
    ) -> DiffView:
        """The file changes from checkpoint ``base`` (None: the oldest) to checkpoint ``head``
        (None: the current files). It only reads, so it does not lock the session."""
        args = ["diff", "--base", "oldest" if base is None else str(base)]
        if head is not None:
            args += ["--head", str(head)]
        result = await self._run(org_id, session_id, args)
        if result is None:
            raise CheckpointServiceError(NO_CHECKPOINTS)
        found: dict[str, Any] = result["diff"]
        return DiffView(
            base=_view(found["base"]),
            head=None if found.get("head") is None else _view(found["head"]),
            files=[
                FileDiffView(
                    path=str(item["path"]),
                    old_path=None if item.get("old_path") is None else str(item["old_path"]),
                    status=str(item.get("status", "modified")),
                    additions=_count(item.get("additions")),
                    deletions=_count(item.get("deletions")),
                    binary=bool(item.get("binary")),
                    patch=redact(str(item.get("patch", ""))),
                    truncated=bool(item.get("truncated")),
                )
                for item in found.get("files", [])
            ],
            truncated=bool(found.get("truncated")),
        )

    async def snapshot(
        self, *, org_id: uuid.UUID, session_id: uuid.UUID, run_id: str, tool: str
    ) -> bool:
        """Takes a checkpoint of the session files for a run that does not pass the gate of
        the runner, for example a coding agent CLI (Section 15.5). True when it made a new
        checkpoint. A failure only logs: it never stops the run."""
        try:
            result = await self._run(
                org_id, session_id, ["snapshot", "--run", run_id, "--tool", tool]
            )
        except CheckpointServiceError:
            return False
        return bool(result and result.get("commit"))

    async def undo(
        self, *, org_id: uuid.UUID, user_id: uuid.UUID | None, session_id: uuid.UUID
    ) -> RestoreView:
        """Restores the files before the latest run that changed files."""
        return await self._restore(org_id, user_id, session_id, None)

    async def rollback(
        self,
        *,
        org_id: uuid.UUID,
        user_id: uuid.UUID | None,
        session_id: uuid.UUID,
        number: int,
        commit: str | None = None,
    ) -> RestoreView:
        """Restores checkpoint ``number`` of ``list``. With ``commit``, only when checkpoint
        ``number`` is still that commit."""
        if number < 1:
            raise CheckpointServiceError("The checkpoint number must be 1 or more.")
        return await self._restore(org_id, user_id, session_id, number, commit)

    async def _restore(
        self,
        org_id: uuid.UUID,
        user_id: uuid.UUID | None,
        session_id: uuid.UUID,
        number: int | None,
        commit: str | None = None,
    ) -> RestoreView:
        """Runs a restore with the session row locked (``FOR UPDATE``) for the whole command.

        A new run needs a lock on the session row: ``RunQueue.enqueue`` locks the row, and the
        insert of a run takes a key share lock on it. So no run can start while the restore
        changes the files. The restore does not take the rate lock of the org. A message to
        this session during the restore waits for the row with the rate lock held, so the new
        runs of the org wait until the restore ends (about 1 second; Section 15.3)."""
        args = ["undo"] if number is None else ["restore", "--number", str(number)]
        if number is not None and commit:
            args += ["--commit", commit]
        async with self._engine.begin() as connection:
            workspace_id = await self._reserve(connection, org_id, session_id)
            result = await self._run(org_id, session_id, args)
            if result is None:
                raise CheckpointServiceError(NO_CHECKPOINTS)
            restored: dict[str, Any] = result["restored"]
            view = RestoreView(
                created_at=_time(restored["created_at"]),
                files=int(restored.get("written", 0)),
                deleted=int(restored.get("deleted", 0)),
                kept=int(restored.get("kept", 0)),
            )
            await connection.execute(
                insert(AuditEntry).values(
                    org_id=org_id,
                    workspace_id=workspace_id,
                    actor_kind="user" if user_id is not None else "system",
                    actor_user_id=user_id,
                    action="session.undo" if number is None else "session.rollback",
                    target_type="session",
                    target_id=str(session_id),
                    details={
                        "commit": str(restored.get("commit", "")),
                        "saved": str(restored.get("saved", "")),
                        "number": number,
                        "files": view.files,
                        "deleted": view.deleted,
                        "kept": view.kept,
                    },
                )
            )
        return view

    @staticmethod
    async def _reserve(
        connection: AsyncConnection, org_id: uuid.UUID, session_id: uuid.UUID
    ) -> uuid.UUID:
        """Locks the session row and returns its workspace. Raises when the session has an
        open run."""
        workspace_id = (
            await connection.execute(
                select(Session.workspace_id)
                .where(Session.org_id == org_id, Session.id == session_id)
                .with_for_update()
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


def _view(item: dict[str, Any]) -> CheckpointView:
    return CheckpointView(
        number=int(item["number"]),
        created_at=_time(item["created_at"]),
        tool=str(item.get("tool", "")),
        kind=str(item.get("kind", "tool")),
        commit=str(item.get("commit", "")),
    )


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


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
    kept = (
        f" sama kept {restored.kept} large or excluded items as they are." if restored.kept else ""
    )
    return (
        f"The session files are back to the checkpoint of {_stamp(restored.created_at)}"
        f" ({restored.files} files, {restored.deleted} deleted).{kept}"
        f" To go back, send {rollback} 1."
    )
