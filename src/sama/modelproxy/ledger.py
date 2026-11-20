"""The write path of the call records (docs/architecture.md, Section 16.6 and 11.7).

Every paid model call must reach ``model_calls``, because the budget checks read the spend from
there. A record that the store does not take blocks the new calls of its org until a later write
succeeds. The model proxy and ``vision_analyze`` use one ledger in the control plane, so one
failed write fences both.

A write that stalls without an error is as bad as a write that fails. So a record is "pending"
from the moment its settlement starts, and a pending record blocks its org like a fenced one. A
budget check waits a short time for the pending writes of its org (they take milliseconds), and
refuses the call if one still runs. Each write attempt has a time limit.
"""

from __future__ import annotations

import asyncio
import logging
import uuid

import anyio

from sama.modelproxy.store import CallRecord, UsageStore

log = logging.getLogger("sama.modelproxy")

RECORD_ATTEMPTS = 3
RECORD_RETRY_S = 0.2
WRITE_TIMEOUT_S = 5.0
"""The time limit of one write attempt."""
WAIT_S = 2.0
"""How long a budget check waits for the pending writes of its org."""


class UsageLedger:
    def __init__(
        self,
        store: UsageStore,
        *,
        attempts: int = RECORD_ATTEMPTS,
        retry_s: float = RECORD_RETRY_S,
        write_timeout_s: float = WRITE_TIMEOUT_S,
        wait_s: float = WAIT_S,
    ) -> None:
        self._store = store
        self._attempts = attempts
        self._retry_s = retry_s
        self._write_timeout_s = write_timeout_s
        self._wait_s = wait_s
        self._flush_lock = asyncio.Lock()
        self._pending: dict[uuid.UUID, tuple[CallRecord, asyncio.Future[None]]] = {}
        self.unrecorded: list[CallRecord] = []
        """Records that the store did not take. They block new calls of their org until a
        write succeeds. The record id makes a second write of the same record harmless."""

    async def save(self, call: CallRecord) -> bool:
        """Writes the record with retries. ``False`` if the store did not take it."""
        for attempt in range(self._attempts):
            if attempt:
                await anyio.sleep(self._retry_s * attempt)
            if await self._write(call):
                return True
        return False

    async def _write(self, call: CallRecord) -> bool:
        """One write attempt with a time limit. A write that the limit stops can have reached
        the store, so the next attempt uses the same ``call_id``."""
        try:
            async with asyncio.timeout(self._write_timeout_s):
                await self._store.record(call)
        except Exception:
            log.exception("the call record write failed run=%s", call.run_id)
            return False
        return True

    def fence(self, call: CallRecord) -> None:
        """Blocks the org of the call until a write of the record succeeds."""
        if call in self.unrecorded:
            return
        self.unrecorded.append(call)
        log.error("the call record was not written; the org is blocked run=%s", call.run_id)

    async def settle(self, call: CallRecord) -> None:
        """Writes the record, or fences it. The record blocks its org from the start to the
        end of the write. A cancel during the write fences it, so it never leaves silently."""
        done: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._pending[call.call_id] = (call, done)
        try:
            if not await self.save(call):
                self.fence(call)
        except asyncio.CancelledError:
            self.fence(call)
            raise
        finally:
            self._pending.pop(call.call_id, None)
            done.set_result(None)

    def blocked(self, org_id: str) -> bool:
        """``True`` if the org has a record that is not written: pending or fenced."""
        return any(call.org_id == org_id for call in self.unrecorded) or any(
            call.org_id == org_id for call, _ in self._pending.values()
        )

    async def flush(self, org_id: str | None = None) -> None:
        """Waits for the pending writes (of ``org_id``, or of all orgs) for at most ``wait_s``.
        Then tries again to write the fenced records. A record stays in ``unrecorded``, so it
        blocks its org, until its write succeeds."""
        waiting = [
            done
            for call, done in list(self._pending.values())
            if org_id is None or call.org_id == org_id
        ]
        if waiting:
            await asyncio.wait(waiting, timeout=self._wait_s)
        if not self.unrecorded:
            return
        try:
            async with asyncio.timeout(self._wait_s):
                await self._flush_lock.acquire()  # another flush runs: wait for it
        except TimeoutError:
            return  # the records still block their orgs
        try:
            for call in list(self.unrecorded):
                if await self._write(call) and call in self.unrecorded:
                    self.unrecorded.remove(call)
        finally:
            self._flush_lock.release()
