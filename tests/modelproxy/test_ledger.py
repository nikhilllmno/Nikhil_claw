"""The shared write path of the call records (docs/architecture.md, Section 16.6 and 11.7)."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from sama.modelproxy.ledger import UsageLedger
from sama.modelproxy.prices import Provider, TokenCounts
from sama.modelproxy.store import CallRecord, MemoryUsageStore
from sama.modelproxy.usage import Endpoint


class Store(MemoryUsageStore):
    def __init__(self, failures: int = 0, *, hang: asyncio.Event | None = None) -> None:
        super().__init__()
        self.failures = failures
        self.hang = hang
        self.attempts = 0

    async def record(self, call: CallRecord) -> None:
        self.attempts += 1
        if self.hang is not None:
            await self.hang.wait()
        if self.failures > 0:
            self.failures -= 1
            raise RuntimeError("down")
        await super().record(call)


def call(org: str = "org-1") -> CallRecord:
    return CallRecord(
        org_id=org,
        workspace_id="ws",
        session_id="s",
        run_id="r",
        user_id=None,
        provider=Provider.ANTHROPIC,
        model="m",
        endpoint=Endpoint.MESSAGES,
        status_code=200,
        counts=TokenCounts(input=1, output=1),
        cost_usd=Decimal("0.01"),
        duration_ms=1,
        created_at=datetime.now(UTC),
        call_id=uuid.uuid4(),
    )


async def test_a_write_is_retried_and_a_failure_fences_the_org() -> None:
    store = Store(failures=2)
    ledger = UsageLedger(store, attempts=3, retry_s=0)
    first = call()
    await ledger.settle(first)
    assert store.attempts == 3
    assert ledger.unrecorded == []
    assert len(store.calls) == 1

    store.failures = 100
    second = call()
    await ledger.settle(second)
    assert ledger.unrecorded == [second]
    assert ledger.blocked("org-1")
    assert not ledger.blocked("org-2")


async def test_a_flush_ends_the_fence_only_after_a_write_succeeds() -> None:
    store = Store(failures=100)
    ledger = UsageLedger(store, retry_s=0)
    record = call()
    await ledger.settle(record)
    await ledger.flush()
    assert ledger.blocked("org-1"), "the store still fails"
    store.failures = 0
    await ledger.flush()
    assert not ledger.blocked("org-1")
    assert store.calls == [record]


async def test_a_fence_is_idempotent() -> None:
    ledger = UsageLedger(Store())
    record = call()
    ledger.fence(record)
    ledger.fence(record)
    assert ledger.unrecorded == [record]


async def test_a_cancel_during_the_write_fences_the_record() -> None:
    hang = asyncio.Event()
    store = Store(hang=hang)
    ledger = UsageLedger(store, retry_s=0)
    record = call()
    task = asyncio.create_task(ledger.settle(record))
    while store.attempts == 0:  # noqa: ASYNC110 - the task has to reach the store
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert ledger.unrecorded == [record]
    hang.set()
    await ledger.flush()
    assert ledger.unrecorded == []
    assert store.calls == [record]
