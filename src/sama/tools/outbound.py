"""The outbound HTTP client of the control plane (docs/architecture.md, Section 16.4).

Requests that start on the control plane (``web_extract``, later connector calls and webhook
deliveries) go through ``OutboundClient``. It enforces one URL policy:

- ``https`` only (``http`` if the policy allows it), no user name or password in the URL.
- Every resolved address of the host must be a public unicast address. The client then
  connects to the checked address, so DNS rebinding cannot change the target.
- Redirects are checked hop by hop, at most ``max_redirects`` hops.
- Size and time limits apply. The body is read as raw bytes, and a compressed body is refused,
  so a small response cannot expand past the size limit.
- No cookies are kept or sent, and no connections are kept, so one request cannot leak state
  to the next host or tenant.
"""

from __future__ import annotations

import asyncio
import http.cookiejar
import ipaddress
import socket
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Final

import httpx

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
Resolver = Callable[[str, int], Awaitable[Sequence[IPAddress]]]

USER_AGENT: Final = "sama/0.1 (+https://github.com/nikhilllmno/Nikhil_claw)"
REDIRECT_CODES: Final = frozenset({301, 302, 303, 307, 308})

METADATA_ADDRESSES: Final = frozenset(
    ipaddress.ip_address(a)
    for a in (
        "169.254.169.254",  # AWS, GCP, Azure, and most clouds
        "169.254.170.2",  # AWS ECS task metadata
        "100.100.100.200",  # Alibaba Cloud
        "192.0.0.192",  # Oracle Cloud
        "fd00:ec2::254",  # AWS IPv6
    )
)
METADATA_HOSTS: Final = frozenset({"metadata.google.internal", "metadata.goog", "metadata"})


class BlockedUrlError(Exception):
    """The URL policy refused the request. The message says why, for the agent."""


class FetchError(Exception):
    """The request was allowed but failed (connection, time, or size)."""


@dataclass(frozen=True)
class UrlPolicy:
    allow_http: bool = False
    allow_hosts: tuple[str, ...] = ()
    """If not empty, only these hosts and their subdomains."""
    block_hosts: tuple[str, ...] = ()
    """These hosts and their subdomains are refused."""
    max_redirects: int = 5
    max_bytes: int = 5 * 1024 * 1024
    hop_timeout_s: float = 10.0
    total_timeout_s: float = 20.0


@dataclass(frozen=True)
class FetchResult:
    url: str
    """The final URL, after redirects."""
    status_code: int
    content_type: str
    content: bytes
    truncated: bool
    """True if the body was larger than ``max_bytes`` and the client stopped reading."""


def address_problem(address: IPAddress) -> str | None:
    """Why the address is not allowed, or ``None`` for a public unicast address.

    ``is_global`` is false also for unspecified and reserved addresses. Multicast needs its
    own check, because some multicast ranges are global."""
    candidates: list[IPAddress] = [address]
    if isinstance(address, ipaddress.IPv6Address):
        for embedded in (address.ipv4_mapped, address.sixtofour):
            if embedded is not None:
                candidates.append(embedded)
        if address.teredo is not None:
            candidates.extend(address.teredo)
    if any(candidate in METADATA_ADDRESSES for candidate in candidates):
        return f"{address} is a cloud metadata address"
    for candidate in candidates:
        if not candidate.is_global or candidate.is_multicast:
            return f"{address} is not a public address"
    return None


def ascii_host(host: str) -> str:
    """The lower-case IDNA (punycode) form of a host name, without a final dot."""
    try:
        raw = httpx.URL(f"https://{host.strip().rstrip('.')}/").raw_host
    except httpx.InvalidURL:
        raise ValueError(f"{host!r} is not a valid host name") from None
    return raw.decode("ascii").lower()


def _host_matches(host: str, patterns: Sequence[str]) -> bool:
    return any(host == p or host.endswith("." + p) for p in patterns)


class _NoCookies(http.cookiejar.DefaultCookiePolicy):
    def set_ok(self, cookie: http.cookiejar.Cookie, request: object) -> bool:
        return False

    def return_ok(self, cookie: http.cookiejar.Cookie, request: object) -> bool:
        return False


async def system_resolver(host: str, port: int) -> Sequence[IPAddress]:
    infos = await asyncio.get_running_loop().getaddrinfo(
        host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP
    )
    # An IPv6 address can carry a zone ("fe80::1%eth0"). The zone is never public.
    return list(dict.fromkeys(ipaddress.ip_address(str(i[4][0]).split("%", 1)[0]) for i in infos))


