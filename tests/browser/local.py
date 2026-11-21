"""A browser target on a local headless Chromium, for tests (Section 14.1).

It gives a ``PlaywrightSession`` like the ``cloud`` target, but on a local browser, so a test
can run the browser tools end to end without Browserbase. It starts Playwright in the event
loop of the first ``start``: the loop of the tool server.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sama.browser.cdp import PlaywrightSession
from sama.browser.target import CLOUD, NavigationGuard, TargetName

if TYPE_CHECKING:
    from playwright.async_api import Playwright


class LocalChromiumTarget:
    """Each session is a new local Chromium. ``start`` raises when Chromium is not installed."""

    def __init__(self) -> None:
        self._playwright: Playwright | None = None
        self.sessions: list[PlaywrightSession] = []

    @property
    def name(self) -> TargetName:
        return CLOUD

    async def start(
        self,
        *,
        org_id: str,
        user_id: str | None,
        run_id: str,
        guard: NavigationGuard,
        session_id: str = "",
    ) -> PlaywrightSession:
        from playwright.async_api import async_playwright

        if self._playwright is None:
            self._playwright = await async_playwright().start()
        browser = await self._playwright.chromium.launch()
        session = await PlaywrightSession.attach(browser, guard, target=CLOUD)
        self.sessions.append(session)
        return session

    async def aclose(self) -> None:
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None
