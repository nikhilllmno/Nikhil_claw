"""Tests of the WhatsApp gateway with a real database, a fake provider, and the fake runtime
(docs/architecture.md, Section 11.9)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.db.models import Attachment, AuditEntry, ChannelIdentity, Session, SessionBinding
from sama.gateway.direct import approval_code
from sama.gateway.sessions import BG_EMPTY, JOINED, FollowSettings
from sama.gateway.whatsapp import WhatsAppGateway
from sama.gateway.whatsapp_providers import Inbound, MediaRef
from sama.orchestrator.queue import RunQueue
from sama.runtime.contract import RunCompleted, TextDelta
from sama.runtime.fake import FakeScript
from tests.gateway.channel_support import MIA, NUMBER, FakeWhatsApp, add_channel, code_for
from tests.gateway.media_support import CSV, OGG, add_media, latest_run
from tests.media.support import PNG
from tests.orchestrator.conftest import (  # noqa: F401
    Tenant,
    engine,
    queue_database_url,
    tenant,
)
from tests.orchestrator.test_worker import Harness, approval_script, until

_ids = iter(range(1, 10_000))


def inbound(text: str, *, sender: str = MIA, media: tuple[MediaRef, ...] = ()) -> Inbound:
    return Inbound(f"wamid.{next(_ids)}", sender, text, media)


class FakeTranscriber:
    async def transcribe(self, audio: bytes, *, filename: str, mime_type: str) -> str:
        return "hi from a voice note"


@pytest.fixture
def provider() -> FakeWhatsApp:
    return FakeWhatsApp()


@pytest.fixture
async def gateway(
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
    provider: FakeWhatsApp,
) -> AsyncIterator[WhatsAppGateway]:
    async with engine.begin() as connection:
        await connection.execute(delete(ChannelIdentity))
    integration_id = await add_channel(
        engine, tenant, "whatsapp", NUMBER, settings={"provider": "meta"}
    )
    gateway = WhatsAppGateway(
        engine,
        provider,
        integration_id=integration_id,
        org_id=tenant.org,
        media_link=lambda media: f"https://sama.test/media/{media.id}",
        settings=FollowSettings(poll_s=0.02, paused_poll_s=0.02, edit_interval_s=0),
        transcriber=FakeTranscriber(),
    )
    yield gateway
    await gateway.aclose()


async def pair(gateway: WhatsAppGateway, engine: AsyncEngine, tenant: Tenant) -> None:  # noqa: F811
    code = await code_for(engine, tenant, "whatsapp")
    await gateway.handle(inbound(code))


async def bound(engine: AsyncEngine) -> Any:  # noqa: F811
    async with engine.connect() as connection:
        return (
            await connection.execute(
                select(Session)
                .join(SessionBinding, SessionBinding.session_id == Session.id)
                .where(SessionBinding.channel == "whatsapp")
                .order_by(Session.created_at.desc())
            )
        ).first()


async def test_an_unknown_number_gets_a_prompt_and_pairs_with_a_code(
    gateway: WhatsAppGateway,
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
    provider: FakeWhatsApp,
) -> None:
    await gateway.handle(inbound("hello"))
    assert "is not linked to sama" in provider.texts()[-1]
    code = await code_for(engine, tenant, "whatsapp")
    await gateway.handle(inbound(f"/pair {code.lower()}"))
    assert (
        provider.texts()[-1] == "This WhatsApp number is linked to sama. Send a message to start."
    )
    async with engine.connect() as connection:
        identity = (
            await connection.execute(
                select(ChannelIdentity).where(ChannelIdentity.external_id == MIA)
            )
        ).one()
        audit = await connection.scalar(
            select(AuditEntry.target_id).where(
                AuditEntry.org_id == tenant.org, AuditEntry.action == "channel_identity.linked"
            )
        )
    assert (identity.channel, identity.user_id) == ("whatsapp", tenant.ids["mia"])
    assert audit == f"whatsapp:{MIA}"
    # A code works once.
    await gateway.handle(inbound(code, sender="+15550009999"))
    assert "wrong, used, or expired" in provider.texts("+15550009999")[-1]


async def test_wrong_codes_lock_the_number(
    gateway: WhatsAppGateway,
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
    provider: FakeWhatsApp,
) -> None:
    for _ in range(5):
        await gateway.handle(inbound("/pair AAAABBBB"))
    code = await code_for(engine, tenant, "whatsapp")
    await gateway.handle(inbound(code))
    assert provider.texts()[-1] == "Too many wrong codes. Try again later."
    assert all(code not in text for text in provider.texts())


async def test_a_message_runs_in_a_private_session_and_the_reply_comes_once(
    gateway: WhatsAppGateway,
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
    provider: FakeWhatsApp,
) -> None:
    await pair(gateway, engine, tenant)
    harness = Harness(engine)
    harness.runtime.add_script(
        FakeScript(
            events=(TextDelta(text="hel"), TextDelta(text="lo"), RunCompleted(output="hello"))
        )
    )
    await gateway.handle(inbound("hi"))
    session = await bound(engine)
    assert (session.audience, session.owner_user_id) == ("private", tenant.ids["mia"])
    assert await harness.worker.run_once()
    await gateway.drain()
    assert provider.texts()[-1] == "hello"
    assert provider.texts().count("hello") == 1, "WhatsApp cannot edit: one message at the end"
    async with engine.connect() as connection:
        config = await connection.scalar(select(Session.run_config).where(Session.id == session.id))
    assert config is not None
    assert "Channel: WhatsApp." in config["environment"]

    # The next message uses the same session; /new starts another one.
    await gateway.handle(inbound("/new"))
    assert provider.texts()[-1] == "A new session started."
    assert (await bound(engine)).id != session.id


async def test_a_long_reply_is_cut_at_the_limit_of_the_provider(
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
    gateway: WhatsAppGateway,
    provider: FakeWhatsApp,
) -> None:
    provider.max_text = 100
    await pair(gateway, engine, tenant)
    harness = Harness(engine)
    words = " ".join(f"word{i}" for i in range(60))
    harness.runtime.add_script(
        FakeScript(events=(TextDelta(text=words), RunCompleted(output=words)))
    )
    await gateway.handle(inbound("talk"))
    assert await harness.worker.run_once()
    await gateway.drain()
    parts = provider.texts()[1:]
    assert len(parts) > 1
    assert all(len(part) <= 100 for part in parts)
    assert " ".join(p.strip() for p in parts) == words


async def test_an_approval_card_has_buttons_and_a_press_decides(
    gateway: WhatsAppGateway,
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
    provider: FakeWhatsApp,
) -> None:
    await pair(gateway, engine, tenant)
    harness = Harness(engine)
    harness.runtime.add_script(approval_script())
    await gateway.handle(inbound("clean up"))
    assert await harness.worker.run_once()

    async def carded() -> bool:
        return bool(provider.cards)

    await until(carded)
    text, buttons = provider.cards[0]
    assert "Bash" in text
    assert "deletes files" in text
    assert [b.title for b in buttons] == ["Approve", "Deny", "Always allow for me"]
    assert "let me " in provider.texts(), "the text before the card goes out first"
    code = buttons[0].payload.split()[1]
    assert buttons[0].payload == f"yes {code}"

    # A press of Approve sends its payload as a message.
    await gateway.handle(inbound(buttons[0].payload))
    assert provider.texts()[-1] == "Approved: Bash."
    assert await harness.worker.run_once()
    await gateway.drain()
    assert provider.texts()[-1] == "deleted"


async def test_a_card_without_buttons_asks_for_a_reply_code(
    gateway: WhatsAppGateway,
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
    provider: FakeWhatsApp,
) -> None:
    provider.buttons = False
    await pair(gateway, engine, tenant)
    harness = Harness(engine)
    harness.runtime.add_script(approval_script())
    await gateway.handle(inbound("clean up"))
    assert await harness.worker.run_once()

    async def carded() -> bool:
        return any("Reply yes" in text for text in provider.texts())

    await until(carded)
    card = next(text for text in provider.texts() if "Reply yes" in text)
    code = card.split("Reply yes ", 1)[1].split()[0]
    await gateway.handle(inbound("no 000000"))
    assert provider.texts()[-1] == "No open approval of this chat has the code 000000."
    await gateway.handle(inbound(f"No {code.lower()}."))
    assert provider.texts()[-1] == "Denied: Bash."


async def test_another_number_cannot_decide_an_approval(
    gateway: WhatsAppGateway,
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
    provider: FakeWhatsApp,
) -> None:
    await pair(gateway, engine, tenant)
    harness = Harness(engine)
    harness.runtime.add_script(approval_script())
    await gateway.handle(inbound("clean up"))
    assert await harness.worker.run_once()

    async def carded() -> bool:
        return bool(provider.cards)

    await until(carded)
    payload = provider.cards[0][1][0].payload
    # An unknown number only gets the pairing prompt.
    await gateway.handle(inbound(payload, sender="+15550002222"))
    assert "is not linked" in provider.texts("+15550002222")[-1]


async def test_an_unlinked_number_gets_no_more_output(
    gateway: WhatsAppGateway,
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
    provider: FakeWhatsApp,
) -> None:
    await pair(gateway, engine, tenant)
    harness = Harness(engine)
    harness.runtime.add_script(FakeScript(events=(RunCompleted(output="secret answer"),)))
    await gateway.handle(inbound("hi"))
    async with engine.begin() as connection:
        await connection.execute(delete(ChannelIdentity))
    assert await harness.worker.run_once()
    await gateway.drain()
    assert "secret answer" not in provider.texts()


async def test_an_image_is_stored_and_a_voice_note_is_transcribed(
    gateway: WhatsAppGateway,
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
    provider: FakeWhatsApp,
) -> None:
    await pair(gateway, engine, tenant)
    provider.files["img1"] = PNG
    provider.files["voice1"] = OGG
    harness = Harness(engine)
    harness.runtime.add_script(FakeScript(events=(RunCompleted(output="seen"),)))
    await gateway.handle(inbound("", media=(MediaRef("img1", "image/png", "photo"),)))
    async with engine.connect() as connection:
        names = (await connection.scalars(select(Attachment.name))).all()
    assert names == ["photo"]
    assert await harness.worker.run_once()
    await gateway.drain()

    harness.runtime.add_script(FakeScript(events=(RunCompleted(output="heard"),)))
    await gateway.handle(inbound("", media=(MediaRef("voice1", "audio/ogg; codecs=opus"),)))
    run = await latest_run(engine, tenant.org)
    async with engine.connect() as connection:
        from sama.db.models import Message

        text = await connection.scalar(
            select(Message.content).where(Message.run_id == run.id, Message.role == "user")
        )
    assert "hi from a voice note" in str(text)

    # Another file type is refused with a note.
    await gateway.handle(inbound("", media=(MediaRef("doc", "application/pdf"),)))
    assert provider.texts()[-1] == "sama reads only images and voice notes here. Send text."


async def test_generated_media_goes_out_through_the_link(
    gateway: WhatsAppGateway,
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
    provider: FakeWhatsApp,
) -> None:
    await pair(gateway, engine, tenant)
    harness = Harness(engine)
    harness.runtime.add_script(FakeScript(events=(RunCompleted(output="here it is"),)))
    await gateway.handle(inbound("draw a cat"))
    run = await latest_run(engine, tenant.org)
    media_id = await add_media(engine, run, "image", PNG)
    assert await harness.worker.run_once()
    await gateway.drain()
    assert provider.media[0][2] == f"https://sama.test/media/{media_id}"


async def test_a_pdf_goes_as_a_file_and_a_csv_gets_a_note(
    gateway: WhatsAppGateway,
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
    provider: FakeWhatsApp,
) -> None:
    await pair(gateway, engine, tenant)
    harness = Harness(engine)
    harness.runtime.add_script(FakeScript(events=(RunCompleted(output="both files"),)))
    await gateway.handle(inbound("make a pdf and a csv"))
    run = await latest_run(engine, tenant.org)
    pdf = await add_media(
        engine, run, "file", b"%PDF-1.7", name="a.pdf", mime_type="application/pdf"
    )
    await add_media(engine, run, "file", CSV, name="top5.csv")
    assert await harness.worker.run_once()
    await gateway.drain()
    assert [m[2] for m in provider.media] == [f"https://sama.test/media/{pdf}"]
    assert any("top5.csv is ready, but WhatsApp cannot send" in t for t in provider.texts())


async def test_a_repeated_message_is_dropped(
    gateway: WhatsAppGateway,
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
    provider: FakeWhatsApp,
) -> None:
    message = inbound("hello")
    gateway.accept(message)
    gateway.accept(message)
    await gateway.drain()
    assert len(provider.texts()) == 1


def test_the_approval_code_is_the_start_of_the_id() -> None:
    approval_id = uuid.UUID("ab12cd34-0000-4000-8000-000000000000")
    assert approval_code(approval_id) == "AB12CD"


async def test_a_long_card_without_buttons_keeps_its_reply_code(
    gateway: WhatsAppGateway,
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
    provider: FakeWhatsApp,
) -> None:
    provider.buttons = False
    provider.max_text = 120
    await pair(gateway, engine, tenant)
    harness = Harness(engine)
    harness.runtime.add_script(approval_script())
    await gateway.handle(inbound("clean up"))
    assert await harness.worker.run_once()

    async def carded() -> bool:
        return any("Reply yes" in text for text in provider.texts())

    await until(carded)
    first = next(text for text in provider.texts() if "Reply yes" in text)
    assert first.startswith("Reply yes ")
    assert all(len(text) <= 120 for text in provider.texts())


async def test_the_coding_commands_route_to_the_coding_agent(
    gateway: WhatsAppGateway,
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
    provider: FakeWhatsApp,
) -> None:
    """``/claude`` and ``/codex`` (Section 15.4) work in a direct channel too."""
    from sama.db.models import Message, Run

    await pair(gateway, engine, tenant)
    await gateway.handle(inbound("/codex"))
    assert "Codex" in provider.texts()[-1]
    session = await bound(engine)
    async with engine.connect() as connection:
        mode = await connection.scalar(select(Session.coding_agent).where(Session.id == session.id))
    assert mode == "codex"
    await gateway.handle(inbound("/sama"))
    assert provider.texts()[-1] == "sama answers the messages of this session again."
    await gateway.handle(inbound("/claude octo/demo fix the typo"))
    async with engine.connect() as connection:
        run = (
            await connection.execute(
                select(Run.coding_agent, Message.content)
                .join(Message, Message.run_id == Run.id)
                .where(Run.session_id == session.id)
            )
        ).one()
    assert run.coding_agent == "claude_code"
    assert run.content["text"] == "octo/demo fix the typo"


async def test_a_message_joins_the_running_run_and_bg_and_stop_work(
    gateway: WhatsAppGateway,
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
    provider: FakeWhatsApp,
) -> None:
    """M7.1: the busy modes in WhatsApp (and email, which shares the direct gateway)."""
    await pair(gateway, engine, tenant)
    queue = RunQueue(engine)
    await gateway.handle(inbound("write the long report"))
    main = await queue.claim("test-owner")
    assert main is not None
    await gateway.handle(inbound("also add a chart"))
    assert provider.texts()[-1] == JOINED
    await gateway.handle(inbound("/bg"))
    assert provider.texts()[-1] == BG_EMPTY.format(command="/bg")
    await gateway.handle(inbound("/bg check the weather"))
    side = await queue.claim("test-owner")
    assert side is not None
    assert side.side
    assert "/stop 2 stops only this run" in provider.texts()[-1]
    await gateway.handle(inbound("/stop 2"))
    assert provider.texts()[-1] == "Stopped run 2: check the weather"
    await gateway.handle(inbound("/stop"))
    assert provider.texts()[-1] == "Stopped 2 runs."
