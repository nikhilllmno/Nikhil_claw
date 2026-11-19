"""Unit tests of the Slack client, the request signature, and the Slack texts (Section 11.6)."""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from pydantic import SecretStr

from sama.gateway.slack import MAX_CARD_CHARS, approval_blocks, escape, unescape
from sama.gateway.slack_client import (
    BOT_TOKEN,
    SIGNING_SECRET,
    SlackClient,
    SlackError,
    signature,
    signed,
)
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


def test_an_approval_card_has_two_buttons_and_plain_text() -> None:
    row = SimpleNamespace(
        id=uuid.uuid4(),
        tool_name="Bash",
        arguments={"command": "x" * 5000},
        reason="deletes files",
    )
    text, blocks = approval_blocks(row)
    assert text.startswith("Approval needed: Bash")
    assert len(text) <= MAX_CARD_CHARS
    section, actions = blocks
    assert section["text"]["type"] == "plain_text", "the model text cannot format the card"
    values = [button["value"] for button in actions["elements"]]
    assert values == [f"ap:{row.id.hex}:y", f"ap:{row.id.hex}:n"]


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
