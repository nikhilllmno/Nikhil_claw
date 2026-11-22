"""Tests of ``SessionWarmer`` (docs/architecture.md, Section 4.4)."""

from __future__ import annotations

import asyncio
import logging

import pytest

from sama.orchestrator.prewarm import SessionWarmer, WarmRateLimitedError, WarmStatus


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Warm:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.gate = asyncio.Event()
        self.gate.set()
        self.fail = False

    async def __call__(self, org_id: str, session_id: str) -> str:
        self.calls.append((org_id, session_id))
        await self.gate.wait()
        if self.fail:
            raise RuntimeError("the provider is down")
        return "ready"


async def test_a_request_warms_the_session_in_the_background() -> None:
    warm = Warm()
    warmer = SessionWarmer(warm)
    assert warmer.request("o", "s1", "u") is WarmStatus.WARMING
    await warmer.wait_idle()
    assert warm.calls == [("o", "s1")]


async def test_a_second_request_while_a_warm_runs_or_soon_after_is_recent() -> None:
    warm, clock = Warm(), Clock()
    warm.gate.clear()
    warmer = SessionWarmer(warm, min_interval_s=60, clock=clock)
    assert warmer.request("o", "s1", "u") is WarmStatus.WARMING
    assert warmer.request("o", "s1", "u") is WarmStatus.RECENT  # running
    warm.gate.set()
    await warmer.wait_idle()
    clock.now += 30
    assert warmer.request("o", "s1", "u") is WarmStatus.RECENT  # in the interval
    assert warmer.request("o", "s2", "u") is WarmStatus.WARMING  # another session
    clock.now += 31
    assert warmer.request("o", "s1", "u") is WarmStatus.WARMING
    await warmer.wait_idle()
    assert warm.calls == [("o", "s1"), ("o", "s2"), ("o", "s1")]


async def test_a_user_who_asks_too_often_gets_a_rate_limit() -> None:
    warm, clock = Warm(), Clock()
    warmer = SessionWarmer(warm, per_user=2, window_s=60, clock=clock)
    warmer.request("o", "s1", "mia")
    warmer.request("o", "s2", "mia")
    with pytest.raises(WarmRateLimitedError) as error:
        warmer.request("o", "s3", "mia")
    assert 1 <= error.value.retry_after_s <= 60
    assert warmer.request("o", "s3", "tom") is WarmStatus.WARMING  # another user
    clock.now += 61
    assert warmer.request("o", "s3", "mia") is WarmStatus.RECENT  # tom warmed it now
    await warmer.wait_idle()


async def test_a_failed_warm_only_logs(caplog: pytest.LogCaptureFixture) -> None:
    warm = Warm()
    warm.fail = True
    warmer = SessionWarmer(warm)
    with caplog.at_level(logging.ERROR, logger="sama.orchestrator"):
        warmer.request("o", "s1", "u")
        await warmer.wait_idle()
    assert "the session warm failed" in caplog.text


async def test_aclose_cancels_a_running_warm() -> None:
    warm = Warm()
    warm.gate.clear()
    warmer = SessionWarmer(warm)
    warmer.request("o", "s1", "u")
    await asyncio.sleep(0)
    await warmer.aclose()
    await warmer.wait_idle()
    assert warm.calls == [("o", "s1")]
