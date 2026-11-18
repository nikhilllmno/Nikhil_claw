"""Live checks of the web tools. Run only with ``SAMA_LIVE_TESTS=1``.

- ``web_extract`` fetches a real https page. This checks that TLS works when the client
  connects to the checked IP address with the original server name.
- ``web_search`` calls Brave. It needs ``BRAVE_API_KEY``.
"""

from __future__ import annotations

import os

import pytest
from pydantic import SecretStr

from sama.tools.outbound import OutboundClient
from sama.tools.web import BraveSearch, extract

LIVE = os.environ.get("SAMA_LIVE_TESTS") == "1"

pytestmark = [pytest.mark.live, pytest.mark.skipif(not LIVE, reason="set SAMA_LIVE_TESTS=1")]


async def test_extract_a_real_page() -> None:
    client = OutboundClient()
    try:
        text = await extract(client, ["https://example.com/"])
    finally:
        await client.aclose()
    assert text.startswith('<untrusted_content source="web_extract" url="https://example.com/">')
    assert "Example Domain" in text


@pytest.mark.skipif(not os.environ.get("BRAVE_API_KEY"), reason="set BRAVE_API_KEY")
async def test_brave_search() -> None:
    search = BraveSearch(SecretStr(os.environ["BRAVE_API_KEY"]))
    try:
        hits = await search.search("Model Context Protocol", 3)
    finally:
        await search.aclose()
    assert 1 <= len(hits) <= 3
    assert all(hit.url.startswith("http") for hit in hits)
