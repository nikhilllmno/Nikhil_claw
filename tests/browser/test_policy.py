"""The URL policy of each org and the login sites (Sections 14.1 and 16.4, M5.3)."""

from __future__ import annotations

import asyncio
import ipaddress
from collections.abc import Sequence

import pytest

from sama.browser.guard import OrgGuard, UrlGuard
from sama.browser.logins import LoginError, normal_site, site_allows
from sama.browser.policy import (
    MAX_HOSTS,
    OrgUrlPolicy,
    PolicyCache,
    PolicyError,
    normal_host,
    normal_hosts,
)
from sama.tools.outbound import BlockedUrlError, IPAddress, OutboundClient, UrlPolicy
from tests.browser.fakes import AllowAll


async def public_resolver(host: str, port: int) -> Sequence[IPAddress]:
    return [ipaddress.ip_address("93.184.216.34")]


class Source:
    def __init__(self, policy: OrgUrlPolicy) -> None:
        self.policy_value = policy
        self.reads = 0
        self.fail = False

    async def policy(self, org_id: str) -> OrgUrlPolicy:
        self.reads += 1
        if self.fail:
            raise RuntimeError("the database is down")
        return self.policy_value


def base_guard() -> UrlGuard:
    return UrlGuard(OutboundClient(UrlPolicy(allow_http=True), resolver=public_resolver))


@pytest.mark.parametrize(
    ("value", "host"),
    [
        ("Example.COM", "example.com"),
        ("*.example.com", "example.com"),
        (".example.com", "example.com"),
        ("https://docs.example.com/a/b?c", "docs.example.com"),
        ("bücher.de", "xn--bcher-kva.de"),
        ("example.com.", "example.com"),
        ("10.0.0.1", "10.0.0.1"),
    ],
)
def test_a_host_entry_gets_its_normal_form(value: str, host: str) -> None:
    assert normal_host(value) == host


@pytest.mark.parametrize(
    "value", ["exa mple.com", "example.com/path", "a@b.com", "*", "  ", "a%20b.com"]
)
def test_a_host_entry_that_is_not_valid_is_refused(value: str) -> None:
    with pytest.raises(PolicyError):
        normal_host(value)


def test_a_list_is_sorted_without_duplicates_and_has_a_limit() -> None:
    assert normal_hosts(["b.com", "A.com", "a.com", "", "  "]) == ("a.com", "b.com")
    with pytest.raises(PolicyError, match="at most"):
        normal_hosts(f"h{n}.com" for n in range(MAX_HOSTS + 1))


def test_the_policy_checks_the_lists() -> None:
    blocked = OrgUrlPolicy(block_hosts=("evil.test",))
    assert blocked.host_problem("example.com") is None
    assert blocked.host_problem("evil.test") == "evil.test is in the block list of the org"
    assert blocked.host_problem("a.evil.test") is not None
    assert blocked.host_problem("notevil.test") is None
    allowed = OrgUrlPolicy(allow_hosts=("example.com",), block_hosts=("admin.example.com",))
    assert allowed.host_problem("docs.example.com") is None
    assert "not in the allow list" in (allowed.host_problem("other.com") or "")
    assert allowed.host_problem("admin.example.com") is not None
    # The block list alone applies to the requests of a page that are not a navigation.
    assert allowed.block_problem("cdn.other.com") is None
    assert allowed.block_problem("x.admin.example.com") is not None


async def test_the_org_guard_applies_the_lists_and_then_the_address_rules() -> None:
    source = Source(OrgUrlPolicy(block_hosts=("evil.test",)))
    guard = OrgGuard(base_guard(), "org-1", source)
    await guard.check("https://example.com/")
    with pytest.raises(BlockedUrlError, match="block list of the org"):
        await guard.check("https://www.evil.test/login")
    with pytest.raises(BlockedUrlError, match="not a public host"):
        await guard.check("http://localhost:8080/")
    with pytest.raises(BlockedUrlError):
        await guard.check("http://10.0.0.1/")
    # A literal private address is refused also when the allow list names it.
    source.policy_value = OrgUrlPolicy(allow_hosts=("10.0.0.1",))
    with pytest.raises(BlockedUrlError, match="not a public address"):
        await guard.check("http://10.0.0.1/")
    source.policy_value = OrgUrlPolicy(allow_hosts=("example.com",))
    with pytest.raises(BlockedUrlError, match="allow list"):
        await guard.check("https://other.com/")
    await guard.check("https://docs.example.com/")
    await guard.check("about:blank")


async def test_the_quick_check_uses_the_block_list_after_a_load() -> None:
    source = Source(OrgUrlPolicy(allow_hosts=("example.com",), block_hosts=("ads.test",)))
    guard = OrgGuard(AllowAll(), "org-1", source)
    # Nothing is loaded yet: the quick check fails closed.
    assert guard.quick_problem("https://cdn.test/a.js") == "the URL policy of the org is not loaded"
    await guard.fresh()
    assert guard.quick_problem("https://cdn.other.com/app.js") is None
    assert (
        guard.quick_problem("https://x.ads.test/pixel.gif")
        == "x.ads.test is in the block list of the org"
    )
    assert guard.quick_problem("data:text/plain,hi") is None


