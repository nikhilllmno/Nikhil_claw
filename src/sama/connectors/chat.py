"""Connect from the chat (docs/architecture.md, Section 10.3.4, M8.1).

``connection_request`` starts the connection and gives the sign-in link of the provider at once.
Its answer ends with one connect card: a line ``<sama-connect>{...}</sama-connect>`` with the
connector, the title, the link, and the connection id. The web app shows the card as a
**Connect** button, and the Telegram and Slack gateways show it as a button under the reply.
The model reads the same line, so the answer stays the same for both runtimes.

The sign-in comes back to a page of the control plane (``/connections/return`` for Composio and
``/connections/oauth/return`` for MCP OAuth), not to the web app. That page needs no sama
session, so a link that opens in a new tab or on a phone works. When the backend then sets
``connected``, sama writes a note into the session that asked, and a short run continues the
request of the user.
"""

from __future__ import annotations

import html
import json
import re
from collections.abc import Mapping
from typing import Any, Final

CARD_TAG: Final = "sama-connect"
CARD_PATTERN: Final = re.compile(rf"<{CARD_TAG}>(\{{.*?\}})</{CARD_TAG}>")
"""The connect card in a tool answer. The gateways and the web app read it."""
MAX_CARD_CHARS: Final = 900
"""A card fits in the head of the tool output preview (``output_preview`` keeps about the first
970 characters of a long answer), so the card goes first in the answer."""
MAX_LINK_IN_TEXT: Final = 300
"""A longer sign-in link is only in the card, so the answer stays short."""


def link_text(url: str, line: str = "") -> str:
    """The link for the prose of an answer: the link, or a pointer to the card ``line`` when the
    card has the link and the link is long."""
    found = cards(line)
    if len(url) <= MAX_LINK_IN_TEXT or not found or found[0].get("url") != url:
        return url
    return "the url in the sama-connect line above (give it to the user as a link)"


def card(
    *,
    connector: str,
    title: str,
    url: str | None,
    connection_id: str,
    state: str,
    scope: str,
) -> str:
    """The connect card line. ``url`` is ``None`` when no sign-in is open (connected)."""
    data = {
        "connector": connector,
        "title": title,
        "url": url,
        "connection_id": connection_id,
        "state": state,
        "scope": scope,
    }
    text = json.dumps(data, separators=(",", ":"), ensure_ascii=True)
    if len(text) > MAX_CARD_CHARS:
        data["url"] = None
        text = json.dumps(data, separators=(",", ":"), ensure_ascii=True)
    return f"<{CARD_TAG}>{text}</{CARD_TAG}>"


def cards(text: str) -> list[dict[str, Any]]:
    """The connect cards in a tool answer. A card without an ``https`` link has ``url: None``."""
    found: list[dict[str, Any]] = []
    for match in CARD_PATTERN.finditer(text or ""):
        try:
            data = json.loads(match.group(1))
        except ValueError:
            continue
        if not isinstance(data, dict) or not isinstance(data.get("title"), str):
            continue
        url = data.get("url")
        data["url"] = url if isinstance(url, str) and url.startswith("https://") else None
        found.append(data)
    return found


def strip_cards(text: str) -> str:
    """The text without the card lines (for a channel that shows the card as a button)."""
    return CARD_PATTERN.sub("", text).strip()


def return_page(title: str, heading: str, body: str, *, link: str | None = None) -> str:
    """A small HTML page for the end of a sign-in. It has no script and no outside resource."""
    button = (
        f'<p><a class="b" href="{html.escape(link, quote=True)}">Open sama</a></p>' if link else ""
    )
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<meta name='referrer' content='no-referrer'>"
        f"<title>{html.escape(title)}</title><style>"
        "body{font-family:system-ui,sans-serif;max-width:32rem;margin:15vh auto;padding:0 1.5rem;"
        "line-height:1.5;color:#1f2328;background:#fff}"
        "@media (prefers-color-scheme:dark){body{color:#e6e6e6;background:#161616}}"
        "h1{font-size:1.4rem}.b{display:inline-block;padding:.5rem 1rem;border-radius:.5rem;"
        "background:#2f7d5b;color:#fff;text-decoration:none}"
        f"</style></head><body><h1>{html.escape(heading)}</h1><p>{html.escape(body)}</p>"
        f"{button}</body></html>"
    )


def resume_text(title: str, scope: str) -> str:
    """The note that continues the request after a sign-in (a message of the session)."""
    return (
        f"(sama) {title} is connected now ({scope} connection). Continue the request that needed"
        f" {title}. Its tools are in tool_search. Do not ask the user to confirm the connection."
    )


def origin_of(config: Mapping[str, Any]) -> dict[str, str] | None:
    """The session that asked for a connection (``config.origin``), or ``None``."""
    origin = config.get("origin")
    if not isinstance(origin, Mapping):
        return None
    if not isinstance(origin.get("session_id"), str):
        return None
    return {k: str(v) for k, v in origin.items() if isinstance(v, str)}


def connection_escalation(name: str, arguments: Mapping[str, Any]) -> str | None:
    """The tool gate asks a person before ``connection_request`` makes a connection for the
    whole workspace (M8.1). A personal connection needs no approval: the user signs in on the
    page of the provider, which is the consent."""
    if name.removeprefix("mcp__sama__") == "connection_request" and (
        arguments.get("scope") == "workspace"
    ):
        return "a connection for the whole workspace: every member and shared session can use it"
    return None
