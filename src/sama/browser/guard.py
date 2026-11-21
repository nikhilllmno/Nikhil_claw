"""The outbound URL policy for browser navigation (docs/architecture.md, Section 16.4).

The browser runs outside sama (in Browserbase for the ``cloud`` target), but it acts for the
org, so the same address rules as ``web_extract`` apply:

- ``check`` is the full check of ``OutboundClient.check`` with DNS. The service runs it before
  each ``browser_open`` and each new tab with a URL, and the session runs it for each navigation
  request of a page (links, forms, redirects that the page starts, frames) and for the final URL
  after a navigation.
- ``quick_problem`` is a check without DNS for every other request of a page: it refuses
  literal private addresses, ``localhost``, and the metadata hosts.

The browser resolves the host again when it connects, so a DNS answer that changes between the
check and the connection (DNS rebinding) is not fully stopped.

``OrgGuard`` adds the host lists of one org (``sama.browser.policy``, M5.3) to these rules.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import math
import time
from collections.abc import Callable
from typing import Final, Protocol, runtime_checkable
from urllib.parse import urlsplit

from sama.browser.policy import CACHE_S, OPEN, OrgUrlPolicy, PolicySource, url_host
from sama.browser.target import NavigationGuard
from sama.tools.outbound import (
    METADATA_HOSTS,
    BlockedUrlError,
    FetchError,
    OutboundClient,
    UrlPolicy,
    address_problem,
)

log = logging.getLogger("sama.browser")

BLANK: Final = "about:blank"
PAGE_SCHEMES: Final = frozenset({"http", "https"})
SUBRESOURCE_SCHEMES: Final = frozenset({"http", "https", "ws", "wss", "data", "blob"})
"""Schemes that a page can load. ``data`` and ``blob`` have no host."""


class UrlGuard:
    """The URL policy of the browser tools. Close it with ``aclose``."""

    def __init__(self, outbound: OutboundClient | None = None) -> None:
        self._outbound = outbound or OutboundClient(UrlPolicy(allow_http=True))

    async def aclose(self) -> None:
        await self._outbound.aclose()

    async def check(self, url: str) -> None:
        """Raises ``BlockedUrlError`` if the browser must not open ``url``. A host that does not
        resolve is refused too: the check fails closed."""
        if url == BLANK:
            return
        try:
            await self._outbound.check(url)
        except FetchError as error:
            raise BlockedUrlError(str(error)) from None

    def quick_problem(self, url: str) -> str | None:
        try:
            parts = urlsplit(url)
            scheme = parts.scheme.lower()
            host = (parts.hostname or "").rstrip(".")
        except ValueError:
            return "the URL is not valid"
        if scheme not in SUBRESOURCE_SCHEMES:
            return f"the scheme {scheme or '(none)'} is not allowed"
        if scheme in {"data", "blob"}:
            return None
        if not host:
            return "the URL has no host"
        if host in METADATA_HOSTS:
            return f"{host} is a cloud metadata host"
        if host == "localhost" or host.endswith(".localhost"):
            return f"{host} is not a public host"
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            return None
        return address_problem(address)


class OrgGuard:
    """The URL guard of the browser sessions of one org: the host lists of the org first, then
    the address rules of ``UrlGuard``.

    ``check`` reads the policy of the org (through a cache) each time, so a change applies to a
    live session too. When the policy cannot be read, the check fails closed.

    ``quick_problem`` (the requests of a page that are not a navigation) has no await, so the
    session first awaits ``fresh``: it loads the policy again when it is past the time of the
    cache or an admin changed it, and the request waits for the load. ``quick_problem`` refuses
    every request while the policy is old or after a failed load, so it fails closed also for a
    caller that does not call ``fresh``.
    """

    def __init__(
        self,
        base: NavigationGuard,
        org_id: str,
        policies: PolicySource | None,
        *,
        policy: OrgUrlPolicy = OPEN,
        fresh_s: float = CACHE_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._base = base
        self._org_id = org_id
        self._policies = policies
        self._policy = policy
        self._fresh_s = fresh_s
        self._clock = clock
        self._fresh_until = -math.inf if policies is not None else math.inf
        self._failed = False
        self._loading: asyncio.Future[OrgUrlPolicy] | None = None

    async def load(self) -> OrgUrlPolicy:
        """Reads the policy of the org. Raises ``BlockedUrlError`` when it cannot be read. The
        policy is fresh until the cache of the source drops it, so a cached policy does not get
        a second period."""
        if self._policies is None:
            return self._policy
        try:
            until_of = getattr(self._policies, "policy_until", None)
            policy: OrgUrlPolicy
            if until_of is not None:
                policy, until = await until_of(self._org_id)
            else:
                policy = await self._policies.policy(self._org_id)
                until = self._clock() + self._fresh_s
        except Exception:
            self._failed = True
            log.warning("the URL policy cannot be read org=%s", self._org_id, exc_info=True)
            raise BlockedUrlError("the URL policy of the org cannot be read; try again") from None
        self._policy, self._failed, self._fresh_until = policy, False, until
        return policy

    def _stale(self) -> bool:
        return self._failed or self._clock() >= self._fresh_until

    async def fresh(self) -> None:
        """Loads the policy when it is old, changed, or not readable last time. Callers at the
        same time share one load. Raises ``BlockedUrlError`` when it cannot be read."""
        if not self._stale():
            return
        if self._loading is None or self._loading.done():
            self._loading = asyncio.ensure_future(self.load())
        await asyncio.shield(self._loading)

    def invalidate(self) -> None:
        """Marks the kept policy as old, after an admin changed it."""
        if self._policies is not None:
            self._fresh_until = -math.inf

    async def check(self, url: str) -> None:
        if url == BLANK:
            return
        policy = await self.load()
        problem = _list_problem(url, policy.host_problem)
        if problem is not None:
            raise BlockedUrlError(problem)
        await self._base.check(url)

    def quick_problem(self, url: str) -> str | None:
        problem = self._base.quick_problem(url)
        if problem is not None:
            return problem
        if self._failed:
            return "the URL policy of the org cannot be read"
        if self._stale():
            return "the URL policy of the org is not loaded"
        return _list_problem(url, self._policy.block_problem)


@runtime_checkable
class FreshGuard(Protocol):
    """A guard whose ``quick_problem`` needs a load first (``OrgGuard``)."""

    async def fresh(self) -> None: ...


def _list_problem(url: str, rule: Callable[[str], str | None]) -> str | None:
    try:
        host = url_host(url)
    except ValueError:
        return "the URL is not valid"
    return rule(host) if host is not None else None
