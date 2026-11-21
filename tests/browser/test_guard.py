"""The URL policy of the browser tools (docs/architecture.md, Section 16.4)."""

from __future__ import annotations

import ipaddress
from collections.abc import AsyncIterator, Sequence

import pytest

from sama.browser.guard import UrlGuard
from sama.tools.outbound import BlockedUrlError, IPAddress, OutboundClient, UrlPolicy

ADDRESSES = {
    "public.example": "93.184.215.14",
    "inside.example": "10.0.0.7",
    "rebind.example": "169.254.169.254",
}


async def _resolve(host: str, port: int) -> Sequence[IPAddress]:
    if host not in ADDRESSES:
        raise OSError("no such host")
    return [ipaddress.ip_address(ADDRESSES[host])]


@pytest.fixture
async def guard() -> AsyncIterator[UrlGuard]:
    guard = UrlGuard(OutboundClient(UrlPolicy(allow_http=True), resolver=_resolve))
    yield guard
    await guard.aclose()


@pytest.mark.parametrize(
    "url",
    ["https://public.example/", "http://public.example/a?b=c", "about:blank"],
)
async def test_check_allows_public_pages(guard: UrlGuard, url: str) -> None:
    await guard.check(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://localhost:8080/",
        "http://10.1.2.3/",
        "http://192.168.0.1/",
        "http://[::1]/",
        "http://169.254.169.254/latest/meta-data/",
        "http://metadata.google.internal/",
        "https://inside.example/",  # a public name with a private address
        "https://rebind.example/",
        "https://unknown.example/",  # fails closed when the host does not resolve
        "file:///etc/passwd",
        "ftp://public.example/",
    ],
)
async def test_check_blocks_private_and_odd_urls(guard: UrlGuard, url: str) -> None:
    with pytest.raises(BlockedUrlError):
        await guard.check(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://public.example/app.js",
        "https://inside.example/x.png",  # no DNS in the quick check
        "wss://public.example/socket",
        "data:image/png;base64,AAAA",
        "blob:https://public.example/1234",
    ],
)
def test_quick_problem_allows_subresources(guard: UrlGuard, url: str) -> None:
    assert guard.quick_problem(url) is None


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("http://127.0.0.1:9222/json", "not a public address"),
        ("http://10.0.0.1/a.js", "not a public address"),
        ("http://[fd00::1]/a.js", ""),
        ("http://localhost/a.js", "not a public host"),
        ("http://api.localhost/a.js", "not a public host"),
        ("http://metadata.google.internal/", "metadata"),
        ("http://169.254.169.254/", ""),
        ("file:///etc/hosts", "scheme file"),
        ("chrome://settings", "scheme chrome"),
        ("http:///nohost", "no host"),
    ],
)
def test_quick_problem_blocks_private_hosts(guard: UrlGuard, url: str, reason: str) -> None:
    problem = guard.quick_problem(url)
    assert problem is not None
    assert reason in problem
