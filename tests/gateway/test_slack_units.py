"""Unit tests of the Slack client, the request signature, and the Slack texts (Section 11.6)."""

from __future__ import annotations

import gzip
import json
import uuid
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from pydantic import SecretStr

from sama.gateway.slack import MAX_CARD_CHARS, SeenEvents, approval_blocks, escape, unescape
from sama.gateway.slack_client import (
    BOT_TOKEN,
    SIGNING_SECRET,
    SlackClient,
    SlackError,
    signature,
    signed,
)
from sama.gateway.telegram import _approval_card as telegram_card
from sama.integrations.service import SLACK_SCOPES, SlackSetup, slack_manifest
from tests.integrations.support import SIGNING, SLACK_TOKEN, FakeSlack

NOW = 1_700_000_000


def test_a_signature_is_checked_with_the_secret_and_the_time() -> None:
    body = b"token=x&text=hi"
    given = signature(SIGNING, str(NOW), body)
    assert given.startswith("v0=")
    assert signed(SIGNING, str(NOW), given, body, NOW)
    assert signed(SIGNING, str(NOW), given, body, NOW + 300)
    assert not signed(SIGNING, str(NOW), given, body, NOW + 301), "an old copy is refused"
    assert not signed(SIGNING, str(NOW), given, body, NOW - 301)
    assert not signed(SIGNING, str(NOW), given, body + b"x", NOW)
    assert not signed("6" * 32, str(NOW), given, body, NOW)
    assert not signed(SIGNING, None, given, body, NOW)
    assert not signed(SIGNING, str(NOW), None, body, NOW)
    assert not signed(SIGNING, "-1", given, body, NOW)


def test_the_formats_of_the_secrets() -> None:
    assert BOT_TOKEN.fullmatch(SLACK_TOKEN)
    assert not BOT_TOKEN.fullmatch("xoxp-" + "1" * 30), "a user token is not a bot token"
    assert SIGNING_SECRET.fullmatch(SIGNING)
    assert not SIGNING_SECRET.fullmatch("Z" * 32)


def test_text_is_escaped_so_that_it_cannot_mention() -> None:
    raw = "a <!channel> & <@U1> > b"
    assert escape(raw) == "a &lt;!channel&gt; &amp; &lt;@U1&gt; &gt; b"
    assert unescape(escape(raw)) == raw
    assert unescape("&amp;lt;") == "&lt;", "an escaped escape stays one level deep"


def test_an_approval_card_has_three_buttons_and_plain_text() -> None:
    row = SimpleNamespace(
        id=uuid.uuid4(),
        tool_name="Bash",
        arguments={"command": "x" * 5000},
        reason="deletes files",
        answerers=None,
    )
    text, blocks = approval_blocks(row)
    assert text.startswith("Approval needed: Bash")
    assert len(text) <= MAX_CARD_CHARS
    section, actions = blocks
    assert section["text"]["type"] == "plain_text", "the model text cannot format the card"
    values = [button["value"] for button in actions["elements"]]
    assert values == [f"ap:{row.id.hex}:y", f"ap:{row.id.hex}:n", f"ap:{row.id.hex}:a"]
    assert actions["elements"][2]["text"]["text"] == "Always allow for me"


def test_a_proposal_card_has_no_always_allow_button() -> None:
    row = SimpleNamespace(
        id=uuid.uuid4(),
        tool_name="memory",
        arguments={},
        reason="the agent proposes a change of workspace memory",
        answerers="A workspace admin or the org owner can decide this.",
        proposal=True,
    )
    text, blocks = approval_blocks(row)
    assert "A workspace admin or the org owner can decide this." in text
    values = [button["value"] for button in blocks[1]["elements"]]
    assert values == [f"ap:{row.id.hex}:y", f"ap:{row.id.hex}:n"], "a rule cannot approve it"


@pytest.mark.parametrize("tool", ["repo_push_approval", "mcp__sama__repo_push_approval"])
def test_a_push_approval_card_has_no_always_allow_button(tool: str) -> None:
    """The gate asks for a protected push each time (Section 15.1): a rule cannot approve it."""
    row = SimpleNamespace(
        id=uuid.uuid4(), tool_name=tool, arguments={}, reason="a protected path", answerers=None
    )
    _, blocks = approval_blocks(row)
    values = [button["value"] for button in blocks[1]["elements"]]
    assert values == [f"ap:{row.id.hex}:y", f"ap:{row.id.hex}:n"]
    _, buttons = telegram_card(row)
    assert [b["callback_data"] for line in buttons for b in line] == values


def test_the_manifest_holds_the_urls_of_the_integration() -> None:
    urls = ("https://s.test/e", "https://s.test/i", "https://s.test/c")
    manifest = slack_manifest(*urls)
    settings = manifest["settings"]
    assert settings["event_subscriptions"]["request_url"] == urls[0]
    assert settings["event_subscriptions"]["bot_events"] == ["app_mention", "message.im"]
    assert settings["interactivity"]["request_url"] == urls[1]
    assert manifest["features"]["slash_commands"][0]["url"] == urls[2]
    assert manifest["oauth_config"]["scopes"]["bot"] == list(SLACK_SCOPES)
    assert settings["socket_mode_enabled"] is False

    view = SimpleNamespace(public=lambda: {})
    setup = SlackSetup(view, *urls, manifest=manifest)  # type: ignore[arg-type]
    link = urlsplit(setup.manifest_link)
    assert (link.netloc, link.path) == ("api.slack.com", "/apps")
    assert json.loads(parse_qs(link.query)["manifest_json"][0]) == manifest


