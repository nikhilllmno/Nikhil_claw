"""The Telegram gateway sends the images and voice notes of a run as a photo and a voice
message (docs/architecture.md, Section 11.8; roadmap M4.9)."""

from __future__ import annotations

import json

from sqlalchemy.ext.asyncio import AsyncEngine

from sama.gateway.telegram import TelegramGateway
from sama.media.generate import GeneratedMediaStore
from sama.runtime.contract import RunCompleted
from sama.runtime.fake import FakeScript
from tests.gateway.media_support import OGG, PNG, add_media, latest_run
from tests.gateway.test_telegram import (  # noqa: F401
    DM,
    gateway,
    message,
    pair,
    telegram,
    transcriber,
)
from tests.integrations.support import FakeTelegram
from tests.orchestrator.conftest import (  # noqa: F401
    Tenant,
    engine,
    queue_database_url,
    tenant,
)
from tests.orchestrator.test_worker import Harness


async def test_telegram_sends_a_photo_and_a_voice_note_after_the_reply(
    gateway: TelegramGateway,  # noqa: F811
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
    telegram: FakeTelegram,  # noqa: F811
) -> None:
    await pair(gateway, engine, tenant)
    harness = Harness(engine)
    harness.runtime.add_script(FakeScript(events=(RunCompleted(output="here you go"),)))
    update = message("draw a fox and say hi")
    await gateway.handle_update(update)
    run = await latest_run(engine, tenant.org)
    await add_media(engine, run, "image", PNG)
    await add_media(engine, run, "audio", OGG)
    assert await harness.worker.run_once()
    await gateway.drain()
    assert telegram.texts(DM)[-1] == "here you go"
    assert telegram.media == [
        ("sendPhoto", DM, PNG),
        ("sendVoice", DM, OGG),
    ]
    [photo] = [body for method, body in telegram.calls if method == "sendPhoto"]
    assert json.loads(photo["reply_parameters"])["message_id"] == update["message"]["message_id"]
    # Each file goes once: they are marked sent.
    assert await GeneratedMediaStore(engine).pending(tenant.org, run.id) == []


async def test_telegram_keeps_a_file_pending_when_the_send_fails(
    gateway: TelegramGateway,  # noqa: F811
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
    telegram: FakeTelegram,  # noqa: F811
) -> None:
    await pair(gateway, engine, tenant)
    harness = Harness(engine)
    harness.runtime.add_script(FakeScript(events=(RunCompleted(output="done"),)))
    telegram.failing.add("sendPhoto")
    await gateway.handle_update(message("draw"))
    run = await latest_run(engine, tenant.org)
    await add_media(engine, run, "image", PNG)
    assert await harness.worker.run_once()
    await gateway.drain()
    assert telegram.texts(DM)[-1] == "done"
    assert len(await GeneratedMediaStore(engine).pending(tenant.org, run.id)) == 1
