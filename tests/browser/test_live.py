"""Live checks of the cloud browser on Browserbase. Run only with ``SAMA_LIVE_TESTS=1``.

They need ``BROWSERBASE_API_KEY`` and ``BROWSERBASE_PROJECT_ID``. Each test deletes the
Browserbase context that it made.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator

import httpx
import pytest
from pydantic import SecretStr

from sama.browser.browserbase import API_URL, BrowserbaseTarget
from sama.browser.contexts import MemoryContextStore
from sama.browser.guard import UrlGuard
from sama.browser.target import CLOUD, ExtractRequest

LIVE = os.environ.get("SAMA_LIVE_TESTS") == "1"
KEY = os.environ.get("BROWSERBASE_API_KEY", "")
PROJECT = os.environ.get("BROWSERBASE_PROJECT_ID", "")

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not LIVE, reason="set SAMA_LIVE_TESTS=1"),
    pytest.mark.skipif(not (KEY and PROJECT), reason="set the Browserbase key and project"),
]

COOKIE_SITE = "https://httpbin.org"


@pytest.fixture
async def target() -> AsyncIterator[tuple[BrowserbaseTarget, MemoryContextStore]]:
    contexts = MemoryContextStore()
    target = BrowserbaseTarget(
        api_key=SecretStr(KEY), project_id=PROJECT, contexts=contexts, session_timeout_s=120
    )
    try:
        yield target, contexts
    finally:
        await target.aclose()
        async with httpx.AsyncClient(
            base_url=API_URL, headers={"X-BB-API-Key": KEY}, timeout=30
        ) as client:
            for context_id in contexts.rows.values():
                await client.delete(f"/v1/contexts/{context_id}")


async def test_a_real_session_opens_a_page_and_takes_a_snapshot(
    target: tuple[BrowserbaseTarget, MemoryContextStore],
) -> None:
    browserbase, contexts = target
    session = await browserbase.start(
        org_id="org-live", user_id="user-live", run_id="run-live-1", guard=UrlGuard()
    )
    try:
        page = await session.navigate("https://example.com/")
        snapshot = await session.snapshot()
        shot = await session.screenshot()
    finally:
        await session.close()
    assert page.url == "https://example.com/"
    assert snapshot.page.title == "Example Domain"
    assert "documentation examples" in snapshot.tree
    assert "[ref=" in snapshot.tree
    assert shot.data[:3] == b"\xff\xd8\xff" or shot.data[:4] == b"\x89PNG"
    assert ("org-live", "user-live", CLOUD) in contexts.rows


async def test_the_persistent_context_keeps_cookies_between_runs(
    target: tuple[BrowserbaseTarget, MemoryContextStore],
) -> None:
    browserbase, contexts = target
    first = await browserbase.start(
        org_id="org-live", user_id="user-live", run_id="run-live-2", guard=UrlGuard()
    )
    try:
        await first.navigate(f"{COOKIE_SITE}/cookies/set?sama_live=kept")
    finally:
        await first.close()
    (context_id,) = contexts.rows.values()

    # Browserbase writes the context after the session ends; give it time.
    seen = ""
    for _ in range(6):
        await asyncio.sleep(5)
        second = await browserbase.start(
            org_id="org-live", user_id="user-live", run_id="run-live-3", guard=UrlGuard()
        )
        try:
            await second.navigate(f"{COOKIE_SITE}/cookies")
            seen = (await second.extract(ExtractRequest(kind="text"))).content
        finally:
            await second.close()
        if "sama_live" in seen:
            break
    assert "kept" in seen, seen
    assert tuple(contexts.rows.values()) == (context_id,)