async def test_the_client_waits_when_slack_asks_and_hides_the_token() -> None:
    slack = FakeSlack()
    client = slack.client(SecretStr(SLACK_TOKEN))
    slack.limited.add("chat.postMessage")
    ts = await client.post_message("D1", "hi", thread_ts="1.1")
    assert ts
    assert [name for name, _ in slack.calls] == ["chat.postMessage", "chat.postMessage"]
    assert slack.calls[-1][1]["thread_ts"] == "1.1"
    assert slack.tokens[-1][0] == SLACK_TOKEN, "the token goes in the Authorization header"

    slack.limited.add("chat.update")
    with pytest.raises(SlackError) as caught:
        await client.without_retries().update_message("D1", ts, "edit")
    assert caught.value.retry_after == 0
    slack.failing.add("chat.update")
    with pytest.raises(SlackError) as caught:
        await client.update_message("D1", ts, "edit")
    assert "channel_not_found" in str(caught.value)
    assert SLACK_TOKEN not in str(caught.value)
    await client.aclose()


async def test_the_client_reports_a_broken_answer() -> None:
    def broken(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="bad gateway")

    client = SlackClient(
        SecretStr(SLACK_TOKEN), http=httpx.AsyncClient(transport=httpx.MockTransport(broken))
    )
    with pytest.raises(SlackError, match=r"auth\.test failed: 502"):
        await client.auth_test()
    assert await client.app_id("B1") is None, "the app id is best effort"
    await client.aclose()


def test_seen_events_forget_old_ids_and_keep_a_max_size() -> None:
    now = [0.0]
    seen = SeenEvents(keep_s=10, max_size=3, clock=lambda: now[0])
    assert not seen.is_repeat("a")
    assert seen.is_repeat("a")
    now[0] = 11
    assert not seen.is_repeat("a")
    for key in ("b", "c"):
        assert not seen.is_repeat(key)
    # A full log still finds a repeat of its oldest id.
    assert seen.is_repeat("a")
    # A new id makes room: the oldest id goes first.
    assert not seen.is_repeat("d")
    assert not seen.is_repeat("a")


FILE = "https://files.slack.com/files-pri/T1-F1/download/a.png"


def _downloader(handler: Any) -> SlackClient:
    return SlackClient(
        SecretStr(SLACK_TOKEN), http=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )


async def test_a_download_sends_the_token_to_a_slack_host_only() -> None:
    seen: list[httpx.Request] = []

    def ok(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=b"PNGDATA")

    client = _downloader(ok)
    assert await client.download(FILE, max_bytes=100) == b"PNGDATA"
    assert seen[0].headers["Authorization"] == f"Bearer {SLACK_TOKEN}"
    for url in (
        "https://evil.example/a.png",
        "http://files.slack.com/a.png",
        "https://slack.com.evil.io/a.png",
        "https://notslack.com/a.png",
        "file:///etc/passwd",
    ):
        with pytest.raises(SlackError, match="not a Slack file URL"):
            await client.download(url, max_bytes=100)
    assert len(seen) == 1, "the other URLs never left"
    await client.aclose()


async def test_a_download_does_not_follow_a_redirect() -> None:
    seen: list[str] = []

    def redirect(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(302, headers={"Location": "https://evil.example/steal"})

    client = _downloader(redirect)
    with pytest.raises(SlackError, match="download failed: 302"):
        await client.download(FILE, max_bytes=100)
    assert seen == [FILE]
    await client.aclose()


async def test_a_download_has_a_byte_cap() -> None:
    def big(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 101)

    def declared(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x", headers={"Content-Length": "5000"})

    def streamed(request: httpx.Request) -> httpx.Response:
        async def chunks() -> Any:
            for _ in range(5):
                yield b"x" * 30

        return httpx.Response(200, content=chunks())

    for handler in (big, declared, streamed):
        client = _downloader(handler)
        with pytest.raises(SlackError, match="too large"):
            await client.download(FILE, max_bytes=100)
        await client.aclose()


async def test_a_failed_download_is_a_slack_error_without_the_url() -> None:
    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    client = _downloader(broken)
    with pytest.raises(SlackError, match="download failed: ConnectError") as error:
        await client.download(FILE, max_bytes=100)
    assert "files.slack.com" not in str(error.value)
    await client.aclose()


async def test_a_download_asks_for_identity_encoding_and_refuses_another() -> None:
    seen: list[httpx.Request] = []

    def gzipped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200, content=gzip.compress(b"x" * 5_000), headers={"Content-Encoding": "gzip"}
        )

    client = _downloader(gzipped)
    with pytest.raises(SlackError, match="encoded"):
        await client.download(FILE, max_bytes=100_000)
    assert seen[0].headers["Accept-Encoding"] == "identity"
    await client.aclose()


async def test_a_compressed_bomb_never_grows_past_the_cap() -> None:
    sent: list[int] = []

    def bomb(request: httpx.Request) -> httpx.Response:
        async def chunks() -> Any:
            for _ in range(1_000):
                sent.append(1)
                yield b"x" * 1_000

        return httpx.Response(200, content=chunks())

    client = _downloader(bomb)
    with pytest.raises(SlackError, match="too large"):
        await client.download(FILE, max_bytes=2_500)
    assert len(sent) <= 3 + 1, "the read stops at the cap"
    await client.aclose()


async def test_the_cap_is_checked_before_a_chunk_is_added() -> None:
    def body(size: int) -> Any:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, content=b"x" * size, headers={"Content-Encoding": "identity"}
            )

        return handler

    client = _downloader(body(100))
    assert len(await client.download(FILE, max_bytes=100)) == 100
    await client.aclose()
    client = _downloader(body(101))
    with pytest.raises(SlackError, match="too large"):
        await client.download(FILE, max_bytes=100)
    await client.aclose()
