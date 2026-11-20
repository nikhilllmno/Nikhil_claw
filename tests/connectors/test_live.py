"""A live check of the Composio API (Section 10.3). Run only with ``SAMA_LIVE_TESTS=1`` and
``COMPOSIO_API_KEY``.

It finds (or makes) the Composio-managed auth config of Gmail, makes a sign-in link for a test
entity, reads the state of the new account, and deletes the account. Nobody signs in, so the
account never gets a credential.
"""

from __future__ import annotations

import os
import uuid

import pytest
from pydantic import SecretStr

from sama.connectors.composio import PENDING_STATUSES, ComposioClient, ComposioError

LIVE = os.environ.get("SAMA_LIVE_TESTS") == "1"

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not LIVE, reason="set SAMA_LIVE_TESTS=1"),
    pytest.mark.skipif(not os.environ.get("COMPOSIO_API_KEY"), reason="set COMPOSIO_API_KEY"),
]


async def test_a_composio_link_starts_and_is_deleted() -> None:
    key = SecretStr(os.environ["COMPOSIO_API_KEY"])
    client = ComposioClient()
    try:
        config = await client.auth_config(key, "gmail")
        link = await client.link(
            key,
            auth_config_id=config,
            user_id=f"sama-live-test-{uuid.uuid4()}",
            callback_url="https://sama-staging.fly.dev/connections",
        )
        assert link.redirect_url.startswith("https://")
        assert await client.status(key, link.account_id) in PENDING_STATUSES
        await client.delete(key, link.account_id)
        with pytest.raises(ComposioError):
            await client.status(key, "ca_does_not_exist")
    finally:
        await client.aclose()
