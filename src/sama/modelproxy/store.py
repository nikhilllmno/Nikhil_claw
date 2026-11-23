"""Call records, spend totals, and org limits (docs/architecture.md, Section 16.6).

``MemoryUsageStore`` is for tests and local runs. ``PostgresUsageStore`` writes the
``model_calls`` table. It needs the ``runs`` row of the run, so it is used after the run queue
writes runs (M1.10).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import ColumnElement, case, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.db.models import ModelCall, Org
from sama.modelproxy.prices import Provider, TokenCounts
from sama.modelproxy.usage import Endpoint
from sama.tenancy.authority import (
    RUN_TOKENS,
    Authority,
    RunSpend,
    load_authority,
    load_run_spend,
)
from sama.vault.store import KeySource


class CallRecord(BaseModel):
    """One forwarded model call."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    call_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    """One id per provider call. A second write of the same record does nothing."""
    org_id: str
    workspace_id: str
    session_id: str
    run_id: str
    user_id: str | None
    provider: Provider
    model: str
    endpoint: Endpoint
    status_code: int
    counts: TokenCounts
    usage_estimated: bool = False
    """True when the final usage was not known and the proxy recorded a high estimate."""
    key_source: KeySource
    """The key of the call: the key of the org (M6.7) or the key of sama. The cost is the same
    for both, so the budgets and the limits count both."""
    cost_usd: Decimal = Field(ge=0)
    duration_ms: int = Field(ge=0)
    created_at: datetime


class OrgLimits(BaseModel):
    """The model limits of an org, from ``orgs.limits``. ``None`` means no limit."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    model_usd_per_month: Decimal | None = Field(default=None, ge=0)
    model_usd_per_user_day: Decimal | None = Field(default=None, ge=0)


@dataclass(frozen=True, slots=True)
class LiveState:
    """What the proxy reads before it forwards a call, from one store call (``UsageStore.live``).

    ``spend`` is the spend of the run. ``month_cost`` is the cost of the org since the start of
    the month, and it is read only when the org has a monthly quota. ``day_cost`` is the cost of
    the user since the start of the day, and it is read only when the call has a user and the org
    has a daily quota. A field that was not read is ``None``. A store reads the fields of the
    spend and the costs only when ``recorded`` is true (the route spends the budget).
    """

    authority: Authority
    limits: OrgLimits = field(default_factory=OrgLimits)
    spend: RunSpend | None = None
    month_cost: Decimal | None = None
    day_cost: Decimal | None = None


class UsageStore(Protocol):
    async def record(self, call: CallRecord) -> None: ...

    async def run_spend(self, org_id: str, run_id: str) -> RunSpend: ...

    async def org_cost(self, org_id: str, since: datetime) -> Decimal: ...

    async def user_cost(self, org_id: str, user_id: str, since: datetime) -> Decimal: ...

    async def org_limits(self, org_id: str) -> OrgLimits: ...

    async def authority(self, org_id: str, run_id: str) -> Authority:
        """The live authority of the run (Section 5.1)."""
        ...

    async def live(
        self,
        org_id: str,
        run_id: str,
        *,
        user_id: str | None,
        month_since: datetime,
        day_since: datetime,
        recorded: bool,
    ) -> LiveState:
        """Everything that the proxy reads before a call, in one store call. The checks and
        their order stay in the proxy: the store only reads."""
        ...


async def live_from_parts(
    store: UsageStore,
    org_id: str,
    run_id: str,
    *,
    user_id: str | None,
    month_since: datetime,
    day_since: datetime,
    recorded: bool,
) -> LiveState:
    """``UsageStore.live`` made of the single reads, one after the other. A store that has no
    faster way (a store in memory) uses it, and so does a test store that overrides a single
    read. It reads only what the checks need, as the proxy did before ``live``."""
    authority = await store.authority(org_id, run_id)
    if not recorded or authority.problem is not None:
        return LiveState(authority)
    spend = await store.run_spend(org_id, run_id)
    limits = await store.org_limits(org_id)
    month_cost = None
    if limits.model_usd_per_month is not None:
        month_cost = await store.org_cost(org_id, month_since)
    day_cost = None
    if limits.model_usd_per_user_day is not None and user_id is not None:
        day_cost = await store.user_cost(org_id, user_id, day_since)
    return LiveState(authority, limits, spend, month_cost, day_cost)


