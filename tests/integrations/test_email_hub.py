"""Tests of the email channel through its hub: the signed parse webhook, the gateway, and the
approval links (docs/architecture.md, Section 11.9)."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import FastAPI
from pydantic import SecretStr
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.db.models import Approval, Attachment, ChannelIdentity, Run, Session, SessionBinding
from sama.gateway.sessions import FollowSettings
from sama.integrations.email_hub import BAD_LINK, EmailHub
from sama.integrations.service import secret_hash
from sama.runtime.contract import RunCompleted
from sama.runtime.fake import FakeScript
from sama.vault.crypto import KEY_BYTES, VaultKey
from sama.vault.store import SecretName, Vault
from tests.gateway.channel_support import FakeSendGrid, add_channel, code_for
from tests.gateway.mail_support import CONTENT_TYPE, form, key_pair, parse_fields, sign
from tests.media.support import PNG
from tests.orchestrator.conftest import Tenant
from tests.orchestrator.test_worker import Harness, approval_script, until

NOW = 1_800_000_000
ADDRESS = "sama@mail.sama.test"
API_KEY = "SG." + "a" * 22 + "." + "b" * 43
MIA = "mia@example.com"


@pytest.fixture
def sendgrid() -> FakeSendGrid:
    return FakeSendGrid()


@pytest.fixture
def keys() -> tuple[ec.EllipticCurvePrivateKey, str]:
    return key_pair()


@pytest.fixture
async def hub(
    engine: AsyncEngine,
    tenant: Tenant,
    sendgrid: FakeSendGrid,
    keys: tuple[ec.EllipticCurvePrivateKey, str],
) -> AsyncIterator[EmailHub]:
    _ENGINE[:] = [engine]
    async with engine.begin() as connection:
        await connection.execute(delete(ChannelIdentity))
    vault = Vault(engine, VaultKey(b"e" * KEY_BYTES))
    await vault.put(tenant.org, SecretName.SENDGRID_API_KEY, API_KEY)
    await vault.put(tenant.org, SecretName.EMAIL_LINK_SECRET, "link-secret")
    await add_channel(
        engine,
        tenant,
        "email",
        ADDRESS,
        settings={"public_key": keys[1], "sender_name": "sama"},
        secret_hash=secret_hash(API_KEY),
    )

    def client(_: SecretStr) -> Any:
        return sendgrid

    hub = EmailHub(
        engine,
        vault,
        public_url="https://sama.test",
        settings=FollowSettings(poll_s=0.02, paused_poll_s=0.02, edit_interval_s=0),
        sendgrid_client=client,
        clock=lambda: NOW,
    )
    yield hub
    await hub.aclose()


@pytest.fixture
async def http(hub: EmailHub) -> AsyncIterator[httpx.AsyncClient]:
    app = FastAPI()
    app.include_router(hub.router())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://sama.test") as client:
        yield client


async def post_mail(
    http: httpx.AsyncClient,
    tenant: Tenant,
    keys: tuple[ec.EllipticCurvePrivateKey, str],
    hub: EmailHub,
    *,
    files: dict[str, tuple[str, str, bytes]] | None = None,
    runs: bool = False,
    **fields: Any,
) -> httpx.Response:
    """Posts one email. With ``runs`` it waits until the email queued a run, and does not wait
    for the follower of the run (the worker of the test runs it later)."""
    before = await run_count(tenant)
    body = form(parse_fields(**fields), files)
    answer = await http.post(
        f"/email/{tenant.ids['email']}/inbound",
        content=body,
        headers={"Content-Type": CONTENT_TYPE, **sign(keys[0], NOW, body)},
    )
    if runs:

        async def queued() -> bool:
            return await run_count(tenant) > before

        await until(queued)
    else:
        await hub.drain()
    return answer


_ENGINE: list[AsyncEngine] = []


async def run_count(tenant: Tenant) -> int:
    async with _ENGINE[0].connect() as connection:
        return int(
            await connection.scalar(
                select(func.count()).select_from(Run).where(Run.org_id == tenant.org)
            )
            or 0
        )


async def pair(
    http: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant, keys: Any, hub: EmailHub
) -> None:
    code = await code_for(engine, tenant, "email")
    await post_mail(http, tenant, keys, hub, subject=code, text="", message_id="<pair@example.com>")


async def test_an_unsigned_request_is_refused(
    http: httpx.AsyncClient, tenant: Tenant, keys: Any, sendgrid: FakeSendGrid
) -> None:
    body = form(parse_fields())
    answer = await http.post(
        f"/email/{tenant.ids['email']}/inbound",
        content=body,
        headers={"Content-Type": CONTENT_TYPE},
    )
    assert answer.status_code == 401
    _, _other = key_pair()
    wrong = await http.post(
        f"/email/{tenant.ids['email']}/inbound",
        content=body,
        headers={"Content-Type": CONTENT_TYPE, **sign(key_pair()[0], NOW, body)},
    )
    assert wrong.status_code == 401
    unknown = await http.post(
        "/email/00000000-0000-4000-8000-000000000000/inbound",
        content=body,
        headers={"Content-Type": CONTENT_TYPE, **sign(keys[0], NOW, body)},
    )
    assert unknown.status_code == 401
    assert sendgrid.mails == []


async def test_an_unknown_sender_gets_one_prompt_and_pairs_with_the_subject(
    http: httpx.AsyncClient,
    engine: AsyncEngine,
    tenant: Tenant,
    keys: Any,
    hub: EmailHub,
    sendgrid: FakeSendGrid,
) -> None:
    assert (await post_mail(http, tenant, keys, hub)).status_code == 200
    await post_mail(http, tenant, keys, hub, message_id="<m2@example.com>")
    assert len(sendgrid.texts(MIA)) == 1, "the prompt goes once an hour"
    assert "is not linked to sama" in sendgrid.texts(MIA)[0]
    await pair(http, engine, tenant, keys, hub)
    assert (
        sendgrid.texts(MIA)[-1] == "This email address is linked to sama. Send a message to start."
    )
    mail = sendgrid.mails[-1]
    assert (mail.sender, mail.to) == (ADDRESS, MIA)
    assert mail.headers["In-Reply-To"] == "<pair@example.com>"


async def test_a_mail_without_dkim_or_spf_is_dropped(
    http: httpx.AsyncClient, tenant: Tenant, keys: Any, hub: EmailHub, sendgrid: FakeSendGrid
) -> None:
    await post_mail(http, tenant, keys, hub, dkim="{@example.com : fail}", spf="softfail")
    await post_mail(http, tenant, keys, hub, to="other@mail.sama.test", message_id="<o@x>")
    assert sendgrid.mails == []


async def test_a_thread_is_a_session_and_the_reply_stays_in_it(
    http: httpx.AsyncClient,
    engine: AsyncEngine,
    tenant: Tenant,
    keys: Any,
    hub: EmailHub,
    sendgrid: FakeSendGrid,
) -> None:
    await pair(http, engine, tenant, keys, hub)
    harness = Harness(engine)
    harness.runtime.add_script(FakeScript(events=(RunCompleted(output="first answer"),)))
    await post_mail(http, tenant, keys, hub, subject="Plan", text="make a plan", runs=True)
    assert await harness.worker.run_once()
    await hub.drain()
    reply = sendgrid.mails[-1]
    assert reply.text == "first answer"
    assert reply.subject == "Re: Plan"
    token = re.fullmatch(r"sama\+t([0-9a-f]{16})@mail\.sama\.test", str(reply.reply_to))
    assert token is not None
    assert reply.headers["Message-ID"].startswith(f"<sama.{token.group(1)}.")
    async with engine.connect() as connection:
        rows = (
            await connection.execute(
                select(SessionBinding.session_id, SessionBinding.external_key).where(
                    SessionBinding.channel == "email"
                )
            )
        ).all()
        session = (await connection.execute(select(Session).where(Session.id == rows[0][0]))).one()
    assert rows[0][1].endswith(f":thread:{MIA}:{token.group(1)}")
    assert (session.audience, session.owner_user_id) == ("private", tenant.ids["mia"])
    assert "Channel: email." in session.run_config["environment"]

    # A reply in the thread goes on in the same session; the quote is cut.
    harness.runtime.add_script(FakeScript(events=(RunCompleted(output="second"),)))
    await post_mail(
        http,
        tenant,
        keys,
        hub,
        subject="Re: Plan",
        text="more please\n\nOn Mon, sama wrote:\n> first answer",
        message_id="<m3@example.com>",
        in_reply_to=reply.headers["Message-ID"],
        runs=True,
    )
    assert await harness.worker.run_once()
    await hub.drain()
    assert sendgrid.mails[-1].text == "second"
    async with engine.connect() as connection:
        count = len(
            (
                await connection.execute(
                    select(SessionBinding.id).where(SessionBinding.channel == "email")
                )
            ).all()
        )
    assert count == 1


async def test_an_approval_link_shows_a_page_and_a_post_decides(
    http: httpx.AsyncClient,
    engine: AsyncEngine,
    tenant: Tenant,
    keys: Any,
    hub: EmailHub,
    sendgrid: FakeSendGrid,
) -> None:
    await pair(http, engine, tenant, keys, hub)
    harness = Harness(engine)
    harness.runtime.add_script(approval_script())
    await post_mail(http, tenant, keys, hub, subject="Clean", text="clean up", runs=True)
    assert await harness.worker.run_once()

    async def carded() -> bool:
        return any("Approve: https://" in text for text in sendgrid.texts(MIA))

    await until(carded)
    card = next(text for text in sendgrid.texts(MIA) if "Approve: https://" in text)
    approve = re.search(r"Approve: https://sama\.test(\S+)", card)
    assert approve is not None
    page = await http.get(approve.group(1))
    assert page.status_code == 200
    assert "Bash" in page.text
    assert "<form" in page.text
    async with engine.connect() as connection:
        assert await connection.scalar(select(Approval.status)) == "pending", "GET changes nothing"
    decided = await http.post(approve.group(1))
    assert "Approved: Bash." in decided.text
    again = await http.post(approve.group(1))
    assert "the approval is already approved" in again.text
    assert await harness.worker.run_once()
    await hub.drain()
    assert sendgrid.texts(MIA)[-1] == "deleted"

    # A link of an unlinked user no longer works.
    async with engine.begin() as connection:
        await connection.execute(delete(ChannelIdentity))
    assert BAD_LINK in (await http.get(approve.group(1))).text


async def test_an_image_attachment_is_stored(
    http: httpx.AsyncClient,
    engine: AsyncEngine,
    tenant: Tenant,
    keys: Any,
    hub: EmailHub,
) -> None:
    await pair(http, engine, tenant, keys, hub)
    await post_mail(
        http,
        tenant,
        keys,
        hub,
        text="what is this?",
        message_id="<img@example.com>",
        files={"attachment1": ("cat.png", "image/png", PNG)},
        runs=True,
    )
    async with engine.connect() as connection:
        names = (await connection.scalars(select(Attachment.name))).all()
    assert names == ["cat.png"]
