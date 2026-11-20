"""A usage write that stalls must not open the budget (docs/architecture.md, Section 11.7).

A write that never ends and never fails is as bad as a write that fails: the next call would
see no spend. The ledger holds each record as pending from the start of its settlement.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable

import pytest
from sqlalchemy import func, insert, select, text
from sqlalchemy.ext.asyncio import AsyncEngine

import sama.media.vision as vision_module
from sama.db.models import ModelCall, Run
from sama.media.vision import VisionError
from sama.modelproxy.ledger import UsageLedger
from sama.modelproxy.store import CallRecord, MemoryUsageStore, PostgresUsageStore, UsageStore
from sama.tenancy.run_token import RunTokenClaims
from tests.media.test_vision import Anthropic, Rig, build
from tests.orchestrator.conftest import Tenant

BUDGET = 1
"""One token: every recorded call, also an estimate, uses it up."""


class StalledUsage(MemoryUsageStore):
    """A usage store whose ``record`` never ends until ``recover`` is called."""

    def __init__(self) -> None:
        super().__init__()
        self.recovered = asyncio.Event()
        self.attempts: list[uuid.UUID] = []

    async def record(self, call: CallRecord) -> None:
        self.attempts.append(call.call_id)
        await self.recovered.wait()
        await super().record(call)


def ledger_of(usage: UsageStore) -> UsageLedger:
    """Short limits, so that a stall shows in milliseconds."""
    return UsageLedger(usage, attempts=2, retry_s=0, write_timeout_s=0.3, wait_s=0.05)


async def until(condition: Callable[[], bool]) -> None:
    for _ in range(500):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the condition did not become true")


async def cancelled_call(rig: Rig, api: Anthropic, claims: RunTokenClaims | None = None) -> None:
    """A caller that goes away after the provider accepted the call. Then the provider
    answers, and the record write starts."""
    assert api.accepted is not None
    assert api.hold is not None
    api.accepted.clear()
    api.hold.clear()
    caller = asyncio.create_task(
        rig.service.analyze(claims or rig.claims(max_tokens=BUDGET), "latest", "?")
    )
    await asyncio.wait_for(api.accepted.wait(), 5)
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    api.hold.set()


def held_api() -> Anthropic:
    return Anthropic(hold=asyncio.Event(), accepted=asyncio.Event())


async def test_a_stalled_write_and_a_cancel_refuse_the_next_calls(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    usage = StalledUsage()
    ledger = ledger_of(usage)
    api = held_api()
    rig = build(engine, tenant, usage=usage, api=api, ledger=ledger)
    await rig.image()
    org = str(tenant.org)

    await cancelled_call(rig, api)
    await until(lambda: bool(usage.attempts))  # the first write attempt runs and does not end
    assert usage.calls == [], "the store has not taken the record"
    assert len(api.requests) == 1

    # While the write stalls, in its first attempt and after it fails, no call gets through.
    for _ in range(3):
        with pytest.raises(VisionError, match="cannot record usage"):
            await rig.service.analyze(rig.claims(max_tokens=BUDGET), "latest", "?")
    assert len(api.requests) == 1, "no call reached the provider"
    await until(lambda: len(ledger.unrecorded) == 1)  # every attempt timed out: fenced
    with pytest.raises(VisionError, match="cannot record usage"):
        await rig.service.analyze(rig.claims(max_tokens=BUDGET), "latest", "?")
    assert len(api.requests) == 1
    assert len(set(usage.attempts)) == 1, "every attempt used the same call id"
    assert len(usage.attempts) >= 2, "a write attempt that stops is tried again"

    # The store comes back. The same record is written once, and the budget sees the spend.
    usage.recovered.set()
    with pytest.raises(VisionError, match="not allowed now"):
        await rig.service.analyze(rig.claims(max_tokens=BUDGET), "latest", "?")
    assert ledger.unrecorded == []
    assert not ledger.blocked(org)
    (record,) = usage.calls
    assert record.call_id == usage.attempts[0]
    assert (record.counts.input, record.usage_estimated) == (1500, False)
    assert len(api.requests) == 1


async def test_a_stalled_write_blocks_only_its_own_org(engine: AsyncEngine, tenant: Tenant) -> None:
    usage = StalledUsage()
    ledger = ledger_of(usage)
    api = held_api()
    rig = build(engine, tenant, usage=usage, api=api, ledger=ledger)
    await rig.image()
    await cancelled_call(rig, api)
    await until(lambda: bool(usage.attempts))
    assert ledger.blocked(str(tenant.org))
    assert not ledger.blocked(str(uuid.uuid4()))


async def test_the_stalled_write_of_a_run_that_ends_normally_is_pending_too(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    """No cancel is needed: a caller that waits gets the answer, and the next call is refused."""
    usage = StalledUsage()
    ledger = ledger_of(usage)
    rig = build(engine, tenant, usage=usage, ledger=ledger)
    await rig.image()
    answer = asyncio.create_task(rig.service.analyze(rig.claims(max_tokens=BUDGET), "latest", "?"))
    await until(lambda: bool(usage.attempts))
    with pytest.raises(VisionError, match="cannot record usage"):
        await rig.service.analyze(rig.claims(max_tokens=BUDGET), "latest", "?")
    usage.recovered.set()
    assert (await answer).text == "A bar chart."
    assert len(rig.api.requests) == 1


async def test_a_fast_write_does_not_refuse_a_concurrent_call(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    """A budget check waits for the pending writes of its org. It does not refuse for them."""

    class Slow(MemoryUsageStore):
        async def record(self, call: CallRecord) -> None:
            await asyncio.sleep(0.02)
            await super().record(call)

    usage = Slow()
    ledger = UsageLedger(usage, retry_s=0, wait_s=2.0)
    rig = build(engine, tenant, usage=usage, ledger=ledger)
    await rig.image()
    results = await asyncio.gather(
        *(rig.service.analyze(rig.claims(), "latest", "?") for _ in range(4))
    )
    assert [r.text for r in results] == ["A bar chart."] * 4
    assert len(usage.calls) == 4


async def test_a_database_that_holds_a_table_lock_refuses_the_next_calls(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    """The real store: a SHARE lock on ``model_calls`` holds every insert, without an error."""

    class Counting(PostgresUsageStore):
        attempts = 0

        async def record(self, call: CallRecord) -> None:
            self.attempts += 1
            await super().record(call)

    usage = Counting(engine)
    ledger = ledger_of(usage)
    api = held_api()
    rig = build(engine, tenant, usage=usage, api=api, ledger=ledger)  # type: ignore[arg-type]
    await rig.image()
    run_id = uuid.uuid4()  # the store reads the authority of a real run
    async with engine.begin() as connection:
        await connection.execute(
            insert(Run).values(
                id=run_id,
                org_id=tenant.org,
                session_id=tenant.ids["s3"],
                agent_profile_id=tenant.ids["profile"],
                runtime="fake",
            )
        )
    claims = rig.claims(max_tokens=BUDGET).model_copy(update={"run_id": str(run_id)})

    async def count() -> int:
        async with engine.connect() as connection:
            result = await connection.execute(
                select(func.count()).select_from(ModelCall).where(ModelCall.org_id == tenant.org)
            )
            return result.scalar_one()

    before = await count()
    async with engine.connect() as locker:
        await locker.execute(text("LOCK TABLE model_calls IN SHARE MODE"))
        await cancelled_call(rig, api, claims)
        await until(lambda: usage.attempts > 0)  # the insert waits for the lock
        for _ in range(2):
            with pytest.raises(VisionError, match="cannot record usage"):
                await rig.service.analyze(claims, "latest", "?")
        assert len(api.requests) == 1, "only the first call reached the provider"
        await until(lambda: len(ledger.unrecorded) == 1)
        with pytest.raises(VisionError, match="cannot record usage"):
            await rig.service.analyze(claims, "latest", "?")
        assert len(api.requests) == 1, "the budget is not open while the write waits"
        await locker.rollback()
    # The lock is gone. The record is written, and the budget now sees it.
    with pytest.raises(VisionError, match="not allowed now"):
        await rig.service.analyze(claims, "latest", "?")
    assert ledger.unrecorded == []
    assert await count() == before + 1
    assert len(api.requests) == 1


async def stop_within(rig: Rig, seconds: float) -> float:
    started = time.monotonic()
    await asyncio.wait_for(rig.service.aclose(), seconds)
    return time.monotonic() - started


async def test_a_stop_does_not_hang_on_a_stalled_estimate_write(
    engine: AsyncEngine, tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(vision_module, "CLOSE_WAIT_S", 0.05)
    monkeypatch.setattr(vision_module, "CLOSE_SETTLE_S", 0.05)
    usage = StalledUsage()
    # A write limit of one hour: only the second cancel of the stop can end the write.
    ledger = UsageLedger(usage, attempts=3, retry_s=0, write_timeout_s=3600, wait_s=0.05)
    api = held_api()
    rig = build(engine, tenant, usage=usage, api=api, ledger=ledger)
    await rig.image()
    assert api.accepted is not None
    caller = asyncio.create_task(rig.service.analyze(rig.claims(), "latest", "?"))
    await asyncio.wait_for(api.accepted.wait(), 5)
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller

    elapsed = await stop_within(rig, 3)

    assert elapsed < 2
    assert rig.service._tasks == set(), "the call ended"
    assert rig.service._http.is_closed
    (kept,) = ledger.unrecorded
    assert kept.usage_estimated, "the estimate stays in the ledger for the final flush"
    assert kept.call_id == usage.attempts[0]
    assert ledger.blocked(str(tenant.org))


async def test_a_stop_does_not_hang_on_a_stalled_write_of_a_real_answer(
    engine: AsyncEngine, tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(vision_module, "CLOSE_WAIT_S", 0.05)
    monkeypatch.setattr(vision_module, "CLOSE_SETTLE_S", 0.05)
    usage = StalledUsage()
    ledger = UsageLedger(usage, attempts=3, retry_s=0, write_timeout_s=3600, wait_s=0.05)
    rig = build(engine, tenant, usage=usage, ledger=ledger)
    await rig.image()
    caller = asyncio.create_task(rig.service.analyze(rig.claims(), "latest", "?"))
    await until(lambda: bool(usage.attempts))  # the answer came; the write stalls

    elapsed = await stop_within(rig, 3)

    assert elapsed < 2
    assert rig.service._http.is_closed
    (kept,) = ledger.unrecorded
    assert not kept.usage_estimated
    assert kept.counts.input == 1500
    caller.cancel()
    await asyncio.gather(caller, return_exceptions=True)


async def test_a_stop_with_a_held_provider_and_no_store_problem_still_records(
    engine: AsyncEngine, tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(vision_module, "CLOSE_WAIT_S", 0.05)
    api = held_api()
    rig = build(engine, tenant, api=api)
    await rig.image()
    assert api.accepted is not None
    caller = asyncio.create_task(rig.service.analyze(rig.claims(), "latest", "?"))
    await asyncio.wait_for(api.accepted.wait(), 5)
    caller.cancel()
    await asyncio.gather(caller, return_exceptions=True)
    await stop_within(rig, 3)
    (call,) = rig.usage.calls
    assert call.usage_estimated
