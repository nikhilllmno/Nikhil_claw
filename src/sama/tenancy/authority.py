"""Live authority of a run (docs/architecture.md, Sections 5.1 and 16.1).

Authority is not frozen when a run starts. The tool gate and the model proxy read it again at
each tool call, each model call, and each approval resume:

- The run must not be ended. A cancel ends the run, so a cancel revokes all authority.
- The user of the run must still see the session (``load_grants`` and ``sees_session``).
- The run budget is the lower of the run budget and the live budget of the agent profile. So a
  lower budget applies at once, and a higher budget does not change a run that started.
- A run of a worker bot (Section 11.6) also stays below the rest of the daily budget of the bot:
  the budget less the cost of the other runs of the bot since 00:00 UTC. A bot that is disabled
  or removed, or that spent its budget, stops its runs.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Final

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from sama.db.models import ENDED_RUN_STATUSES, AgentProfile, ModelCall, Run, Session
from sama.tenancy.access import load_grants
from sama.workerbots import bot_limit

UNKNOWN_RUN: Final = "the run is unknown"
ENDED_RUN: Final = "the run ended"
LOST_ACCESS: Final = "the user of the run can no longer see the session"

RUN_TOKENS: Final = (
    ModelCall.input_tokens
    + ModelCall.output_tokens
    + ModelCall.cache_read_tokens
    + ModelCall.cache_write_tokens
    + ModelCall.cache_write_long_tokens
)
"""The tokens of one model call that count against ``max_tokens``."""


@dataclass(frozen=True, slots=True)
class Authority:
    """The live authority of one run. ``problem`` is ``None`` while the run has authority."""

    problem: str | None = None
    max_tokens: int | None = None
    max_cost_usd: Decimal | None = None


@dataclass(frozen=True, slots=True)
class RunSpend:
    tokens: int = 0
    cost_usd: Decimal = Decimal(0)


def lower(first: Any, second: Any) -> Any:
    """The lower of two limits. ``None`` means no limit."""
    if first is None:
        return second
    if second is None:
        return first
    return min(first, second)


def run_user(
    triggered_by: uuid.UUID | None, audience: str, owner: uuid.UUID | None
) -> uuid.UUID | None:
    """The user of a run: the user who started it, else the owner of a private session."""
    if triggered_by is None and audience == "private":
        return owner
    return triggered_by


def _tokens(budget: Any) -> int | None:
    value = budget.get("max_tokens") if isinstance(budget, dict) else None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _cost(budget: Any) -> Decimal | None:
    value = budget.get("max_cost_usd") if isinstance(budget, dict) else None
    if value is None or isinstance(value, bool):
        return None
    try:
        cost = Decimal(str(value))
    except InvalidOperation:
        return None
    return cost if cost.is_finite() and cost > 0 else None


async def load_authority(
    connection: AsyncConnection, org_id: uuid.UUID, run_id: uuid.UUID
) -> Authority:
    """Reads the live authority of a run. The caller scopes the read to one org."""
    row = (
        await connection.execute(
            select(
                Run.status,
                Run.triggered_by_user_id,
                Run.budget,
                AgentProfile.budget.label("profile_budget"),
                Session.workspace_id,
                Session.audience,
                Session.owner_user_id,
                Session.worker_bot_id,
            )
            .join(Session, and_(Session.org_id == Run.org_id, Session.id == Run.session_id))
            .join(
                AgentProfile,
                and_(AgentProfile.org_id == Run.org_id, AgentProfile.id == Run.agent_profile_id),
            )
            .where(Run.org_id == org_id, Run.id == run_id)
        )
    ).one_or_none()
    if row is None:
        return Authority(problem=UNKNOWN_RUN)
    if row.status in ENDED_RUN_STATUSES:
        return Authority(problem=ENDED_RUN)
    user_id = run_user(row.triggered_by_user_id, row.audience, row.owner_user_id)
    if user_id is not None:
        grants = await load_grants(connection, org_id, user_id)
        if not grants.sees_session(row.workspace_id, row.audience, row.owner_user_id):
            return Authority(problem=LOST_ACCESS)
    max_cost_usd = lower(_cost(row.budget), _cost(row.profile_budget))
    if row.worker_bot_id is not None:
        limit = await bot_limit(connection, org_id, row.worker_bot_id, run_id)
        if limit.problem is not None:
            return Authority(problem=limit.problem)
        max_cost_usd = lower(max_cost_usd, limit.max_cost_usd)
    return Authority(
        max_tokens=lower(_tokens(row.budget), _tokens(row.profile_budget)),
        max_cost_usd=max_cost_usd,
    )


async def load_run_spend(
    connection: AsyncConnection, org_id: uuid.UUID, run_id: uuid.UUID
) -> RunSpend:
    """The tokens and the cost of the recorded model calls of a run."""
    tokens, cost = (
        await connection.execute(
            select(
                func.coalesce(func.sum(RUN_TOKENS), 0),
                func.coalesce(func.sum(ModelCall.cost_usd), 0),
            ).where(ModelCall.org_id == org_id, ModelCall.run_id == run_id)
        )
    ).one()
    return RunSpend(tokens=int(tokens), cost_usd=Decimal(cost))


def budget_problem(
    tokens: int, cost_usd: Decimal, max_tokens: int | None, max_cost_usd: Decimal | None
) -> str | None:
    """Why the run cannot spend more, or ``None``."""
    if max_tokens is not None and tokens >= max_tokens:
        return "the run reached max_tokens"
    if max_cost_usd is not None and cost_usd >= max_cost_usd:
        return "the run reached max_cost_usd"
    return None


__all__ = [
    "ENDED_RUN",
    "LOST_ACCESS",
    "UNKNOWN_RUN",
    "Authority",
    "RunSpend",
    "budget_problem",
    "load_authority",
    "load_run_spend",
    "lower",
    "run_user",
]
