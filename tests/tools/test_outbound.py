"""The outbound URL policy of ``web_extract`` (docs/architecture.md, Section 16.4)."""

from __future__ import annotations

import ipaddress
from collections.abc import Callable, Sequence

import httpx
import pytest

from sama.tools.outbound import (
    BlockedUrlError,
    FetchError,
    IPAddress,
    OutboundClient,
    UrlPolicy,
    address_problem,
)

PUBLIC = "93.184.215.14"

DNS: dict[str, list[str]] = {
    "example.com": [PUBLIC],
    "www.example.com": [PUBLIC],
    "internal.example.com": ["10.1.2.3"],
    "mixed.example.com": [PUBLIC, "192.168.1.1"],
    "loop.example.com": ["127.0.0.1"],
    "meta.example.com": ["169.254.169.254"],
    "mapped.example.com": ["::ffff:10.0.0.1"],
    "v6.example.com": ["2606:2800:21f:cb07:6820:80da:af6b:8b2c"],
}


async def resolver(host: str, port: int) -> Sequence[IPAddress]:
    if host not in DNS:
        raise OSError("no such host")
    return [ipaddress.ip_address(a) for a in DNS[host]]


Handler = Callable[[httpx.Request], httpx.Response]


def page(text: str = "<title>Hi</title><p>Hello</p>") -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/html"}, text=text)

    return handler


def client(handler: Handler, policy: UrlPolicy | None = None) -> OutboundClient:
    return OutboundClient(policy, resolver=resolver, transport=httpx.MockTransport(handler))


@pytest.mark.parametrize(
    ("address", "allowed"),
    [
        (PUBLIC, True),
        ("8.8.8.8", True),
        ("2606:4700:4700::1111", True),
        ("10.0.0.1", False),
        ("172.16.5.4", False),
        ("192.168.0.1", False),
        ("127.0.0.1", False),
        ("0.0.0.0", False),  # noqa: S104 - a test value, not a bind
        ("169.254.1.1", False),
        ("169.254.169.254", False),
        ("100.64.0.1", False),
        ("100.100.100.200", False),
        ("224.0.0.1", False),
        ("240.0.0.1", False),
        ("255.255.255.255", False),
        ("::1", False),
        ("::", False),
        ("fe80::1", False),
        ("fc00::1", False),
        ("fd00:ec2::254", False),
        ("ff02::1", False),
        ("::ffff:127.0.0.1", False),
        ("::ffff:8.8.8.8", True),
        ("2002:0a00:0001::1", False),  # 6to4 of 10.0.0.1
        ("2001:0:4136:e378:8000:63bf:f5ff:fffe", False),  # Teredo of 10.0.0.1
    ],
)
def test_address_problem(address: str, allowed: bool) -> None:
    assert (address_problem(ipaddress.ip_address(address)) is None) is allowed


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("http://example.com/", "scheme must be https"),
        ("ftp://example.com/", "scheme must be https"),
        ("file:///etc/passwd", "scheme must be https"),
        ("https://user:pw@example.com/", "user name or a password"),
        ("https://10.0.0.1/", "not a public address"),
        ("https://127.0.0.1:8080/", "not a public address"),
        ("https://[::1]/", "not a public address"),
        ("https://[::ffff:169.254.169.254]/", "cloud metadata address"),
        ("https://169.254.169.254/latest/meta-data/", "cloud metadata address"),
        ("https://metadata.google.internal/computeMetadata/v1/", "cloud metadata host"),
        ("https://METADATA.google.internal./", "cloud metadata host"),
        ("https://localhost/", "not a public host"),
        ("https://app.localhost/", "not a public host"),
        ("https://internal.example.com/", "not a public address"),
        ("https://mixed.example.com/", "192.168.1.1 is not a public address"),
        ("https://loop.example.com/", "not a public address"),
        ("https://meta.example.com/", "cloud metadata address"),
        ("https://mapped.example.com/", "not a public address"),
        ("not a url", "scheme must be https"),
    ],
)
async def test_blocked_urls(url: str, reason: str) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200)

    outbound = client(handler)
    with pytest.raises(BlockedUrlError, match=reason):
        await outbound.get(url)
    assert calls == []
    await outbound.aclose()


