"""``web_search`` with Brave and ``web_extract`` (docs/architecture.md, Section 16.7)."""

from __future__ import annotations

import ipaddress
from collections.abc import Callable, Sequence

import httpx
import pytest
from pydantic import SecretStr

from sama.tools.outbound import IPAddress, OutboundClient
from sama.tools.web import BRAVE_URL, BraveSearch, SearchError, SearchHit, extract, format_hits
from tests.support import wire_transport


def brave(handler: Callable[[httpx.Request], httpx.Response]) -> BraveSearch:
    return BraveSearch(SecretStr("key-1"), transport=httpx.MockTransport(handler))


async def test_brave_request_and_hits() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "web": {
                    "results": [
                        {"title": "A", "url": "https://a.test", "description": "first"},
                        {"title": "No URL"},
                        "junk",
                        {"title": "B", "url": "https://b.test"},
                        {"title": "C", "url": "https://c.test"},
                    ]
                }
            },
        )

    hits = await brave(handler).search("sama", 2)
    assert hits == [
        SearchHit(title="A", url="https://a.test", snippet="first"),
        SearchHit(title="B", url="https://b.test", snippet=""),
    ]
    [request] = seen
    assert str(request.url).startswith(BRAVE_URL)
    assert request.url.params["q"] == "sama"
    assert request.url.params["count"] == "2"
    assert request.headers["x-subscription-token"] == "key-1"


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (httpx.Response(429), "limits the rate"),
        (httpx.Response(500), "status 500"),
        (httpx.Response(200, text="<html>"), "not JSON"),
    ],
)
async def test_brave_errors(response: httpx.Response, message: str) -> None:
    with pytest.raises(SearchError, match=message):
        await brave(lambda r: response).search("q", 3)


async def test_brave_reply_without_results() -> None:
    assert await brave(lambda r: httpx.Response(200, json=[1])).search("q", 3) == []
    assert await brave(lambda r: httpx.Response(200, json={})).search("q", 3) == []


async def test_brave_key_is_not_in_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(SearchError) as caught:
        await brave(handler).search("q", 3)
    assert "key-1" not in str(caught.value)


def test_format_hits() -> None:
    assert format_hits("q", []) == (
        '<untrusted_content source="web_search">\nNo results for: q\n</untrusted_content>'
    )


async def _resolve(host: str, port: int) -> Sequence[IPAddress]:
    return [ipaddress.ip_address("10.0.0.1" if host == "private.test" else "93.184.215.14")]


async def test_extract_reports_each_url() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        match request.headers["host"]:
            case "page.test":
                return httpx.Response(
                    200,
                    headers={"content-type": "text/html"},
                    text="<title>T</title><p>Body</p>",
                )
            case "missing.test":
                return httpx.Response(404)
            case "idna.test":
                # "idna" is a codec, but not a text charset. It raises UnicodeError.
                return httpx.Response(
                    200,
                    headers={"content-type": "text/plain; charset=idna"},
                    content=b"caf\xc3\xa9",
                )
            case _:
                return httpx.Response(200, headers={"content-type": "image/png"}, content=b"\x89")

    client = OutboundClient(resolver=_resolve, transport=wire_transport(handler))
    text = await extract(
        client,
        [
            "https://page.test/",
            "https://idna.test/",
            "https://missing.test/",
            "https://image.test/",
            "https://private.test/",
        ],
    )
    assert text.startswith(
        '<untrusted_content source="web_extract" url="https://page.test/">\n'
        "Title: T\n\nBody\n</untrusted_content>\n\n"
        '<untrusted_content source="web_extract" url="https://idna.test/">\ncafé\n'
        "</untrusted_content>\n\n"
    )
    assert "[https://missing.test/] error: the server returned status 404" in text
    assert "[https://image.test/] error: the content type image/png is not supported" in text
    assert "[https://private.test/] error: private.test resolves to 10.0.0.1" in text


async def test_an_unexpected_error_fails_only_its_own_url(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def page_text(content: bytes, content_type: str) -> tuple[str, str]:
        if content == b"bad":
            raise RuntimeError("secret detail")
        return "", content.decode()

    monkeypatch.setattr("sama.tools.web.page_text", page_text)

    def handler(request: httpx.Request) -> httpx.Response:
        body = b"bad" if request.headers["host"] == "bad.test" else b"good"
        return httpx.Response(200, headers={"content-type": "text/plain"}, content=body)

    client = OutboundClient(resolver=_resolve, transport=wire_transport(handler))
    text = await extract(client, ["https://good.test/", "https://bad.test/"])
    assert "\ngood\n" in text
    assert "[https://bad.test/] error: the page could not be read" in text
    assert "secret detail" not in text
    assert "secret detail" not in caplog.text
    assert "error=RuntimeError" in caplog.text
