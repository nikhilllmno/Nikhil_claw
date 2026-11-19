"""Alerts to a Slack channel on the run error rate (docs/architecture.md, Section 18.3).

The alerter counts the runs that ended in the last ``window`` in all orgs. When at least
``min_runs`` ended and the share of failed runs is ``threshold`` or more, it posts one alert.
It posts again only after the rate goes below the threshold (a recovery message) and the
``cooldown`` has passed since the last alert. So a flapping rate does not flood the channel.

Many processes can run the alerter. Only the holder of a Postgres advisory lock checks the
rate, so one breach gives one message.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final, Protocol

import httpx
from pydantic import SecretStr
from sqlalchemy import case, func, select, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from sama.db.models import Run

log: Final = logging.getLogger("sama.observability.alerts")

LOCK_KEY: Final = 0x53414D41_414C5254
"""The advisory lock of the alerter leader ("SAMAALRT"). It fits in a signed bigint."""


class Notifier(Protocol):
    async def post(self, text: str) -> None: ...


class SlackWebhook:
    """Posts text to a Slack incoming webhook. The URL is a secret: it never goes in a log
    or an error."""

    def __init__(
        self, url: SecretStr, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._url = url
        self._http = httpx.AsyncClient(timeout=10.0, transport=transport)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def post(self, text: str) -> None:
        try:
            response = await self._http.post(self._url.get_secret_value(), json={"text": text})
        except httpx.HTTPError as error:
            raise AlertError(f"Slack is not reachable: {type(error).__name__}") from None
        if not response.is_success:
            raise AlertError(f"Slack answered HTTP {response.status_code}")


class AlertError(Exception):
    """The alert did not reach the channel."""


@dataclass(frozen=True, slots=True)
class ErrorRate:
    ended: int
    failed: int
    top_codes: tuple[tuple[str, int], ...]

    @property
    def rate(self) -> float:
        return self.failed / self.ended if self.ended else 0.0


async def error_rate(connection: AsyncConnection, window: timedelta) -> ErrorRate:
    """The failed share of the runs that completed or failed in the last ``window``.
    A cancelled run is a choice of a user, so it does not count."""
    recent = (Run.finished_at > func.now() - window, Run.status.in_(("completed", "failed")))
    ended, failed = (
        await connection.execute(
            select(func.count(), func.count(case((Run.status == "failed", 1)))).where(*recent)
        )
    ).one()
    codes = (
        await connection.execute(
            select(func.coalesce(Run.failure_code, "unknown"), func.count())
            .where(*recent, Run.status == "failed")
            .group_by(Run.failure_code)
            .order_by(func.count().desc())
            .limit(3)
        )
    ).all()
    return ErrorRate(int(ended), int(failed), tuple((str(c), int(n)) for c, n in codes))


class ErrorRateAlerter:
    def __init__(
        self,
        engine: AsyncEngine,
        notifier: Notifier,
        *,
        environment: str | None = None,
        window: timedelta = timedelta(minutes=15),
        threshold: float = 0.25,
        min_runs: int = 5,
        cooldown: timedelta = timedelta(minutes=30),
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._engine = engine
        self._notifier = notifier
        self._environment = environment or "unknown"
        self._window = window
        self._threshold = threshold
        self._min_runs = min_runs
        self._cooldown = cooldown
        self._clock = clock or (lambda: datetime.now().astimezone())
        self._firing = False
        self._last_alert: datetime | None = None

    @property
    def firing(self) -> bool:
        return self._firing

    async def check(self, connection: AsyncConnection) -> ErrorRate:
        """Reads the rate and posts an alert or a recovery when the state changes. Call it
        only while this process holds the leader lock."""
        rate = await error_rate(connection, self._window)
        breach = rate.ended >= self._min_runs and rate.rate >= self._threshold
        now = self._clock()
        if breach and not self._firing:
            if self._last_alert is not None and now - self._last_alert < self._cooldown:
                return rate
            await self._notifier.post(self._alert_text(rate))
            self._firing = True
            self._last_alert = now
            log.warning(
                "error rate alert sent",
                extra={"failed": rate.failed, "ended": rate.ended},
            )
        elif not breach and self._firing:
            await self._notifier.post(self._recovery_text(rate))
            self._firing = False
            log.info("error rate recovery sent")
        return rate

    def _alert_text(self, rate: ErrorRate) -> str:
        codes = ", ".join(f"{code} {count}" for code, count in rate.top_codes) or "none"
        minutes = int(self._window.total_seconds() // 60)
        return (
            f":rotating_light: sama {self._environment}: {rate.failed} of {rate.ended} runs "
            f"failed in the last {minutes} min ({rate.rate:.0%}). Top failure codes: {codes}."
        )

    def _recovery_text(self, rate: ErrorRate) -> str:
        return (
            f":white_check_mark: sama {self._environment}: the run error rate is "
            f"{rate.rate:.0%} ({rate.failed} of {rate.ended}). The alert is over."
        )

    async def serve(self, stop: asyncio.Event, *, interval_s: float = 60.0) -> None:
        """Checks the rate each ``interval_s`` while this process is the leader. It holds
        one connection for the advisory lock. It tries to get the lock again when the
        connection breaks."""
        while not stop.is_set():
            try:
                async with self._engine.connect() as connection:
                    await self._lead(connection, stop, interval_s)
            except Exception:
                log.exception("the error rate check failed")
            await _sleep(stop, interval_s)

    async def _lead(
        self, connection: AsyncConnection, stop: asyncio.Event, interval_s: float
    ) -> None:
        held = False
        try:
            while not stop.is_set():
                if not held:
                    held = bool(
                        (
                            await connection.execute(
                                text("SELECT pg_try_advisory_lock(:key)"), {"key": LOCK_KEY}
                            )
                        ).scalar_one()
                    )
                    await connection.commit()
                if held:
                    try:
                        await self.check(connection)
                    except AlertError as error:
                        log.warning("the alert did not reach Slack: %s", error)
                    await connection.commit()
                await _sleep(stop, interval_s)
        finally:
            if held:
                await _unlock(connection)


async def _unlock(connection: AsyncConnection) -> None:
    """Gives the leader lock back. A rollback does not free a session lock, and a pool keeps
    the database session. So when the unlock fails, the connection is closed for good."""
    try:
        # A failed statement leaves the transaction aborted. The unlock needs a new one.
        await connection.rollback()
        await connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": LOCK_KEY})
        await connection.commit()
    except Exception:
        log.warning("the leader lock is not given back, so the connection is closed")
        with contextlib.suppress(Exception):
            await connection.invalidate()


async def _sleep(stop: asyncio.Event, seconds: float) -> None:
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=seconds)
