from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.observability.alerts import AlertError, ErrorRateAlerter, SlackWebhook
from tests.observability.conftest import add_run
from tests.orchestrator.conftest import Tenant


class Channel:
    def __init__(self) -> None:
        self.posts: list[str] = []
        self.down = False

    async def post(self, text: str) -> None:
        if self.down:
            raise AlertError("Slack answered HTTP 500")
        self.posts.append(text)


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 19, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


async def _runs(engine: AsyncEngine, tenant: Tenant, *, failed: int, completed: int) -> None:
    for _ in range(failed):
        await add_run(engine, tenant, status="failed", failure_code="model_error")
    for _ in range(completed):
        await add_run(engine, tenant)


async def _check(alerter: ErrorRateAlerter, engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        await alerter.check(connection)


async def test_a_breach_posts_one_alert_and_one_recovery(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    channel = Channel()
    alerter = ErrorRateAlerter(engine, channel, environment="staging", min_runs=4)
    await _runs(engine, tenant, failed=2, completed=2)
    await add_run(engine, tenant, status="cancelled", failure_code="cancelled")
    await _check(alerter, engine)
    await _check(alerter, engine)
    [alert] = channel.posts
    assert "sama staging: 2 of 4 runs failed" in alert
    assert "(50%)" in alert
    assert "model_error 2" in alert

    await _runs(engine, tenant, failed=0, completed=6)
    await _check(alerter, engine)
    assert len(channel.posts) == 2
    assert "The alert is over" in channel.posts[1]
    assert not alerter.firing


async def test_few_runs_or_a_low_rate_post_nothing(engine: AsyncEngine, tenant: Tenant) -> None:
    channel = Channel()
    alerter = ErrorRateAlerter(engine, channel, min_runs=5)
    await _runs(engine, tenant, failed=4, completed=0)
    await _check(alerter, engine)
    await _runs(engine, tenant, failed=0, completed=20)
    await _check(alerter, engine)
    assert channel.posts == []


async def test_old_runs_do_not_count(engine: AsyncEngine, tenant: Tenant) -> None:
    channel = Channel()
    alerter = ErrorRateAlerter(engine, channel, min_runs=1, window=timedelta(minutes=15))
    for _ in range(3):
        await add_run(
            engine, tenant, status="failed", failure_code="internal", ended_ago=timedelta(hours=1)
        )
    await _check(alerter, engine)
    assert channel.posts == []


async def test_the_cooldown_stops_a_flapping_rate(engine: AsyncEngine, tenant: Tenant) -> None:
    channel = Channel()
    clock = Clock()
    alerter = ErrorRateAlerter(
        engine, channel, min_runs=1, threshold=0.5, cooldown=timedelta(minutes=30), clock=clock
    )
    await _runs(engine, tenant, failed=1, completed=0)
    await _check(alerter, engine)
    await _runs(engine, tenant, failed=0, completed=2)
    await _check(alerter, engine)
    assert len(channel.posts) == 2  # the alert and the recovery

    await _runs(engine, tenant, failed=2, completed=0)
    clock.now += timedelta(minutes=10)
    await _check(alerter, engine)
    assert len(channel.posts) == 2
    clock.now += timedelta(minutes=25)
    await _check(alerter, engine)
    assert len(channel.posts) == 3
    assert alerter.firing


async def test_a_failed_post_is_tried_again(engine: AsyncEngine, tenant: Tenant) -> None:
    channel = Channel()
    alerter = ErrorRateAlerter(engine, channel, min_runs=1)
    await _runs(engine, tenant, failed=2, completed=0)
    channel.down = True
    with pytest.raises(AlertError):
        await _check(alerter, engine)
    assert not alerter.firing
    channel.down = False
    await _check(alerter, engine)
    assert len(channel.posts) == 1


async def test_only_the_leader_posts(engine: AsyncEngine, tenant: Tenant) -> None:
    channels = [Channel(), Channel()]
    alerters = [ErrorRateAlerter(engine, channel, min_runs=1) for channel in channels]
    await _runs(engine, tenant, failed=3, completed=0)
    stop = asyncio.Event()
    tasks = [asyncio.create_task(a.serve(stop, interval_s=0.05)) for a in alerters]
    for _ in range(100):
        if any(channel.posts for channel in channels):
            break
        await asyncio.sleep(0.05)
    await asyncio.sleep(0.3)
    stop.set()
    await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)
    assert sorted(len(channel.posts) for channel in channels) == [0, 1]

    # The lock is free again after the stop, so a new alerter can lead.
    late = Channel()
    stop = asyncio.Event()
    task = asyncio.create_task(
        ErrorRateAlerter(engine, late, min_runs=1).serve(stop, interval_s=0.05)
    )
    for _ in range(100):
        if late.posts:
            break
        await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, timeout=5)
    assert len(late.posts) == 1


async def test_the_slack_webhook_posts_text_and_hides_the_url() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200 if len(seen) == 1 else 404, text="no")

    url = SecretStr("https://hooks.slack.example.test/services/T/B/secret-part")
    slack = SlackWebhook(url, transport=httpx.MockTransport(handler))
    await slack.post("hello")
    assert json.loads(seen[0].content) == {"text": "hello"}
    with pytest.raises(AlertError) as caught:
        await slack.post("again")
    assert "secret-part" not in str(caught.value)
    assert "404" in str(caught.value)
    await slack.aclose()
