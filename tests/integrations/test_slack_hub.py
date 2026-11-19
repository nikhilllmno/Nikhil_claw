"""Tests of the request URLs of the Slack integrations (docs/architecture.md, Section 11.6)."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlencode

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import delete, func, update
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.db.models import ChannelIdentity, Integration
from sama.gateway.slack import PAIR_PROMPT
from sama.gateway.slack_client import signature
from sama.integrations.slack_hub import MAX_BODY_BYTES, SlackHub
from sama.vault.crypto import KEY_BYTES, VaultKey
from sama.vault.store import SecretName, Vault
from tests.integrations.support import SIGNING, SLACK_TOKEN, TEAM, FakeSlack, add_slack
from tests.orchestrator.conftest import Tenant

NOW = 1_700_000_000


def headers(body: bytes, secret: str = SIGNING, at: int = NOW) -> dict[str, str]:
    return {
        "X-Slack-Request-Timestamp": str(at),
        "X-Slack-Signature": signature(secret, str(at), body),
    }


def dm_event(event_id: str = "Ev1", team: str = TEAM) -> dict[str, Any]:
    return {
        "type": "event_callback",
        "team_id": team,
        "event_id": event_id,
        "event": {
            "type": "message",
            "channel_type": "im",
            "channel": "DMIA0001",
            "user": "UMIA0001",
            "text": "hi",
            "ts": "1.1",
        },
    }


@pytest.fixture
def slack() -> FakeSlack:
    return FakeSlack()


@pytest.fixture
def vault(engine: AsyncEngine) -> Vault:
    return Vault(engine, VaultKey(b"s" * KEY_BYTES))


@pytest.fixture
async def hub(
    engine: AsyncEngine, tenant: Tenant, slack: FakeSlack, vault: Vault
) -> AsyncIterator[SlackHub]:
    async with engine.begin() as connection:
        await connection.execute(delete(ChannelIdentity))
    await vault.put(tenant.org, SecretName.SLACK_BOT_TOKEN, SLACK_TOKEN)
    await vault.put(tenant.org, SecretName.SLACK_SIGNING_SECRET, SIGNING)
    await add_slack(engine, tenant)
    hub = SlackHub(engine, vault, slack_client=slack.client, clock=lambda: NOW)
    yield hub
    await hub.aclose()


@pytest.fixture
async def http(hub: SlackHub) -> AsyncIterator[httpx.AsyncClient]:
    app = FastAPI()
    app.include_router(hub.router())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gw.test") as client:
        yield client


async def drain(hub: SlackHub) -> None:
    for app in list(hub._bots.values()):
        await app.gateway.drain()


async def test_events_need_the_signature_of_the_app(
    http: httpx.AsyncClient, hub: SlackHub, tenant: Tenant, slack: FakeSlack
) -> None:
    url = f"/slack/{tenant.ids['slack']}/events"
    body = json.dumps(dm_event()).encode()
    assert (await http.post(url, content=body)).status_code == 401
    wrong = headers(body, secret="6" * 32)
    assert (await http.post(url, content=body, headers=wrong)).status_code == 401
    old = headers(body, at=NOW - 600)
    assert (await http.post(url, content=body, headers=old)).status_code == 401
    assert (await http.post(f"/slack/{uuid.uuid4()}/events", content=body)).status_code == 401
    assert (await http.post("/slack/nope/events", content=body)).status_code == 401
    assert slack.calls == []

    response = await http.post(url, content=body, headers=headers(body))
    assert response.status_code == 200
    # Slack sends an event again when the answer is slow: the repeat is dropped.
    assert (await http.post(url, content=body, headers=headers(body))).status_code == 200
    other_team = json.dumps(dm_event("Ev2", team="T0OTHER")).encode()
    assert (
        await http.post(url, content=other_team, headers=headers(other_team))
    ).status_code == 200
    await drain(hub)
    assert slack.texts() == [PAIR_PROMPT]

    challenge = json.dumps({"type": "url_verification", "challenge": "abc"}).encode()
    answer = await http.post(url, content=challenge, headers=headers(challenge))
    assert answer.json() == {"challenge": "abc"}
    broken = b"{"
    assert (await http.post(url, content=broken, headers=headers(broken))).status_code == 400
    big = b" " * (MAX_BODY_BYTES + 1)
    assert (await http.post(url, content=big, headers=headers(big))).status_code == 413


async def test_a_pending_app_answers_only_the_url_check(
    http: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            update(Integration)
            .where(Integration.id == tenant.ids["slack"])
            .values(status="pending", external_id=None, secret_hash=None)
        )
    base = f"/slack/{tenant.ids['slack']}"
    challenge = json.dumps({"type": "url_verification", "challenge": "c1"}).encode()
    assert (await http.post(f"{base}/events", content=challenge)).json() == {"challenge": "c1"}
    event = json.dumps(dm_event()).encode()
    assert (await http.post(f"{base}/events", content=event)).status_code == 401
    form = urlencode({"payload": "{}"}).encode()
    assert (await http.post(f"{base}/interactions", content=form)).status_code == 401
    assert (await http.post(f"{base}/commands", content=b"text=help")).status_code == 401


async def test_secrets_that_do_not_match_the_row_stop_the_app(
    http: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant, vault: Vault
) -> None:
    await vault.put(tenant.org, SecretName.SLACK_SIGNING_SECRET, "7" * 32)
    body = json.dumps(dm_event()).encode()
    url = f"/slack/{tenant.ids['slack']}/events"
    assert (await http.post(url, content=body, headers=headers(body))).status_code == 503
    await vault.put(tenant.org, SecretName.SLACK_SIGNING_SECRET, SIGNING)
    assert (await http.post(url, content=body, headers=headers(body))).status_code == 200


async def test_a_command_gets_a_private_answer(
    http: httpx.AsyncClient, hub: SlackHub, engine: AsyncEngine, tenant: Tenant
) -> None:
    url = f"/slack/{tenant.ids['slack']}/commands"
    fields = {"team_id": TEAM, "user_id": "UMIA0001", "channel_id": "DMIA0001", "text": "new"}
    body = urlencode(fields).encode()
    response = await http.post(url, content=body, headers=headers(body))
    assert response.json() == {"response_type": "ephemeral", "text": PAIR_PROMPT}
    other = urlencode({**fields, "team_id": "T0OTHER"}).encode()
    assert (await http.post(url, content=other, headers=headers(other))).status_code == 401
    assert (await http.post(url, content=body)).status_code == 401

    # A new row version builds a new gateway.
    first = hub._bots[tenant.ids["slack"]]
    async with engine.begin() as connection:
        await connection.execute(
            update(Integration)
            .where(Integration.id == tenant.ids["slack"])
            .values(updated_at=func.now() + func.make_interval(0, 0, 0, 0, 0, 0, 1))
        )
    assert (await http.post(url, content=body, headers=headers(body))).status_code == 200
    assert hub._bots[tenant.ids["slack"]] is not first


async def test_a_button_click_goes_to_the_gateway(
    http: httpx.AsyncClient, hub: SlackHub, tenant: Tenant, slack: FakeSlack
) -> None:
    url = f"/slack/{tenant.ids['slack']}/interactions"
    payload = {
        "type": "block_actions",
        "team": {"id": TEAM},
        "user": {"id": "UMIA0001"},
        "channel": {"id": "DMIA0001"},
        "message": {"ts": "1.1"},
        "actions": [{"value": f"ap:{uuid.uuid4().hex}:y"}],
    }
    body = urlencode({"payload": json.dumps(payload)}).encode()
    assert (await http.post(url, content=body, headers=headers(body))).status_code == 200
    stranger = urlencode({"payload": json.dumps({**payload, "team": {"id": "T0X"}})}).encode()
    assert (await http.post(url, content=stranger, headers=headers(stranger))).status_code == 200
    await drain(hub)
    assert slack.whispers("DMIA0001") == ["Link your Slack account to sama first."]


async def test_a_chunked_body_stops_at_the_limit(
    http: httpx.AsyncClient, tenant: Tenant, slack: FakeSlack
) -> None:
    """A body with no Content-Length is read in chunks and stops at the limit."""
    chunk = b" " * (256 * 1024)
    total = MAX_BODY_BYTES // len(chunk) + 8
    sent = 0

    async def chunks() -> AsyncIterator[bytes]:
        nonlocal sent
        for _ in range(total):
            sent += 1
            yield chunk

    url = f"/slack/{uuid.uuid4()}/events"
    response = await http.post(url, content=chunks())
    assert response.status_code == 413
    # The hub stops to read at the limit; it does not take the rest of the body.
    assert sent < total
    assert slack.calls == []


async def test_a_repeat_after_a_reconnect_is_dropped(
    http: httpx.AsyncClient, hub: SlackHub, engine: AsyncEngine, tenant: Tenant, slack: FakeSlack
) -> None:
    url = f"/slack/{tenant.ids['slack']}/events"
    body = json.dumps(dm_event("EvRe")).encode()
    assert (await http.post(url, content=body, headers=headers(body))).status_code == 200
    await drain(hub)
    first = hub._bots[tenant.ids["slack"]]
    # A reconnect with the same secrets changes the row version: the hub builds a new gateway.
    async with engine.begin() as connection:
        await connection.execute(
            update(Integration)
            .where(Integration.id == tenant.ids["slack"])
            .values(updated_at=func.now() + func.make_interval(0, 0, 0, 0, 0, 0, 1))
        )
    assert (await http.post(url, content=body, headers=headers(body))).status_code == 200
    assert hub._bots[tenant.ids["slack"]] is not first
    await drain(hub)
    assert slack.texts() == [PAIR_PROMPT]
