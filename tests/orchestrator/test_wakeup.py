"""The queue notifications that wake idle workers (docs/architecture.md, Section 4.3)."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import asyncpg
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.orchestrator.wakeup import CHANNEL, QueueWakeup
from tests.orchestrator.conftest import Tenant
from tests.orchestrator.test_worker import Harness, until


async def test_a_ticket_wakes_on_notify_and_not_before() -> None:
    wakeup = QueueWakeup(None)
    stop = asyncio.Event()
    ticket = wakeup.ticket()
    assert not await ticket.wait(stop, 0.01)  # the poll time ends the wait
    waiter = asyncio.create_task(ticket.wait(stop, 10.0))
    await asyncio.sleep(0)
    wakeup.notify()
    assert await asyncio.wait_for(waiter, 1.0)
    # A ticket taken after the notify waits for the next one.
    assert not await wakeup.ticket().wait(stop, 0.01)


async def test_a_notify_before_the_wait_is_not_lost() -> None:
    wakeup = QueueWakeup(None)
    ticket = wakeup.ticket()
    wakeup.notify()  # for example, a run queued while the slot read the queue
    assert ticket.woken
    assert await ticket.wait(asyncio.Event(), 10.0)


async def test_stop_ends_the_wait() -> None:
    wakeup = QueueWakeup(None)
    stop = asyncio.Event()
    waiter = asyncio.create_task(wakeup.ticket().wait(stop, 10.0))
    await asyncio.sleep(0)
    stop.set()
    assert not await asyncio.wait_for(waiter, 1.0)
    await wakeup.run(stop)  # without an engine it does nothing


async def _notifications(url: str, action: Callable[[], Awaitable[object]]) -> list[str]:
    """Runs ``action`` and returns the notifications on the channel."""
    got: list[str] = []
    listener = await asyncpg.connect(url)
    try:
        await listener.add_listener(CHANNEL, lambda *args: got.append(args[3]))
        await action()
        await asyncio.sleep(0.2)
    finally:
        await listener.close()
    return got


async def test_the_trigger_notifies_a_queued_run_and_an_ended_run(
    engine: AsyncEngine, tenant: Tenant, queue_database_url: str
) -> None:
    harness = Harness(engine)
    run_ids: list[uuid.UUID] = []

    async def send() -> None:
        run_ids.append(await harness.send(tenant, "s1", "hi"))

    async def set_status(values: str) -> None:
        async with engine.begin() as connection:
            await connection.execute(
                text(f"UPDATE runs SET {values} WHERE id = :id"),  # noqa: S608 -- fixed text
                {"id": run_ids[0]},
            )

    assert await _notifications(queue_database_url, send) == [""]
    assert await _notifications(queue_database_url, lambda: set_status("status = 'queued'")) == []
    running = "status = 'running', lease_owner = 'w', lease_expires_at = now() + interval '1m'"
    assert await _notifications(queue_database_url, lambda: set_status(running)) == []
    ended = "status = 'cancelled', lease_owner = NULL, lease_expires_at = NULL"
    assert await _notifications(queue_database_url, lambda: set_status(ended)) == [""]


async def test_an_idle_worker_claims_a_new_run_before_its_poll(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    # A poll of one minute: only the notification can start the run in time.
    harness = Harness(engine, poll_s=60.0, maintenance_s=60.0)
    wakeup: Any = harness.worker._wakeup
    stop = asyncio.Event()
    server = asyncio.create_task(harness.worker.serve(stop, concurrency=1))
    try:
        await asyncio.wait_for(wakeup.listening.wait(), 10.0)
        await asyncio.sleep(0.2)  # the slot is idle now
        started = time.monotonic()
        run_id = await harness.send(tenant, "s1", "hi")
        await until(lambda: harness.status_is(run_id, "completed"), timeout_s=10.0)
        assert time.monotonic() - started < 5.0
    finally:
        stop.set()
        async with asyncio.timeout(10):
            await server


async def test_the_listener_connects_again_after_an_error(
    engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    wakeup = QueueWakeup(engine, retry_s=0.05, check_s=0.05)
    stop = asyncio.Event()
    task = asyncio.create_task(wakeup.run(stop))
    try:
        await asyncio.wait_for(wakeup.listening.wait(), 10.0)
        # End the listen connection from the server side.
        async with engine.connect() as connection:
            await connection.exec_driver_sql(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity"
                " WHERE (query LIKE '%LISTEN%' || $1 || '%' OR query = 'SELECT 1')"
                " AND pid <> pg_backend_pid()",
                (CHANNEL,),
            )
        await until(_cleared(wakeup), timeout_s=10.0)
        await asyncio.wait_for(wakeup.listening.wait(), 10.0)
        assert "the run queue listener stopped" in caplog.text
    finally:
        stop.set()
        await asyncio.wait_for(task, 10.0)


def _cleared(wakeup: QueueWakeup) -> Any:
    seen = {"cleared": False}

    async def check() -> bool:
        if not wakeup.listening.is_set():
            seen["cleared"] = True
        return seen["cleared"]

    return check