async def test_an_unknown_host_is_a_fetch_error() -> None:
    with pytest.raises(FetchError, match="cannot be resolved"):
        await client(page()).get("https://nowhere.example.com/")


async def test_the_request_goes_to_the_checked_address_with_host_and_sni() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, headers={"content-type": "text/plain"}, text="ok")

    result = await client(handler).get("https://www.example.com/a?b=1")
    [request] = seen
    assert request.url.host == PUBLIC
    assert request.url.path == "/a"
    assert request.url.query == b"b=1"
    assert request.headers["host"] == "www.example.com"
    assert request.headers["accept-encoding"] == "identity"
    assert request.extensions["sni_hostname"] == "www.example.com"
    assert result.url == "https://www.example.com/a?b=1"
    assert result.content == b"ok"


async def test_an_ipv6_address_is_used_in_brackets() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    await client(handler).get("https://v6.example.com:8443/")
    assert seen[0].url.host == "2606:2800:21f:cb07:6820:80da:af6b:8b2c"
    assert seen[0].url.port == 8443
    assert seen[0].headers["host"] == "v6.example.com:8443"


@pytest.mark.parametrize(
    "location",
    [
        "https://10.0.0.1/",
        "https://internal.example.com/",
        "https://169.254.169.254/latest/meta-data/",
        "https://metadata.google.internal/",
        "http://example.com/",
        "https://[::1]/",
    ],
)
async def test_a_redirect_to_a_blocked_url_is_refused(location: str) -> None:
    hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.headers["host"])
        return httpx.Response(302, headers={"location": location})

    with pytest.raises(BlockedUrlError):
        await client(handler).get("https://example.com/")
    assert hosts == ["example.com"]


async def test_a_relative_redirect_is_followed_and_checked() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/old":
            return httpx.Response(301, headers={"location": "/new"})
        return httpx.Response(200, headers={"content-type": "text/plain"}, text="new")

    result = await client(handler).get("https://example.com/old")
    assert result.url == "https://example.com/new"
    assert result.content == b"new"


async def test_the_redirect_limit() -> None:
    hops: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hops.append(request.url.path)
        return httpx.Response(302, headers={"location": f"/{len(hops)}"})

    with pytest.raises(BlockedUrlError, match="more than 2 redirects"):
        await client(handler, UrlPolicy(max_redirects=2)).get("https://example.com/")
    assert len(hops) == 3


async def test_a_redirect_without_a_location_fails() -> None:
    with pytest.raises(FetchError, match="without a location"):
        await client(lambda r: httpx.Response(302)).get("https://example.com/")


async def test_the_body_is_capped() -> None:
    result = await client(page("x" * 5000), UrlPolicy(max_bytes=1000)).get("https://example.com/")
    assert result.truncated
    assert len(result.content) == 1000


async def test_http_is_allowed_only_by_the_policy() -> None:
    result = await client(page(), UrlPolicy(allow_http=True)).get("http://example.com/")
    assert result.status_code == 200


async def test_allow_and_block_lists() -> None:
    allow = client(page(), UrlPolicy(allow_hosts=("example.com",)))
    assert (await allow.get("https://www.example.com/")).status_code == 200
    with pytest.raises(BlockedUrlError, match="allow list"):
        await allow.get("https://v6.example.com.evil.test/")
    block = client(page(), UrlPolicy(block_hosts=("www.example.com",)))
    assert (await block.get("https://example.com/")).status_code == 200
    with pytest.raises(BlockedUrlError, match="block list"):
        await block.get("https://www.example.com/")


async def test_a_transport_error_is_a_fetch_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(FetchError, match="ConnectError"):
        await client(handler).get("https://example.com/")


async def test_the_total_time_limit() -> None:
    import asyncio

    async def slow(host: str, port: int) -> Sequence[IPAddress]:
        await asyncio.sleep(5)
        return [ipaddress.ip_address(PUBLIC)]

    outbound = OutboundClient(
        UrlPolicy(total_timeout_s=0.05), resolver=slow, transport=httpx.MockTransport(page())
    )
    with pytest.raises(FetchError, match="too long"):
        await outbound.get("https://example.com/")