class OutboundClient:
    """GET requests under a ``UrlPolicy``. Create one per policy. Close it with ``aclose``."""

    def __init__(
        self,
        policy: UrlPolicy | None = None,
        *,
        resolver: Resolver = system_resolver,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.policy = policy or UrlPolicy()
        self._allow = tuple(ascii_host(h) for h in self.policy.allow_hosts)
        self._block = tuple(ascii_host(h) for h in self.policy.block_hosts)
        self._resolve = resolver
        self._client = httpx.AsyncClient(
            transport=transport,
            follow_redirects=False,
            trust_env=False,
            # The client sends requests to IP addresses, so a kept cookie would go to every
            # host on that address, also for another tenant. Give the jar itself: httpx copies a
            # ``Cookies`` object into a new jar without the policy.
            cookies=http.cookiejar.CookieJar(policy=_NoCookies()),
            timeout=httpx.Timeout(self.policy.hop_timeout_s),
            # No kept connections: two hosts on one address must not share a TLS session.
            limits=httpx.Limits(max_keepalive_connections=0),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def check(self, url: str) -> tuple[httpx.URL, IPAddress]:
        """Checks the URL and its addresses. Returns the parsed URL and the address to use."""
        try:
            parsed = httpx.URL(url)
        except (httpx.InvalidURL, TypeError, ValueError):
            raise BlockedUrlError("the URL is not valid") from None
        schemes = ("https", "http") if self.policy.allow_http else ("https",)
        if parsed.scheme not in schemes:
            raise BlockedUrlError(f"the scheme must be {' or '.join(schemes)}")
        if parsed.userinfo:
            raise BlockedUrlError("a URL with a user name or a password is not allowed")
        # The IDNA form, so "bücher.de" and "xn--bcher-kva.de" match the same list entry.
        host = parsed.raw_host.decode("ascii").lower().rstrip(".")
        if not host:
            raise BlockedUrlError("the URL has no host")
        if host in METADATA_HOSTS:
            raise BlockedUrlError(f"{host} is a cloud metadata host")
        if host == "localhost" or host.endswith(".localhost"):
            raise BlockedUrlError(f"{host} is not a public host")
        if self._allow and not _host_matches(host, self._allow):
            raise BlockedUrlError(f"{host} is not in the allow list of the org")
        if _host_matches(host, self._block):
            raise BlockedUrlError(f"{host} is in the block list of the org")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        try:
            addresses: Sequence[IPAddress] = [ipaddress.ip_address(host)]
        except ValueError:
            try:
                addresses = await self._resolve(host, port)
            except (OSError, UnicodeError):
                raise FetchError(f"{host} cannot be resolved") from None
        if not addresses:
            raise FetchError(f"{host} cannot be resolved")
        for address in addresses:
            problem = address_problem(address)
            if problem is not None:
                raise BlockedUrlError(f"{host} resolves to {problem}")
        return parsed, addresses[0]

    async def get(self, url: str) -> FetchResult:
        """GETs the URL and follows checked redirects. Raises ``BlockedUrlError`` or
        ``FetchError``."""
        try:
            async with asyncio.timeout(self.policy.total_timeout_s):
                return await self._get(url)
        except TimeoutError:
            raise FetchError("the request took too long") from None

    async def _get(self, url: str) -> FetchResult:
        current = url
        for _hop in range(self.policy.max_redirects + 1):
            parsed, address = await self.check(current)
            result = await self._send(parsed, address)
            if isinstance(result, FetchResult):
                return result
            current = result
        raise BlockedUrlError(f"more than {self.policy.max_redirects} redirects")

    async def _send(self, parsed: httpx.URL, address: IPAddress) -> FetchResult | str:
        """Sends one request to the checked address. Returns the result, or the next URL of a
        redirect."""
        host = parsed.raw_host.decode("ascii")
        target = parsed.copy_with(host=str(address))
        headers = {
            "host": parsed.netloc.decode("ascii"),
            "user-agent": USER_AGENT,
            "accept": "text/html, text/plain, application/json, */*;q=0.5",
            "accept-encoding": "identity",
        }
        extensions = {"sni_hostname": host} if parsed.scheme == "https" else {}
        request = self._client.build_request("GET", target, headers=headers, extensions=extensions)
        try:
            response = await self._client.send(request, stream=True)
        except httpx.HTTPError as error:
            raise FetchError(f"the request to {host} failed: {type(error).__name__}") from None
        try:
            if response.status_code in REDIRECT_CODES:
                location = response.headers.get("location")
                if not location:
                    raise FetchError(f"{host} sent a redirect without a location")
                return str(parsed.join(location))
            encoding = response.headers.get("content-encoding", "identity").strip().lower()
            if encoding not in ("", "identity"):
                # The client asks for identity. A compressed body could expand past the limit.
                raise FetchError(f"{host} sent a compressed body ({encoding})")
            body = bytearray()
            truncated = False
            try:
                async for chunk in response.aiter_raw():
                    room = self.policy.max_bytes - len(body)
                    if len(chunk) > room:
                        body += chunk[:room]
                        truncated = True
                        break
                    body += chunk
            except httpx.HTTPError as error:
                raise FetchError(f"reading {host} failed: {type(error).__name__}") from None
            return FetchResult(
                url=str(parsed),
                status_code=response.status_code,
                content_type=response.headers.get("content-type", ""),
                content=bytes(body),
                truncated=truncated,
            )
        finally:
            await response.aclose()
