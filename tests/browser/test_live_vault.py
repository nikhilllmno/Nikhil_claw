"""Live checks of the browser vault and the URL policy on Browserbase (Section 14.1, M5.3).

Run only with ``SAMA_LIVE_TESTS=1``, the Browserbase key and project, and
``SAMA_LIVE_LOGIN_URL``: the public base URL of the test login site of
``tests/browser/test_cdp_fill.py`` (for example, serve ``_app()`` and expose the port). The
user name and the password are the values of that test site.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import httpx
import pytest
from pydantic import SecretStr

from sama.browser.browserbase import API_URL, BrowserbaseTarget
from sama.browser.contexts import MemoryContextStore
from sama.browser.guard import UrlGuard
from sama.browser.logins import Credentials, normal_site
from sama.browser.policy import OrgUrlPolicy, PolicyCache
from sama.browser.service import REDACTED, BrowserService
from sama.browser.target import Act, BrowserError
from tests.browser.test_cdp import ref_of
from tests.browser.test_cdp_fill import PASSWORD, USERNAME
from tests.browser.test_vault_service import Logins, Policies, login
from tests.support import make_claims

LIVE = os.environ.get("SAMA_LIVE_TESTS") == "1"
KEY = os.environ.get("BROWSERBASE_API_KEY", "")
PROJECT = os.environ.get("BROWSERBASE_PROJECT_ID", "")
SITE = os.environ.get("SAMA_LIVE_LOGIN_URL", "").rstrip("/")

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not LIVE, reason="set SAMA_LIVE_TESTS=1"),
    pytest.mark.skipif(not (KEY and PROJECT), reason="set the Browserbase key and project"),
    pytest.mark.skipif(not SITE, reason="set SAMA_LIVE_LOGIN_URL"),
]


@pytest.fixture
async def service() -> AsyncIterator[tuple[BrowserService, Policies]]:
    contexts = MemoryContextStore()
    target = BrowserbaseTarget(
        api_key=SecretStr(KEY), project_id=PROJECT, contexts=contexts, session_timeout_s=180
    )
    policies = Policies(OrgUrlPolicy())
    logins = Logins(login("Test site", SITE, personal=True))
    logins.secrets[logins.logins[0].id] = Credentials(
        username=SecretStr(USERNAME), password=SecretStr(PASSWORD)
    )
    service = BrowserService(target, UrlGuard(), policies=PolicyCache(policies), logins=logins)
    try:
        yield service, policies
    finally:
        await service.aclose()
        async with httpx.AsyncClient(
            base_url=API_URL, headers={"X-BB-API-Key": KEY}, timeout=30
        ) as client:
            for context_id in contexts.rows.values():
                await client.delete(f"/v1/contexts/{context_id}")


async def test_a_saved_login_signs_in_on_browserbase_without_the_secret(
    service: tuple[BrowserService, Policies],
) -> None:
    browser, _ = service
    claims = make_claims("run-live-vault")
    listed = await browser.vault_list(claims)
    assert normal_site(SITE) in listed
    opened = await browser.open(claims, SITE + "/login")
    assert "Sign in" in opened
    filled = await browser.vault_fill(claims, "test site", submit=True)
    text = await browser.extract(claims, "text")
    outputs = listed + opened + filled + text
    assert PASSWORD not in outputs
    assert USERNAME not in outputs
    assert "Title: Account" in filled
    assert f"Signed in as {REDACTED}" in text
    browser.end_run("org-1", "run-live-vault")


async def test_the_url_policy_of_the_org_blocks_on_browserbase(
    service: tuple[BrowserService, Policies],
) -> None:
    browser, policies = service
    claims = make_claims("run-live-policy")
    host = httpx.URL(SITE).host
    policies.value = OrgUrlPolicy(block_hosts=(host,))
    with pytest.raises(BrowserError, match="block list of the org"):
        await browser.open(claims, SITE + "/login")
    policies.value = OrgUrlPolicy(allow_hosts=("example.com",))
    browser.forget_policy("org-1")
    opened = await browser.open(claims, "https://example.com/")
    assert "Example Domain" in opened
    with pytest.raises(BrowserError, match="not in the allow list of the org"):
        await browser.open(claims, SITE + "/login")
    # A link of the page to a host out of the allow list is blocked in the browser too.
    acted = await browser.act(claims, Act(action="click", ref=ref_of(opened, "link")))
    assert "Blocked by the URL policy" in acted
    assert "not in the allow list of the org" in acted
    browser.end_run("org-1", "run-live-policy")
