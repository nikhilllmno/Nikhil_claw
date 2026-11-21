"""The purge worker: the cleanups outside Postgres after a delete (docs/architecture.md,
Section 17.4).

A delete writes ``data_purges`` rows in its own transaction. This loop does each cleanup and
tries again with a backoff until it works:

- ``sandboxes``: kills the E2B sandboxes of the deleted sessions. It finds them by their
  labels, so it kills only a sandbox of the org and of a deleted session;
- ``traces``: asks Langfuse to delete the traces of the deleted runs. The delete writes these
  rows with a later ``next_attempt_at`` (``TRACE_DELAY``).

A process without E2B or without Langfuse leaves those rows for a process that has them.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import Mapping, Sequence
from datetime import timedelta
from typing import Any, Final, Protocol

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.db.models import DataPurge

log: Final = logging.getLogger("sama.datacontrols.purge")

ORG_LABEL: Final = "sama.org_id"
SESSION_LABEL: Final = "sama.session_id"
FIND_LIMIT: Final = 100
"""A label search returns at most this many sandboxes. A full page means that more can wait."""
LEASE: Final = timedelta(minutes=5)
"""A claimed row waits this long before another worker can claim it."""
MAX_BACKOFF: Final = timedelta(hours=6)
KEEP_DONE: Final = timedelta(days=30)
"""Done rows stay this long for the admins, then the worker deletes them."""
BATCH: Final = 20


class FoundSandbox(Protocol):
    @property
    def sandbox_id(self) -> str: ...

    @property
    def labels(self) -> Mapping[str, str]: ...


class SandboxBackend(Protocol):
    """Finds sandboxes by labels and kills them. ``E2BHostBackend`` in production."""

    async def find(self, labels: Mapping[str, str]) -> Sequence[FoundSandbox]: ...

    async def kill(self, sandbox_id: str) -> None: ...


class TraceDeleter(Protocol):
    """``LangfuseClient`` in production."""

    async def delete_traces(self, trace_ids: Sequence[str]) -> None: ...


class PurgeWorker:
    def __init__(
        self,
        engine: AsyncEngine,
        *,
        sandboxes: SandboxBackend | None = None,
        traces: TraceDeleter | None = None,
    ) -> None:
        self._engine = engine
        self._sandboxes = sandboxes
        self._traces = traces

    @property
    def kinds(self) -> tuple[str, ...]:
        """The kinds of rows that this worker can do."""
        return tuple(
            kind
            for kind, backend in (("sandboxes", self._sandboxes), ("traces", self._traces))
            if backend is not None
        )

    async def serve(self, stop: asyncio.Event, *, interval_s: float = 30.0) -> None:
        while not stop.is_set():
            try:
                await self.run_once()
            except Exception:
                log.exception("the data purge failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=interval_s)

    async def run_once(self) -> int:
        """Does the due rows. Returns how many are done."""
        done = 0
        for row in await self._claim():
            try:
                complete = await self._purge(row.org_id, row.kind, row.refs)
            except Exception as error:
                # Only the type: the message of a provider error can hold a URL or a key.
                await self._failed(row.id, row.attempts, type(error).__name__)
                continue
            if complete:
                await self._done(row.id)
                done += 1
            else:
                await self._failed(row.id, 0, "more sandboxes remain")
        await self._forget_old()
        return done

    async def _claim(self) -> list[Any]:
        kinds = self.kinds
        if not kinds:
            return []
        async with self._engine.begin() as connection:
            rows = (
                await connection.execute(
                    select(DataPurge.id, DataPurge.org_id, DataPurge.kind, DataPurge.refs)
                    .where(
                        DataPurge.done_at.is_(None),
                        DataPurge.next_attempt_at <= func.now(),
                        DataPurge.kind.in_(kinds),
                    )
                    .order_by(DataPurge.next_attempt_at)
                    .limit(BATCH)
                    .with_for_update(skip_locked=True)
                )
            ).all()
            if not rows:
                return []
            claimed = await connection.execute(
                update(DataPurge)
                .where(DataPurge.id.in_([row.id for row in rows]))
                .values(
                    attempts=DataPurge.attempts + 1,
                    next_attempt_at=func.now() + LEASE,
                )
                .returning(
                    DataPurge.id,
                    DataPurge.org_id,
                    DataPurge.kind,
                    DataPurge.refs,
                    DataPurge.attempts,
                )
            )
            return list(claimed.all())

    async def _purge(self, org_id: uuid.UUID, kind: str, refs: Mapping[str, Any]) -> bool:
        if kind == "traces":
            if self._traces is None:
                return False
            await self._traces.delete_traces([str(trace) for trace in refs.get("trace_ids", [])])
            return True
        if self._sandboxes is None:
            return False
        org = str(org_id)
        sessions: list[str | None] = (
            [None] if refs.get("all") else [str(session) for session in refs.get("session_ids", [])]
        )
        complete = True
        for session in sessions:
            labels = {ORG_LABEL: org} | ({SESSION_LABEL: session} if session else {})
            found = await self._sandboxes.find(labels)
            for sandbox in found:
                # The labels prove that the sandbox is of the org and of the deleted session.
                if all(sandbox.labels.get(key) == value for key, value in labels.items()):
                    await self._sandboxes.kill(sandbox.sandbox_id)
            complete = complete and len(found) < FIND_LIMIT
        return complete

    async def _done(self, row_id: uuid.UUID) -> None:
        async with self._engine.begin() as connection:
            await connection.execute(
                update(DataPurge)
                .where(DataPurge.id == row_id)
                .values(done_at=func.now(), last_error=None)
            )

    async def _failed(self, row_id: uuid.UUID, attempts: int, error: str) -> None:
        wait = min(timedelta(seconds=30) * 2 ** min(attempts, 20), MAX_BACKOFF)
        log.warning("a data purge failed and waits %s: %s", wait, error)
        async with self._engine.begin() as connection:
            await connection.execute(
                update(DataPurge)
                .where(DataPurge.id == row_id)
                .values(last_error=error[:500], next_attempt_at=func.now() + wait)
            )

    async def _forget_old(self) -> None:
        async with self._engine.begin() as connection:
            await connection.execute(
                delete(DataPurge).where(
                    DataPurge.done_at.is_not(None), DataPurge.done_at < func.now() - KEEP_DONE
                )
            )
