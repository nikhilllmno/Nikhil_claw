"""The money checks and the usage records of the media tools (docs/architecture.md, Section
11.8; roadmap M4.9).

``image_generate`` and ``text_to_speech`` call a provider with the key of the org, as
``vision_analyze`` does. The meter does the same checks as the model proxy before each call,
and it writes one ``model_calls`` row for each call, so the call counts against the run, the
user, and the org. A provider call runs in a task that a cancel of the caller does not stop:
the provider charges for a call that the caller no longer waits for.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Awaitable, Callable, Coroutine
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Final, TypeVar

from sama.modelproxy.ledger import UsageLedger
from sama.modelproxy.prices import PriceTable, Provider, TokenCounts
from sama.modelproxy.store import CallRecord, UsageStore, day_start, month_start
from sama.modelproxy.usage import Endpoint
from sama.tenancy.authority import budget_problem, lower
from sama.tenancy.run_token import RunTokenClaims
from sama.vault.store import KeySource

log = logging.getLogger("sama.media")

CLOSE_WAIT_S: Final = 5.0
"""At the stop, the calls that run get this long to end."""
CLOSE_SETTLE_S: Final = 5.0
"""At the stop, a cancelled call gets this long to write its estimate."""

T = TypeVar("T")


class MediaError(Exception):
    """A media tool failed. The text is safe for the model: it has no secret, no URL, and no
    body of the provider."""


class MediaMeter:
    """Checks the budget before a call and writes the record after it. ``label`` names the
    feature in the errors, for example ``Image generation``."""

    def __init__(
        self,
        *,
        usage: UsageStore,
        prices: PriceTable,
        ledger: UsageLedger | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._usage = usage
        self._prices = prices
        self._ledger = ledger or UsageLedger(usage)
        self._clock = clock
        self._tasks: set[asyncio.Task[Any]] = set()

    def has_price(self, provider: Provider, model: str, *, chat: bool = False) -> bool:
        """``chat``: a model of the ``models`` table (``moa``, ``video_analyze``), not of the
        ``media`` table."""
        if chat:
            return self._prices.lookup(provider, model) is not None
        return self._prices.media(provider, model) is not None

    @property
    def prices(self) -> PriceTable:
        return self._prices

    async def remaining_usd(self, claims: RunTokenClaims) -> Decimal | None:
        """The money that the run can still spend, or ``None`` when the run has no cost
        limit. ``moa`` uses it to cap its plan (Section 10.2.2). Call ``check`` first."""
        authority = await self._usage.authority(claims.org_id, claims.run_id)
        max_cost = lower(claims.max_cost_usd, authority.max_cost_usd)
        if max_cost is None:
            return None
        spend = await self._usage.run_spend(claims.org_id, claims.run_id)
        return max(Decimal(0), Decimal(str(max_cost)) - Decimal(str(spend.cost_usd)))

    async def check(self, claims: RunTokenClaims, label: str) -> None:
        """Fails closed, like the model proxy: a run without authority or without budget, and
        a store that cannot answer, refuse the call."""
        await self._ledger.flush(claims.org_id)
        if self._ledger.blocked(claims.org_id):
            raise MediaError(f"{label} cannot record usage now. Try again later.")
        try:
            authority = await self._usage.authority(claims.org_id, claims.run_id)
            if authority.problem is not None:
                raise MediaError(f"{label} is not allowed now: {authority.problem}.")
            max_tokens = lower(claims.max_tokens, authority.max_tokens)
            max_cost = lower(claims.max_cost_usd, authority.max_cost_usd)
            if max_tokens is not None or max_cost is not None:
                spend = await self._usage.run_spend(claims.org_id, claims.run_id)
                problem = budget_problem(spend.tokens, spend.cost_usd, max_tokens, max_cost)
                if problem is not None:
                    raise MediaError(f"{label} is not allowed now: {problem}.")
            limits = await self._usage.org_limits(claims.org_id)
            now = self._clock()
            if limits.model_usd_per_month is not None:
                spent = await self._usage.org_cost(claims.org_id, month_start(now))
                if spent >= limits.model_usd_per_month:
                    raise MediaError(f"{label} is not allowed now: the org reached its limit.")
            if limits.model_usd_per_user_day is not None and claims.user_id is not None:
                spent = await self._usage.user_cost(claims.org_id, claims.user_id, day_start(now))
                if spent >= limits.model_usd_per_user_day:
                    raise MediaError(f"{label} is not allowed now: you reached your limit.")
        except MediaError:
            raise
        except Exception:
            log.exception("the budget check of a media call failed run=%s", claims.run_id)
            raise MediaError(f"{label} cannot check the budget now. Try again.") from None

    async def shielded(self, call: Coroutine[Any, Any, T]) -> T:
        """Runs ``call`` in a task that a cancel of the caller does not stop."""
        task: asyncio.Task[T] = asyncio.create_task(call)
        self._tasks.add(task)
        task.add_done_callback(self._done)
        return await asyncio.shield(task)

    def _done(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if not task.cancelled():
            task.exception()  # read it, so that a cancelled caller leaves no warning

    async def settle(
        self,
        claims: RunTokenClaims,
        *,
        call_id: uuid.UUID,
        started: float,
        provider: Provider,
        model: str,
        endpoint: Endpoint,
        counts: TokenCounts,
        estimated: bool,
        status: int,
        key_source: KeySource,
        chat: bool = False,
    ) -> None:
        if chat:
            cost = self._prices.cost(provider, model, counts)
        else:
            cost = self._prices.media_cost(provider, model, counts)
        log.info(
            "media call run=%s model=%s tokens=%d cost_usd=%s estimated=%s",
            claims.run_id,
            model,
            counts.total_tokens,
            cost,
            estimated,
        )
        await self._ledger.settle(
            CallRecord(
                call_id=call_id,
                org_id=claims.org_id,
                workspace_id=claims.workspace_id,
                session_id=claims.session_id,
                run_id=claims.run_id,
                user_id=claims.user_id,
                provider=provider,
                model=model,
                endpoint=endpoint,
                status_code=status,
                counts=counts,
                usage_estimated=estimated,
                key_source=key_source,
                cost_usd=cost,
                duration_ms=int((time.monotonic() - started) * 1000),
                created_at=self._clock(),
            )
        )

    async def aclose(self, close: Callable[[], Awaitable[None]] | None = None) -> None:
        """Stops within a fixed time, as ``VisionService.aclose`` does. Then runs ``close``."""
        pending = set(self._tasks)
        if pending:
            _, pending = await asyncio.wait(pending, timeout=CLOSE_WAIT_S)
        for task in pending:
            task.cancel()  # the call writes an estimate when it is cancelled
        if pending:
            _, pending = await asyncio.wait(pending, timeout=CLOSE_SETTLE_S)
        for task in pending:
            task.cancel()
        if pending:
            _, pending = await asyncio.wait(pending, timeout=CLOSE_SETTLE_S)
        if pending:
            log.error("%d media calls did not end at the stop", len(pending))
        if close is not None:
            await close()
