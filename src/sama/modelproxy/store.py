"""Call records, spend totals, and org limits (docs/architecture.md, Section 16.6).

``MemoryUsageStore`` is for tests and local runs. ``PostgresUsageStore`` writes the
``model_calls`` table. It needs the ``runs`` row of the run, so it is used after the run queue
writes runs (M1.10).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import ColumnElement, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.db.models import ModelCall, Org
from sama.modelproxy.prices import Provider, TokenCounts
from sama.modelproxy.usage import Endpoint
from sama.tenancy.authority import Authority, RunSpend, load_authority, load_run_spend


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
    cost_usd: Decimal = Field(ge=0)
    duration_ms: int = Field(ge=0)
    created_at: datetime


class OrgLimits(BaseModel):
    """The model limits of an org, from ``orgs.limits``. ``None`` means no limit."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    model_usd_per_month: Decimal | None = Field(default=None, ge=0)
    model_usd_per_user_day: Decimal | None = Field(default=None, ge=0)


class UsageStore(Protocol):
    async def record(self, call: CallRecord) -> None: ...

    async def run_spend(self, org_id: str, run_id: str) -> RunSpend: ...

    async def org_cost(self, org_id: str, since: datetime) -> Decimal: ...

    async def user_cost(self, org_id: str, user_id: str, since: datetime) -> Decimal: ...

    async def org_limits(self, org_id: str) -> OrgLimits: ...

    async def authority(self, org_id: str, run_id: str) -> Authority:
        """The live authority of the run (Section 5.1)."""
        ...


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


class PostgresUsageStore:
    """Writes ``model_calls`` and reads the spend and ``orgs.limits`` as the table owner."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

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