class MemoryUsageStore:
    """Keeps the records in memory. ``limits`` maps an org id to its limits. ``authorities``
    maps a run id to its live authority. A run that is not in the map has full authority."""

    def __init__(self, limits: dict[str, OrgLimits] | None = None) -> None:
        self.calls: list[CallRecord] = []
        self.limits = limits if limits is not None else {}
        self.authorities: dict[str, Authority] = {}

    async def record(self, call: CallRecord) -> None:
        if all(c.call_id != call.call_id for c in self.calls):
            self.calls.append(call)

    async def run_spend(self, org_id: str, run_id: str) -> RunSpend:
        calls = [c for c in self.calls if c.org_id == org_id and c.run_id == run_id]
        return RunSpend(
            tokens=sum(c.counts.total_tokens for c in calls),
            cost_usd=sum((c.cost_usd for c in calls), Decimal(0)),
        )

    async def org_cost(self, org_id: str, since: datetime) -> Decimal:
        calls = (c for c in self.calls if c.org_id == org_id and c.created_at >= since)
        return sum((c.cost_usd for c in calls), Decimal(0))

    async def user_cost(self, org_id: str, user_id: str, since: datetime) -> Decimal:
        calls = (
            c
            for c in self.calls
            if c.org_id == org_id and c.user_id == user_id and c.created_at >= since
        )
        return sum((c.cost_usd for c in calls), Decimal(0))

    async def org_limits(self, org_id: str) -> OrgLimits:
        return self.limits.get(org_id, OrgLimits())

    async def authority(self, org_id: str, run_id: str) -> Authority:
        return self.authorities.get(run_id, Authority())

    async def live(
        self,
        org_id: str,
        run_id: str,
        *,
        user_id: str | None,
        month_since: datetime,
        day_since: datetime,
        recorded: bool,
    ) -> LiveState:
        return await live_from_parts(
            self,
            org_id,
            run_id,
            user_id=user_id,
            month_since=month_since,
            day_since=day_since,
            recorded=recorded,
        )


