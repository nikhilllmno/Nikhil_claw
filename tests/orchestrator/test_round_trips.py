"""The database round trips of one chat run (docs/architecture.md, Section 4.3).

Each statement is one round trip to the database, and each transaction adds a ``BEGIN`` and a
``COMMIT``. These tests keep the count of the hot path from growing by accident.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.db import autocommit
from sama.orchestrator.queue import Lease, RunQueue
from sama.prompt import PromptAssembler
from sama.runtime.contract import Usage
from tests.orchestrator.conftest import Tenant

OWNER = "worker-a"


@contextmanager
def counted(engine: AsyncEngine) -> Iterator[list[str]]:
    """Collects the SQL text of each statement that ``engine`` sends."""
    statements: list[str] = []

    def record(*args: Any) -> None:
        statements.append(str(args[2]))

    event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        yield statements
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record)


async def test_a_chat_run_keeps_its_round_trip_budget(engine: AsyncEngine, tenant: Tenant) -> None:
    queue = RunQueue(engine)
    config = PromptAssembler(engine)
    for turn in range(2):
        with counted(engine) as enqueue:
            await queue.enqueue(
                org_id=tenant.org,
                session_id=tenant.ids["s1"],
                text=f"hi {turn}",
                user_id=tenant.ids["mia"],
            )
        # The rate lock, one read of the session, the profile, and the rate counts, and one
        # insert of the message and the run.
        assert len(enqueue) == 3, enqueue
        with counted(engine) as claim:
            run = await queue.claim(OWNER)
        assert run is not None
        # The full orgs, the candidate, the running lock, the limit, the claim, and the pins.
        assert len(claim) == 6, claim
        with counted(engine) as read_config:
            await config(run)
        if turn:
            # The session has its frozen config: one read and no lock.
            assert len(read_config) == 1, read_config
        with counted(engine) as dispatch:
            assert await queue.dispatch(run, OWNER, config_fingerprint="f")
            assert await queue.add_events(run, OWNER, [(run.next_seq, "text_delta", {"t": "a"})])
            assert await queue.heartbeat(run, OWNER) is Lease.HELD
        assert len(dispatch) == 3, dispatch
        assert await queue.complete(run, OWNER, output="ok", runtime_state=None, usage=Usage())


async def test_autocommit_runs_each_statement_in_its_own_transaction(engine: AsyncEngine) -> None:
    async with autocommit(engine) as connection:
        first = await connection.scalar(text("SELECT now()"))
        await asyncio.sleep(0.05)
        # In a transaction, now() stays the time of its start.
        second = await connection.scalar(text("SELECT now()"))
    assert first < second