async def test_the_org_guard_fails_closed_when_the_policy_cannot_be_read() -> None:
    source = Source(OrgUrlPolicy())
    source.fail = True
    guard = OrgGuard(AllowAll(), "org-1", source)
    with pytest.raises(BlockedUrlError, match="cannot be read"):
        await guard.check("https://example.com/")


async def test_the_cache_keeps_a_policy_for_a_time_and_forgets_on_change() -> None:
    now = [0.0]
    source = Source(OrgUrlPolicy(block_hosts=("a.test",)))
    cache = PolicyCache(source, cache_s=30, clock=lambda: now[0])
    assert (await cache.policy("org-1")).block_hosts == ("a.test",)
    source.policy_value = OrgUrlPolicy(block_hosts=("b.test",))
    assert (await cache.policy("org-1")).block_hosts == ("a.test",)
    now[0] = 31
    assert (await cache.policy("org-1")).block_hosts == ("b.test",)
    source.policy_value = OrgUrlPolicy()
    cache.forget("org-1")
    assert (await cache.policy("org-1")).block_hosts == ()
    assert source.reads == 3
    # An error is not kept.
    source.fail = True
    cache.forget("org-1")
    with pytest.raises(RuntimeError):
        await cache.policy("org-1")
    source.fail = False
    assert await cache.policy("org-1") == OrgUrlPolicy()


@pytest.mark.parametrize(
    ("value", "site"),
    [
        ("github.com", "https://github.com"),
        ("https://GitHub.com/login?x=1", "https://github.com"),
        ("http://intranet.example.com:8080/a", "http://intranet.example.com:8080"),
        ("https://example.com:443/", "https://example.com"),
        ("https://bücher.de", "https://xn--bcher-kva.de"),
    ],
)
def test_a_site_gets_its_origin(value: str, site: str) -> None:
    assert normal_site(value) == site


@pytest.mark.parametrize("value", ["ftp://a.com", "https://u:p@a.com", "https://", "https://a b"])
def test_a_site_that_is_not_valid_is_refused(value: str) -> None:
    with pytest.raises(LoginError):
        normal_site(value)


@pytest.mark.parametrize(
    ("url", "allowed"),
    [
        ("https://example.com/login", True),
        ("https://accounts.example.com/signin", True),
        ("https://EXAMPLE.com:443/", True),
        ("http://example.com/login", False),  # another scheme
        ("https://example.com:8443/", False),  # another port
        ("https://example.com.evil.test/", False),
        ("https://notexample.com/", False),
        ("https://user@example.com/", False),
        ("about:srcdoc", False),
        ("about:blank", False),
        ("not a url", False),
    ],
)
def test_a_login_fills_only_on_its_site(url: str, allowed: bool) -> None:
    assert site_allows("https://example.com", url) is allowed


async def test_the_quick_check_waits_for_a_fresh_policy_and_fails_closed() -> None:
    now = [0.0]
    source = Source(OrgUrlPolicy(block_hosts=("ads.test",)))
    cache = PolicyCache(source, cache_s=30, clock=lambda: now[0])
    guard = OrgGuard(AllowAll(), "org-1", cache, clock=lambda: now[0])
    await guard.fresh()
    assert guard.quick_problem("https://new.test/a.js") is None
    # An admin in another process blocks new.test at t=1. A navigation at t=29 reads the cached
    # policy, which does not make it fresh for a second period.
    source.policy_value = OrgUrlPolicy(block_hosts=("new.test",))
    now[0] = 29
    await guard.check("https://example.com/")
    now[0] = 30
    assert guard.quick_problem("https://new.test/a.js") == "the URL policy of the org is not loaded"
    await guard.fresh()
    assert (
        guard.quick_problem("https://new.test/a.js") == "new.test is in the block list of the org"
    )
    # invalidate (a change in this process) makes the policy old at once.
    source.policy_value = OrgUrlPolicy()
    cache.forget("org-1")
    guard.invalidate()
    assert guard.quick_problem("https://new.test/a.js") == "the URL policy of the org is not loaded"
    await asyncio.gather(guard.fresh(), guard.fresh())
    assert source.reads == 3  # two callers shared one load
    assert guard.quick_problem("https://new.test/a.js") is None
    # A failed load refuses every request until a load works.
    source.fail = True
    cache.forget("org-1")
    guard.invalidate()
    with pytest.raises(BlockedUrlError):
        await guard.fresh()
    assert guard.quick_problem("https://ok.test/a.js") == "the URL policy of the org cannot be read"
    source.fail = False
    await guard.fresh()
    assert guard.quick_problem("https://ok.test/a.js") is None