class PostgresUsageStore:
    """Writes ``model_calls`` and reads the spend and ``orgs.limits`` as the table owner."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        # The reads of ``live`` change nothing and need no snapshot (each statement reads on its
        # own, as in a transaction at the default level). Without a transaction, a read costs
        # no ``BEGIN`` and no ``ROLLBACK``: two round trips less.
        self._reads = engine.execution_options(isolation_level="AUTOCOMMIT")

    async def record(self, call: CallRecord) -> None:
        values: dict[str, Any] = {
            "call_id": call.call_id,
            "org_id": uuid.UUID(call.org_id),
            "workspace_id": uuid.UUID(call.workspace_id),
            "session_id": uuid.UUID(call.session_id),
            "run_id": uuid.UUID(call.run_id),
            "user_id": uuid.UUID(call.user_id) if call.user_id else None,
            "provider": call.provider.value,
            "model": call.model,
            "endpoint": call.endpoint.value,
            "status_code": call.status_code,
            "input_tokens": call.counts.input,
            "output_tokens": call.counts.output,
            "cache_read_tokens": call.counts.cache_read,
            "cache_write_tokens": call.counts.cache_write,
            "cache_write_long_tokens": call.counts.cache_write_long,
            "web_search_requests": call.counts.web_search_requests,
            "usage_estimated": call.usage_estimated,
            "key_source": call.key_source.value,
            "cost_usd": call.cost_usd,
            "duration_ms": call.duration_ms,
            "created_at": call.created_at,
        }
        async with self._engine.begin() as connection:
            await connection.execute(
                insert(ModelCall)
                .values(**values)
                .on_conflict_do_nothing(index_elements=[ModelCall.call_id])
            )

    async def run_spend(self, org_id: str, run_id: str) -> RunSpend:
        async with self._engine.connect() as connection:
            return await load_run_spend(connection, uuid.UUID(org_id), uuid.UUID(run_id))

    async def org_cost(self, org_id: str, since: datetime) -> Decimal:
        return await self._cost(
            ModelCall.org_id == uuid.UUID(org_id), ModelCall.created_at >= since
        )

    async def user_cost(self, org_id: str, user_id: str, since: datetime) -> Decimal:
        return await self._cost(
            ModelCall.org_id == uuid.UUID(org_id),
            ModelCall.user_id == uuid.UUID(user_id),
            ModelCall.created_at >= since,
        )

    async def org_limits(self, org_id: str) -> OrgLimits:
        query = select(Org.limits).where(Org.id == uuid.UUID(org_id))
        async with self._engine.connect() as connection:
            limits = (await connection.execute(query)).scalar_one_or_none()
        return OrgLimits.model_validate(limits or {})

    async def authority(self, org_id: str, run_id: str) -> Authority:
        async with self._engine.connect() as connection:
            return await load_authority(connection, uuid.UUID(org_id), uuid.UUID(run_id))

    async def live(
        self,
        org_id: str,
        run_id: str,
        *,
        user_id: str | None,
        month_since: datetime,
        day_since: datetime,
        recorded: bool,
    ) -> LiveState:
        """The authority of the run (its statements) and, for a recorded route, one statement
        for the limits of the org, the spend of the run, and the costs of the org and the user.
        All of it runs on one connection. Every part reads with the ``org_id`` of the token.
        The cost of the org is read only if the limits have a monthly quota, and the cost of the
        user only if they have a daily quota, as before (a ``CASE`` on the limits)."""
        org, run = uuid.UUID(org_id), uuid.UUID(run_id)
        async with self._reads.connect() as connection:
            authority = await load_authority(connection, org, run)
            if not recorded or authority.problem is not None:
                return LiveState(authority)
            limits = select(Org.limits).where(Org.id == org).cte("live_limits")
            monthly = limits.c.limits.op("->>")("model_usd_per_month").is_not(None)
            daily = limits.c.limits.op("->>")("model_usd_per_user_day").is_not(None)
            org_cost = (
                select(func.coalesce(func.sum(ModelCall.cost_usd), 0))
                .where(ModelCall.org_id == org, ModelCall.created_at >= month_since)
                .scalar_subquery()
            )
            statement = select(
                select(limits.c.limits).scalar_subquery().label("limits"),
                select(func.coalesce(func.sum(RUN_TOKENS), 0))
                .where(ModelCall.org_id == org, ModelCall.run_id == run)
                .scalar_subquery()
                .label("run_tokens"),
                select(func.coalesce(func.sum(ModelCall.cost_usd), 0))
                .where(ModelCall.org_id == org, ModelCall.run_id == run)
                .scalar_subquery()
                .label("run_cost"),
                case((select(monthly).scalar_subquery(), org_cost)).label("month_cost"),
                *(
                    ()
                    if user_id is None
                    else (
                        case(
                            (
                                select(daily).scalar_subquery(),
                                select(func.coalesce(func.sum(ModelCall.cost_usd), 0))
                                .where(
                                    ModelCall.org_id == org,
                                    ModelCall.user_id == uuid.UUID(user_id),
                                    ModelCall.created_at >= day_since,
                                )
                                .scalar_subquery(),
                            )
                        ).label("day_cost"),
                    )
                ),
            )
            row = (await connection.execute(statement)).one()
        return LiveState(
            authority,
            OrgLimits.model_validate(row.limits or {}),
            RunSpend(tokens=int(row.run_tokens), cost_usd=Decimal(row.run_cost)),
            None if row.month_cost is None else Decimal(row.month_cost),
            None if user_id is None or row.day_cost is None else Decimal(row.day_cost),
        )

    async def _cost(self, *where: ColumnElement[bool]) -> Decimal:
        query = select(func.coalesce(func.sum(ModelCall.cost_usd), 0)).where(*where)
        async with self._engine.connect() as connection:
            return Decimal((await connection.execute(query)).scalar_one())


def month_start(now: datetime) -> datetime:
    """The start of the calendar month of ``now`` in UTC."""
    return day_start(now).replace(day=1)


def day_start(now: datetime) -> datetime:
    """The start of the calendar day of ``now`` in UTC."""
    return now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
