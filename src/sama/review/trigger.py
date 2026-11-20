"""The trigger of the background review (docs/architecture.md, Section 9.4).

``after_completed_run`` runs in the transaction that ends a chat run. It counts the user turn
and the tool calls of the run on the session row. When a counter reaches its threshold, it
queues one review run and resets the counters. The update of the session row locks the row,
so the runs of one session count in a sequence.

Rules:

- Only a completed chat run counts. A failed, a cancelled, or a review run does not count. So a
  review cannot start a review (recursion is off).
- A session of the lineage ``delegation`` is a task of a sub-agent. It is not reviewed.
- A profile with ``background_review = false`` is not reviewed, and its counters stay at 0.
- A session has at most one waiting review. A new trigger moves the end of the range of the
  waiting review. The running review is not touched, and the session has at most one.
- When the org or the user spent the model limit, sama does not queue a review. The counters
  stay, so the next completed run tries again.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, Final

from sqlalchemy import and_, func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from sama.db.models import AgentProfile, Message, ModelCall, Org, Run, RunEvent, Session
from sama.review.policy import (
    MAX_COST_USD,
    MAX_TURNS,
    MAX_WALL_TIME_S,
    TOOL_ITERATIONS,
    USER_TURNS,
)
from sama.tenancy.authority import lower, run_user

if TYPE_CHECKING:
    from sama.orchestrator.queue import ClaimedRun

QUEUED_REVIEW: Final = text("kind = 'review' AND status = 'queued'")


def review_budget(profile_budget: Any) -> dict[str, Any]:
    """The budget of a review: the lower of the profile budget and the caps of the policy."""
    source = profile_budget if isinstance(profile_budget, dict) else {}
    cost = lower(_decimal(source.get("max_cost_usd")), MAX_COST_USD)
    budget: dict[str, Any] = {
        "max_turns": lower(_positive_int(source.get("max_turns")), MAX_TURNS),
        "max_wall_time_s": lower(_positive_number(source.get("max_wall_time_s")), MAX_WALL_TIME_S),
        "max_cost_usd": str(cost),
    }
    tokens = _positive_int(source.get("max_tokens"))
    if tokens is not None:
        budget["max_tokens"] = tokens
    return budget


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _positive_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        return None
    return float(value)


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return None
    return number if number.is_finite() and number > 0 else None


async def after_completed_run(connection: AsyncConnection, run: ClaimedRun) -> uuid.UUID | None:
    """Counts the completed chat run. Returns the id of the review run that it queued, or of
    the waiting review that it extended, or ``None``."""
    if run.kind != "chat":
        return None
    profile = (
        await connection.execute(
            select(
                Session.lineage,
                AgentProfile.background_review,
                AgentProfile.budget,
            )
            .join(
                AgentProfile,
                and_(
                    AgentProfile.org_id == Session.org_id,
                    AgentProfile.id == Session.agent_profile_id,
                ),
            )
            .where(Session.org_id == run.org_id, Session.id == run.session_id)
        )
    ).one_or_none()
    if profile is None or profile.lineage == "delegation" or not profile.background_review:
        return None
    tools = (
        await connection.execute(
            select(func.count()).where(
                RunEvent.org_id == run.org_id,
                RunEvent.run_id == run.run_id,
                RunEvent.type == "tool_call_started",
            )
        )
    ).scalar_one()
    counters = (
        await connection.execute(
            update(Session)
            .where(Session.org_id == run.org_id, Session.id == run.session_id)
            .values(
                review_user_turns=Session.review_user_turns + 1,
                review_tool_iterations=Session.review_tool_iterations + int(tools),
            )
            .returning(
                Session.review_user_turns,
                Session.review_tool_iterations,
                Session.review_cursor_seq,
            )
        )
    ).one()
    if (
        counters.review_user_turns < USER_TURNS
        and counters.review_tool_iterations < TOOL_ITERATIONS
    ):
        return None
    if await budget_spent(connection, run):
        return None
    last = (
        await connection.execute(
            select(func.coalesce(func.max(Message.seq), 0)).where(
                Message.org_id == run.org_id, Message.session_id == run.session_id
            )
        )
    ).scalar_one()
    review_id = await _queue_review(
        connection, run, profile.budget, int(counters.review_cursor_seq), int(last)
    )
    await connection.execute(
        update(Session)
        .where(Session.org_id == run.org_id, Session.id == run.session_id)
        .values(review_user_turns=0, review_tool_iterations=0, review_cursor_seq=int(last))
    )
    return review_id


async def _queue_review(
    connection: AsyncConnection,
    run: ClaimedRun,
    profile_budget: Any,
    from_seq: int,
    to_seq: int,
) -> uuid.UUID | None:
    waiting = (
        await connection.execute(
            update(Run)
            .where(
                Run.org_id == run.org_id,
                Run.session_id == run.session_id,
                Run.kind == "review",
                Run.status == "queued",
            )
            .values(review_to_seq=to_seq)
            .returning(Run.id)
        )
    ).scalar_one_or_none()
    if waiting is not None:
        return waiting
    # The lock on the session row (the update of the counters) means that no other trigger of
    # this session runs now. The unique index is the second guard.
    return (
        await connection.execute(
            pg_insert(Run)
            .values(
                org_id=run.org_id,
                session_id=run.session_id,
                agent_profile_id=run.agent_profile_id,
                triggered_by_user_id=None,
                runtime=run.runtime,
                budget=review_budget(profile_budget),
                unattended=True,
                kind="review",
                review_from_seq=from_seq,
                review_to_seq=to_seq,
            )
            .on_conflict_do_nothing(index_elements=[Run.session_id], index_where=QUEUED_REVIEW)
            .returning(Run.id)
        )
    ).scalar_one_or_none()


async def budget_spent(connection: AsyncConnection, run: ClaimedRun) -> bool:
    """True when the org spent its model limit of the month, or the user of the session spent
    the limit of the day (``orgs.limits``, Section 16.6)."""
    limits = (await connection.execute(select(Org.limits).where(Org.id == run.org_id))).scalar_one()
    now = datetime.now(UTC)
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    monthly = _decimal((limits or {}).get("model_usd_per_month"))
    if monthly is not None and await _spent(connection, run, day.replace(day=1)) >= monthly:
        return True
    daily = _decimal((limits or {}).get("model_usd_per_user_day"))
    user = run_user(None, run.audience, run.owner_user_id)
    return bool(
        daily is not None and user is not None and await _spent(connection, run, day, user) >= daily
    )


async def _spent(
    connection: AsyncConnection, run: ClaimedRun, since: datetime, user: uuid.UUID | None = None
) -> Decimal:
    query = select(func.coalesce(func.sum(ModelCall.cost_usd), 0)).where(
        ModelCall.org_id == run.org_id, ModelCall.created_at >= since
    )
    if user is not None:
        query = query.where(ModelCall.user_id == user)
    return Decimal((await connection.execute(query)).scalar_one())
