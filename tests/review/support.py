"""Builders for the tests of the background review."""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.db.models import (
    AgentProfile,
    Membership,
    ModelCall,
    Org,
    Run,
    RunEvent,
    Session,
    User,
)
from sama.orchestrator.queue import ClaimedRun, RunQueue
from sama.runtime.contract import FailureCode, Usage
from tests.orchestrator.conftest import Tenant

OWNER = "w-review"


async def complete_chat(
    engine: AsyncEngine,
    tenant: Tenant,
    session: str = "s1",
    *,
    tools: int = 0,
    text: str = "hello",
    output: str = "done",
    queue: RunQueue | None = None,
    runtime_state: bytes | None = None,
) -> ClaimedRun:
    """Sends one message, claims the run, and completes it with ``tools`` tool calls."""
    queue = queue or RunQueue(engine)
    # No rate or quota check at enqueue: the tests of a spent quota check the review trigger.
    async with engine.begin() as connection:
        await queue.enqueue_in(
            connection,
            org_id=tenant.org,
            session_id=tenant.ids[session],
            text=text,
            user_id=tenant.ids["mia"],
            check_rate=False,
        )
    run = await queue.claim(OWNER)
    assert run is not None
    assert run.kind == "chat"
    await add_tool_calls(engine, run, tools)
    assert await queue.complete(
        run, OWNER, output=output, runtime_state=runtime_state, usage=Usage()
    )
    return run


async def fail_chat(engine: AsyncEngine, tenant: Tenant, session: str = "s1") -> None:
    queue = RunQueue(engine)
    await queue.enqueue(
        org_id=tenant.org, session_id=tenant.ids[session], text="x", user_id=tenant.ids["mia"]
    )
    run = await queue.claim(OWNER)
    assert run is not None
    await add_tool_calls(engine, run, 3)
    assert await queue.fail(run, OWNER, code=FailureCode.MODEL_ERROR, message="boom", usage=Usage())


async def add_tool_calls(engine: AsyncEngine, run: ClaimedRun, count: int) -> None:
    async with engine.begin() as connection:
        for seq in range(count):
            await connection.execute(
                insert(RunEvent).values(
                    org_id=run.org_id,
                    session_id=run.session_id,
                    run_id=run.run_id,
                    seq=run.next_seq + seq,
                    type="tool_call_started",
                    payload={"name": "Bash" if seq % 2 else "Read"},
                )
            )


async def reviews(engine: AsyncEngine, tenant: Tenant, session: str = "s1") -> list[Any]:
    """The review runs of a session, oldest first."""
    async with engine.connect() as connection:
        rows = await connection.execute(
            select(Run)
            .where(Run.org_id == tenant.org, Run.session_id == tenant.ids[session])
            .where(Run.kind == "review")
            .order_by(Run.created_at, Run.id)
        )
        return list(rows)


async def counters(
    engine: AsyncEngine, tenant: Tenant, session: str = "s1"
) -> tuple[int, int, int]:
    """``(user turns, tool iterations, cursor)`` of the session."""
    async with engine.connect() as connection:
        row = (
            await connection.execute(
                select(
                    Session.review_user_turns,
                    Session.review_tool_iterations,
                    Session.review_cursor_seq,
                ).where(Session.id == tenant.ids[session])
            )
        ).one()
    return row.review_user_turns, row.review_tool_iterations, row.review_cursor_seq


async def set_counters(
    engine: AsyncEngine, tenant: Tenant, session: str = "s1", *, turns: int = 0, tools: int = 0
) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            update(Session)
            .where(Session.id == tenant.ids[session])
            .values(review_user_turns=turns, review_tool_iterations=tools)
        )


async def add_member(
    engine: AsyncEngine,
    tenant: Tenant,
    label: str,
    *,
    org_role: str = "member",
    workspace_role: str | None = "member",
) -> uuid.UUID:
    """A user of the org. Returns the user id. The Supabase Auth id is ``<label>_auth``.
    ``workspace_role`` ``None``: no workspace membership."""
    user_id, auth_id = uuid.uuid4(), uuid.uuid4()
    async with engine.begin() as connection:
        await connection.execute(
            insert(User).values(
                id=user_id,
                org_id=tenant.org,
                auth_user_id=auth_id,
                email=f"{label}@example.test",
                display_name=label,
            )
        )
        await connection.execute(
            insert(Membership).values(org_id=tenant.org, user_id=user_id, role=org_role)
        )
        if workspace_role is not None:
            await connection.execute(
                insert(Membership).values(
                    org_id=tenant.org,
                    user_id=user_id,
                    workspace_id=tenant.ids["ws"],
                    role=workspace_role,
                )
            )
    tenant.ids[label] = user_id
    tenant.ids[f"{label}_auth"] = auth_id
    return user_id


async def set_profile(engine: AsyncEngine, tenant: Tenant, **values: Any) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            update(AgentProfile).where(AgentProfile.id == tenant.ids["profile"]).values(**values)
        )


async def set_limits(engine: AsyncEngine, tenant: Tenant, limits: dict[str, Any]) -> None:
    async with engine.begin() as connection:
        await connection.execute(update(Org).where(Org.id == tenant.org).values(limits=limits))


async def spend(
    engine: AsyncEngine,
    tenant: Tenant,
    run: ClaimedRun,
    cost: str,
    *,
    user: uuid.UUID | None = None,
) -> None:
    """A model call of ``run`` that cost ``cost`` USD."""
    async with engine.begin() as connection:
        await connection.execute(
            insert(ModelCall).values(
                call_id=uuid.uuid4(),
                org_id=tenant.org,
                workspace_id=tenant.ids["ws"],
                session_id=run.session_id,
                run_id=run.run_id,
                user_id=user,
                provider="anthropic",
                model="m",
                endpoint="messages",
                status_code=200,
                cost_usd=Decimal(cost),
                duration_ms=1,
            )
        )
