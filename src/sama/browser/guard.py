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
check and the connection (DNS rebinding) is not fully stopped. M5.3 adds the per-org URL policy.
"""

from __future__ import annotations

import ipaddress
from typing import Final
from urllib.parse import urlsplit

from sama.tools.outbound import (
    METADATA_HOSTS,
    BlockedUrlError,
    FetchError,
    OutboundClient,
    UrlPolicy,
    address_problem,
)

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
