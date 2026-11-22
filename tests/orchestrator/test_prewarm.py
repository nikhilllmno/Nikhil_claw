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


async def test_the_org_and_process_limits_keep_capacity_for_runs() -> None:
    started: list[tuple[str, str]] = []
    gate = asyncio.Event()

    async def warm(org_id: str, session_id: str) -> str:
        started.append((org_id, session_id))
        await gate.wait()
        return "ready"

    now = [0.0]
    warmer = SessionWarmer(
        warm, per_org=2, total=3, max_running=10, per_user=100, clock=lambda: now[0]
    )
    assert warmer.request("o1", "s1", "u1") is WarmStatus.WARMING
    assert warmer.request("o1", "s2", "u2") is WarmStatus.WARMING
    assert warmer.request("o1", "s3", "u3") is WarmStatus.LIMITED, "per org"
    assert warmer.request("o2", "s4", "u4") is WarmStatus.WARMING
    assert warmer.request("o3", "s5", "u5") is WarmStatus.LIMITED, "per process"
    now[0] = 61.0
    assert warmer.request("o1", "s3", "u3") is WarmStatus.WARMING, "a new window"
    gate.set()
    await warmer.wait_idle()
    assert ("o1", "s3") in started


async def test_at_most_max_running_warms_run_at_once() -> None:
    gate = asyncio.Event()

    async def warm(org_id: str, session_id: str) -> str:
        await gate.wait()
        return "ready"

    warmer = SessionWarmer(warm, max_running=1, per_org=100, total=100, per_user=100)
    assert warmer.request("o1", "s1", "u1") is WarmStatus.WARMING
    assert warmer.request("o2", "s2", "u2") is WarmStatus.LIMITED
    gate.set()
    await warmer.wait_idle()
    assert warmer.request("o2", "s2", "u2") is WarmStatus.WARMING
    await warmer.wait_idle()
