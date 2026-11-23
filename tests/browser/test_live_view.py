"""The live view and the control of the cloud browser (Section 14.1, M8.3)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest

from sama.browser.service import BOT_CHECK_NOTE, USER_HAS_CONTROL, BrowserService, render_state
from sama.browser.target import BrowserError, LiveView, PageState
from tests.browser.fakes import AllowAll, FakeSession, FakeTarget
from tests.support import make_claims


@dataclass
class LiveSession(FakeSession):
    ttls: list[int] | None = None

    async def live_view(self, ttl_s: int) -> LiveView | None:
        if self.ttls is None:
            self.ttls = []
        self.ttls.append(ttl_s)
        return LiveView(
            view_url="https://www.browserbase.com/fs/x?navbar=false",
            page_url=self.url,
            title=self.title,
            tabs=1,
            page_id="p1",
        )


@pytest.fixture
async def service() -> AsyncIterator[tuple[BrowserService, FakeTarget]]:
    target = FakeTarget(session_class=LiveSession)
    service = BrowserService(target, AllowAll(), idle_s=60, control_wait_s=0.2)
    yield service, target
    await service.aclose()


async def test_the_live_state_of_a_run(service: tuple[BrowserService, FakeTarget]) -> None:
    browser, _target = service
    claims = make_claims("run-1")
    assert await browser.live_state(claims.org_id, "run-1") is None
    await browser.open(claims, "https://shop.example/")
    state = await browser.live_state(claims.org_id, "run-1")
    assert state is not None
    assert state.view is not None
    assert (state.target, state.user_control) == ("cloud", False)
    assert state.view.page_url == "https://shop.example/"
    assert state.view.page_id == "p1"
    assert await browser.live_state(claims.org_id, "run-2") is None
    assert await browser.live_state("other-org", "run-1") is None
    browser.end_run(claims.org_id, "run-1")
    assert await browser.live_state(claims.org_id, "run-1") is None


async def test_the_agent_waits_while_the_user_has_control(
    service: tuple[BrowserService, FakeTarget],
) -> None:
    browser, target = service
    claims = make_claims("run-1")
    await browser.open(claims, "https://shop.example/")
    assert browser.set_control(claims.org_id, "run-1", user=True)
    state = await browser.live_state(claims.org_id, "run-1")
    assert state is not None
    assert state.user_control
    waiting = asyncio.create_task(browser.snapshot(claims))
    await asyncio.sleep(0.05)
    assert not waiting.done()
    assert browser.set_control(claims.org_id, "run-1", user=False)
    assert "[ref=e1]" in await waiting
    # One snapshot of browser_open, one after the give back.
    assert target.sessions[0].calls.count("snapshot") == 2


async def test_a_call_fails_when_the_user_keeps_control(
    service: tuple[BrowserService, FakeTarget],
) -> None:
    browser, _target = service
    claims = make_claims("run-1")
    await browser.open(claims, "https://shop.example/")
    browser.set_control(claims.org_id, "run-1", user=True)
    with pytest.raises(BrowserError, match="the user has control"):
        await browser.snapshot(claims)
    assert USER_HAS_CONTROL.startswith("the user has control")
    # The idle reaper keeps a session that a person controls.
    browser._live[(claims.org_id, "run-1", "cloud")].used = -1_000.0
    assert browser.reap() == 0


async def test_control_needs_an_open_session(service: tuple[BrowserService, FakeTarget]) -> None:
    browser, _target = service
    assert not browser.set_control("org-1", "run-1", user=True)


def test_a_bot_check_page_gets_a_fixed_note() -> None:
    blocked = PageState(url="https://www.hyatt.com/", title="Access Denied", tab=0)
    assert BOT_CHECK_NOTE in render_state(blocked)
    fine = PageState(url="https://www.hyatt.com/", title="Hyatt Place San Jose", tab=0)
    assert BOT_CHECK_NOTE not in render_state(fine)
