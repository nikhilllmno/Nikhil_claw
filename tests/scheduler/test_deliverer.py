"""The delivery rows with Postgres: receipts, retries, and the unknown result (M4.3)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from pydantic import SecretStr
from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.db.models import Delivery, JobFire, SessionBinding
from sama.scheduler.delivery import MAX_ATTEMPTS, Deliverer, Sender, SendResult
from sama.scheduler.jobs import JobService
from sama.scheduler.spec import JobInput
from sama.vault.store import SecretName
from tests.scheduler.conftest import Tenant


class FakeSender(Sender):
    """Answers each call with the next result and keeps the calls."""

    def __init__(self, *results: SendResult) -> None:
        async def no_secret(org_id: uuid.UUID, name: SecretName) -> SecretStr | None:
            return None

        super().__init__(no_secret)
        self.results = list(results)
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def _next(self, kind: str, *args: Any) -> SendResult:
        self.calls.append((kind, args))
        return self.results.pop(0)

    async def telegram(
        self, org_id: uuid.UUID, chat_id: str, text: str, thread_id: int | None = None
    ) -> SendResult:
        return self._next("telegram", chat_id, text)

    async def slack(
        self, org_id: uuid.UUID, channel: str, text: str, thread_ts: str | None = None
    ) -> SendResult:
        return self._next("slack", channel, thread_ts)

    async def email(self, address: str, subject: str, text: str) -> SendResult:
        return self._next("email", address, subject)


@pytest.fixture
async def fire(engine: AsyncEngine, jobs: JobService, tenant: Tenant) -> AsyncIterator[Any]:
    view = await jobs.create(
        tenant.user(), JobInput(name="news", schedule="every hour", prompt="hi")
    )
    fire_id = await jobs.run_now(tenant.user(), view.id)
    async with engine.begin() as connection:
        row = (await connection.execute(select(JobFire).where(JobFire.id == fire_id))).one()
    yield row
    await jobs.cancel(tenant.user(), view.id)


async def _add(engine: AsyncEngine, fire: Any, target: str, kind: str = "result") -> uuid.UUID:
    delivery_id = uuid.uuid4()
    async with engine.begin() as connection:
        await connection.execute(
            insert(Delivery).values(
                id=delivery_id,
                org_id=fire.org_id,
                job_id=fire.job_id,
                fire_id=fire.id,
                kind=kind,
                target=target,
                text="news\n\nAll quiet.",
            )
        )
    return delivery_id


async def _row(engine: AsyncEngine, delivery_id: uuid.UUID) -> Any:
    async with engine.connect() as connection:
        return (await connection.execute(select(Delivery).where(Delivery.id == delivery_id))).one()


async def _due_now(engine: AsyncEngine, delivery_id: uuid.UUID) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            update(Delivery).where(Delivery.id == delivery_id).values(next_attempt_at=func.now())
        )


async def test_a_sent_delivery_keeps_its_receipt(engine: AsyncEngine, fire: Any) -> None:
    delivery_id = await _add(engine, fire, "telegram:-1001")
    sender = FakeSender(SendResult("sent", receipt="telegram:-1001:77"))
    assert await Deliverer(engine, sender).run_once() == 1
    row = await _row(engine, delivery_id)
    assert (row.status, row.provider_message_id, row.attempts) == (
        "sent",
        "telegram:-1001:77",
        1,
    )
    assert row.sent_at is not None
    assert sender.calls == [("telegram", ("-1001", "news\n\nAll quiet."))]


async def test_an_unknown_result_is_not_sent_again(engine: AsyncEngine, fire: Any) -> None:
    delivery_id = await _add(engine, fire, "slack:C1")
    sender = FakeSender(SendResult("unknown", error="no answer: ReadTimeout"))
    deliverer = Deliverer(engine, sender)
    await deliverer.run_once()
    await _due_now(engine, delivery_id)
    assert await deliverer.run_once() == 0
    row = await _row(engine, delivery_id)
    assert (row.status, row.attempts, row.error) == ("unknown", 1, "no answer: ReadTimeout")
    assert len(sender.calls) == 1


async def test_a_failure_tries_again_then_stops(engine: AsyncEngine, fire: Any) -> None:
    delivery_id = await _add(engine, fire, "email:ana@example.test")
    sender = FakeSender(*[SendResult("failed", error="refused")] * MAX_ATTEMPTS)
    deliverer = Deliverer(engine, sender)
    await deliverer.run_once()
    row = await _row(engine, delivery_id)
    assert (row.status, row.attempts) == ("pending", 1)
    assert row.next_attempt_at is not None
    assert await deliverer.run_once() == 0, "the backoff holds the row"
    for _ in range(MAX_ATTEMPTS - 1):
        await _due_now(engine, delivery_id)
        await deliverer.run_once()
    row = await _row(engine, delivery_id)
    assert (row.status, row.attempts) == ("failed", MAX_ATTEMPTS)
    assert sender.calls[0] == ("email", ("ana@example.test", "sama: news"))


async def test_a_lost_lease_becomes_unknown(engine: AsyncEngine, fire: Any) -> None:
    delivery_id = await _add(engine, fire, "slack:C1")
    deliverer = Deliverer(engine, FakeSender())
    claimed = await deliverer.claim()
    assert [item.id for item in claimed] == [delivery_id]
    async with engine.begin() as connection:
        await connection.execute(
            update(Delivery)
            .where(Delivery.id == delivery_id)
            .values(lease_expires_at=Delivery.created_at)
        )
    assert await deliverer.expire() == 1
    row = await _row(engine, delivery_id)
    assert (row.status, row.lease_expires_at) == ("unknown", None)


async def test_origin_goes_to_the_chat_of_the_job(
    engine: AsyncEngine, fire: Any, tenant: Tenant
) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            insert(SessionBinding).values(
                org_id=tenant.org,
                workspace_id=tenant.ids["ws"],
                session_id=tenant.ids["chat"],
                channel="slack",
                external_key="iid:thread:C9:171.5",
            )
        )
    await _add(engine, fire, "origin")
    sender = FakeSender(SendResult("sent", receipt="slack:C9:1"))
    await Deliverer(engine, sender).run_once()
    assert sender.calls == [("slack", ("C9", "171.5"))]


async def test_origin_with_no_chat_stays_in_the_session(engine: AsyncEngine, fire: Any) -> None:
    delivery_id = await _add(engine, fire, "origin")
    await Deliverer(engine, FakeSender()).run_once()
    row = await _row(engine, delivery_id)
    assert (row.status, row.provider_message_id) == ("sent", f"session:{fire.session_id}")


async def test_none_needs_no_provider(engine: AsyncEngine, fire: Any) -> None:
    delivery_id = await _add(engine, fire, "none")
    await Deliverer(engine, FakeSender()).run_once()
    assert (await _row(engine, delivery_id)).status == "sent"


async def test_a_send_after_a_lost_lease_keeps_its_receipt(engine: AsyncEngine, fire: Any) -> None:
    delivery_id = await _add(engine, fire, "slack:C1")
    deliverer = Deliverer(engine, FakeSender(SendResult("sent", receipt="slack:C1:1.5")))
    [item] = await deliverer.claim()
    async with engine.begin() as connection:
        await connection.execute(
            update(Delivery)
            .where(Delivery.id == delivery_id)
            .values(status="unknown", lease_expires_at=None)
        )
    await deliverer._record(item, await deliverer.send(item))
    row = await _row(engine, delivery_id)
    assert (row.status, row.provider_message_id) == ("sent", "slack:C1:1.5")


async def test_deliveries_are_claimed_one_at_a_time(engine: AsyncEngine, fire: Any) -> None:
    first = await _add(engine, fire, "slack:C1", kind="result")
    second = await _add(engine, fire, "slack:C2", kind="failure")
    states: list[str] = []

    class Watching(FakeSender):
        async def slack(
            self, org_id: uuid.UUID, channel: str, text: str, thread_ts: str | None = None
        ) -> SendResult:
            other = second if channel == "C1" else first
            states.append((await _row(engine, other)).status)
            return await super().slack(org_id, channel, text, thread_ts)

    sender = Watching(SendResult("sent", receipt="a"), SendResult("sent", receipt="b"))
    assert await Deliverer(engine, sender).run_once() == 2
    assert states == ["pending", "sent"]
