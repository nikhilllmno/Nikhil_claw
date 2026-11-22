"""Connect cards, aliases, and chat buttons (docs/architecture.md, Section 10.3.4, M8.1)."""

from __future__ import annotations

import json

from sama.connectors.chat import card, cards, return_page, strip_cards
from sama.connectors.manifest import Catalog, normalize_alias
from sama.gateway.slack import connect_blocks
from tests.connectors.support import CATALOG


def test_a_card_round_trips_and_keeps_only_https_links() -> None:
    line = card(
        connector="gmail",
        title="Gmail",
        url="https://connect.composio.dev/link/1",
        connection_id="c-1",
        state="initiated",
        scope="personal",
    )
    text = f"Open the link.\n{line}"
    [found] = cards(text)
    assert found["url"] == "https://connect.composio.dev/link/1"
    assert strip_cards(text) == "Open the link."
    bad = card(
        connector="x",
        title="X",
        url="javascript:alert(1)",
        connection_id="c",
        state="initiated",
        scope="personal",
    )
    assert cards(bad)[0]["url"] is None
    assert cards("<sama-connect>{not json}</sama-connect>") == []
    huge = card(
        connector="x",
        title="X",
        url="https://a.test/" + "a" * 3000,
        connection_id="c",
        state="initiated",
        scope="personal",
    )
    assert cards(huge)[0]["url"] is None


def test_the_return_page_escapes_its_text() -> None:
    page = return_page("<b>", "<i>", "a & b", link='https://x.test/"q')
    assert "<b>" not in page.split("<style>")[0].split("<title>")[1]
    assert "&amp;" in page
    assert "&quot;" in page
    assert "<script" not in page


def test_the_words_of_a_user_map_to_connectors() -> None:
    catalog = Catalog.load(CATALOG)
    assert normalize_alias("  My   Google_Calendar ") == "google calendar"
    assert [m.name for m in catalog.resolve("my email")] == ["gmail", "outlook"]
    assert [m.name for m in catalog.resolve("inbox")] == ["gmail", "outlook"]
    assert [m.name for m in catalog.resolve("calendar")] == ["googlecalendar"]
    assert [m.name for m in catalog.resolve("Google Drive")] == ["googledrive"]
    assert [m.name for m in catalog.resolve("supabase mcp")] == ["supabase-mcp"]
    assert [m.name for m in catalog.resolve("slack")] == ["slack"]
    assert catalog.resolve("composio") == []
    assert catalog.resolve("") == []


def test_the_slack_connect_card_is_a_link_button() -> None:
    text, blocks = connect_blocks({"title": "Gmail", "url": "https://connect.composio.dev/l"})
    assert "Gmail" in text
    button = blocks[1]["elements"][0]
    assert button["url"] == "https://connect.composio.dev/l"
    assert button["text"]["text"] == "Connect Gmail"
    assert "value" not in button


def test_a_card_in_json_text_blocks_is_found() -> None:
    """The OpenAI runtime gives the tool output as JSON text blocks (M8.1, staging)."""
    line = card(
        connector="gmail",
        title="Gmail",
        url="https://sama.test/connections/go?connection=c&t=x",
        connection_id="c",
        state="initiated",
        scope="personal",
    )
    wrapped = json.dumps({"type": "text", "text": f"{line}\nThe sign-in is open."})
    [found] = cards(wrapped)
    assert found["url"] == "https://sama.test/connections/go?connection=c&t=x"
    assert found["title"] == "Gmail"
