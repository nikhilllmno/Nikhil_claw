"""Fixtures for the trace export and alert tests.

The exporter and the alerter read the runs of all orgs, so these tests use their own
database. The ``tenant`` fixture deletes its org after each test, with all of its runs.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import insert, text
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.db.models import Message, ModelCall, Run, RunEvent, ToolCall
from tests.db.conftest import migrated_database
from tests.orchestrator.conftest import Tenant, engine, tenant  # noqa: F401


@pytest.fixture(scope="session")
def queue_database_url() -> Iterator[str]:
    with migrated_database() as url:
        yield url


async def add_run(
    db: AsyncEngine,
    org: Tenant,
    *,
    status: str = "completed",
    ended_ago: timedelta = timedelta(seconds=1),
    failure_code: str | None = None,
    **values: Any,
) -> uuid.UUID:
    """Adds a run of the session ``s1`` that ended ``ended_ago`` before now."""
    run_id = uuid.uuid4()
    ended = status in ("completed", "failed", "cancelled")
    row: dict[str, Any] = {
        "id": run_id,
        "org_id": org.org,
        "session_id": org.ids["s1"],
        "agent_profile_id": org.ids["profile"],
        "triggered_by_user_id": org.ids["mia"],
        "status": status,
        "runtime": "fake",
        "attempts": 1,
        "failure_code": failure_code,
        "failure_message": "it broke" if failure_code else None,
        "started_at": text(f"now() - interval '{ended_ago.total_seconds() + 5} seconds'"),
        "finished_at": text(f"now() - interval '{ended_ago.total_seconds()} seconds'")
        if ended
        else None,
    }
    if status == "running":
        row |= {"lease_owner": "w", "lease_expires_at": text("now() + interval '1 minute'")}
    if status == "paused":
        row["pending_approval_id"] = "a1"
    async with db.begin() as connection:
        await connection.execute(insert(Run).values(**(row | values)))
    return run_id


async def add_details(db: AsyncEngine, org: Tenant, run_id: uuid.UUID, at: datetime) -> uuid.UUID:
    """Adds two messages, a model call, a tool call, and three events to the run. Returns the
    model call id."""
    keys = {"org_id": org.org, "session_id": org.ids["s1"], "run_id": run_id}
    call_id = uuid.uuid4()
    async with db.begin() as connection:
        await connection.execute(
            insert(Message).values(role="user", content={"text": "Hi there"}, **keys)
        )
        await connection.execute(
            insert(Message).values(role="assistant", content={"text": "Hello!"}, **keys)
        )
        await connection.execute(
            insert(ModelCall).values(
                call_id=call_id,
                workspace_id=org.ids["ws"],
                provider="anthropic",
                model="claude-x",
                endpoint="messages",
                status_code=200,
                input_tokens=10,
                output_tokens=5,
                cache_read_tokens=2,
                cache_write_tokens=1,
                cost_usd=Decimal("0.0125"),
                duration_ms=1500,
                created_at=at,
                **keys,
            )
        )
        await connection.execute(
            insert(ToolCall).values(
                call_id="t1",
                tool_name="web_search",
                arguments={"query": "secret plans"},
                decision="allow",
                status="finished",
                result_preview="the result",
                created_at=at,
                finished_at=at + timedelta(seconds=1),
                **keys,
            )
        )
        for seq, (kind, payload) in enumerate(
            [
                ("text_delta", {"text": "Hello!"}),
                ("compacted", {"tokens_before": 100, "tokens_after": 40}),
                ("tool_call_finished", {"call_id": "t1", "output_preview": "the result"}),
            ],
            start=1,
        ):
            await connection.execute(
                insert(RunEvent).values(seq=seq, type=kind, payload=payload, created_at=at, **keys)
            )
    return call_id
