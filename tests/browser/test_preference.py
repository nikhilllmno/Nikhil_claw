"""The browser choice of a session decides the target of a call without one (M8.3)."""

from __future__ import annotations

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.browser.preference import SessionBrowserChoice
from sama.browser.service import BrowserService
from sama.browser.target import CLOUD, MY_CHROME, BrowserError, TargetName
from sama.db.models import Session
from sama.tenancy.run_token import RunTokenClaims
from tests.browser.fakes import AllowAll, FakeTarget
from tests.orchestrator.conftest import Tenant, engine, queue_database_url, tenant
from tests.support import make_claims

__all__ = ["engine", "queue_database_url", "tenant"]


class ChromeFake(FakeTarget):
    @property
    def name(self) -> TargetName:
        return MY_CHROME


async def test_a_call_without_a_target_uses_the_choice_of_the_session() -> None:
    chosen: list[TargetName | None] = [MY_CHROME]

    async def preference(claims: RunTokenClaims) -> TargetName | None:
        return chosen[0]

    cloud, chrome = FakeTarget(), ChromeFake()
    browser = BrowserService([cloud, chrome], AllowAll(), preference=preference)
    claims = make_claims("run-1")
    await browser.open(claims, "https://shop.example/")
    assert (len(cloud.sessions), len(chrome.sessions)) == (0, 1)
    # The run keeps its target for the next calls, also when the choice changes.
    chosen[0] = CLOUD
    await browser.snapshot(claims)
    assert len(cloud.sessions) == 0
    # A named target wins over the choice.
    await browser.open(make_claims("run-2"), "https://a.example/", target=CLOUD)
    assert len(cloud.sessions) == 1
    await browser.aclose()


async def test_a_failed_or_unknown_choice_uses_the_default() -> None:
    async def broken(claims: RunTokenClaims) -> TargetName | None:
        raise RuntimeError("database down")

    cloud = FakeTarget()
    browser = BrowserService(cloud, AllowAll(), preference=broken)
    await browser.open(make_claims("run-1"), "https://shop.example/")
    assert len(cloud.sessions) == 1
    await browser.aclose()


async def test_the_store_reads_the_column_of_the_session(
    engine: AsyncEngine,
    tenant: Tenant,
) -> None:
    now = [0.0]
    choice = SessionBrowserChoice(engine, clock=lambda: now[0])
    claims = RunTokenClaims(
        org_id=str(tenant.org),
        workspace_id=str(tenant.ids["ws"]),
        session_id=str(tenant.ids["mine"]),
        run_id="run-1",
        user_id=str(tenant.ids["mia"]),
        token_id="t",
        expires_at=2_000_000_000,
    )
    assert await choice(claims) is None
    async with engine.begin() as connection:
        await connection.execute(
            update(Session)
            .where(Session.id == tenant.ids["mine"])
            .values(browser_target="my_chrome")
        )
    assert await choice(claims) is None  # kept for a short time
    now[0] = 11.0
    assert await choice(claims) == MY_CHROME
    choice.forget(claims.org_id, claims.session_id)
    assert await choice(claims) == MY_CHROME


async def test_a_cloud_choice_never_falls_back_to_the_chrome_of_the_user() -> None:
    """Review: without the cloud target, a cloud choice gives the error of the target, not the
    default target (``my_chrome``)."""

    async def cloud(claims: RunTokenClaims) -> TargetName | None:
        return CLOUD

    chrome = ChromeFake()
    browser = BrowserService(chrome, AllowAll(), preference=cloud)
    with pytest.raises(BrowserError, match="not available here"):
        await browser.open(make_claims("run-1"), "https://shop.example/")
    assert chrome.sessions == []
    await browser.aclose()
