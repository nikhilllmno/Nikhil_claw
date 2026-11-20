"""The write path of the call records (docs/architecture.md, Section 16.6 and 11.7).

Every paid model call must reach ``model_calls``, because the budget checks read the spend from
there. A record that the store does not take blocks the new calls of its org until a later write
succeeds. The model proxy and ``vision_analyze`` use one ledger in the control plane, so one
failed write fences both.
"""

from __future__ import annotations

import asyncio
import logging

import anyio

from sama.modelproxy.store import CallRecord, UsageStore

log = logging.getLogger("sama.modelproxy")

RECORD_ATTEMPTS = 3
RECORD_RETRY_S = 0.2


class UsageLedger:
    def __init__(
        self,
        store: UsageStore,
        *,
        attempts: int = RECORD_ATTEMPTS,
        retry_s: float = RECORD_RETRY_S,
    ) -> None:
        self._store = store
        self._attempts = attempts
        self._retry_s = retry_s
        self._flush_lock = asyncio.Lock()
        self.unrecorded: list[CallRecord] = []
        """Records that the store did not take. They block new calls of their org until a
        write succeeds. The record id makes a second write of the same record harmless."""

    async def save(self, call: CallRecord) -> bool:
        """Writes the record with retries. ``False`` if the store did not take it."""
        for attempt in range(self._attempts):
            if attempt:
                await anyio.sleep(self._retry_s * attempt)
            try:
                await self._store.record(call)
            except Exception:
                log.exception("the call record write failed run=%s", call.run_id)
            else:
                return True
        return False

    def fence(self, call: CallRecord) -> None:
        """Blocks the org of the call until a write of the record succeeds."""
        if call in self.unrecorded:
            return
        self.unrecorded.append(call)
        log.error("the call record was not written; the org is blocked run=%s", call.run_id)

    async def settle(self, call: CallRecord) -> None:
        """Writes the record, or fences it. A cancel during the write fences it too."""
        try:
            if not await self.save(call):
                self.fence(call)
        except asyncio.CancelledError:
            self.fence(call)
            raise

    def blocked(self, org_id: str) -> bool:
        return any(call.org_id == org_id for call in self.unrecorded)

    async def flush(self) -> None:
        """Tries again to write the records that the store did not take. A record stays in
        ``unrecorded``, so it blocks its org, until its write succeeds."""
        if not self.unrecorded:
            return
        async with self._flush_lock:
            for call in list(self.unrecorded):
                try:
                    await self._store.record(call)
                except Exception:
                    log.exception("the call record write failed again run=%s", call.run_id)
                    continue
                self.unrecorded.remove(call)
