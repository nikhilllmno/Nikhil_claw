"""Images in Telegram (docs/architecture.md, Section 11.7)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.db.models import Attachment, Message, Run
from sama.gateway.telegram import TelegramGateway
from sama.media.images import (
    MAX_IMAGE_BYTES,
    MAX_IMAGES_PER_MESSAGE,
    TOO_LARGE,
    TOO_MANY,
    UNREADABLE,
    UNSUPPORTED,
)
from tests.gateway.test_telegram import (  # noqa: F401
    DM,
    GROUP,
    bound_session,
    clean_links,
    code_for,
    gateway,
    message,
    no_followers,
    pair,
    telegram,
    transcriber,
)
from tests.integrations.support import FakeTelegram
from tests.media.support import JPEG, NOT_AN_IMAGE, PNG
from tests.orchestrator.conftest import (  # noqa: F401
    Tenant,
    engine,
    queue_database_url,
    tenant,
)


def photo(*sizes: tuple[str, int, int, int]) -> list[dict[str, Any]]:
    """Telegram sizes of one photo: ``(file_id, width, height, file_size)``."""
    return [
        {"file_id": file_id, "width": width, "height": height, "file_size": size}
        for file_id, width, height, size in sizes
    ]


async def stored(engine: AsyncEngine) -> list[Any]:  # noqa: F811
    async with engine.connect() as connection:
        rows = await connection.execute(
            select(
                Attachment.id,
                Attachment.name,
                Attachment.mime_type,
                Attachment.size_bytes,
                Attachment.message_id,
                Attachment.session_id,
            ).order_by(Attachment.created_at)
        )
        return list(rows)


async def user_text(engine: AsyncEngine, message_id: Any) -> str:  # noqa: F811
    async with engine.connect() as connection:
        return str(
            (
                await connection.execute(
                    select(Message.content["text"].as_string()).where(Message.id == message_id)
                )
            ).scalar_one()
        )


async def run_count(engine: AsyncEngine) -> int:  # noqa: F811
    async with engine.connect() as connection:
        return int((await connection.execute(select(func.count()).select_from(Run))).scalar_one())


async def test_a_photo_with_a_caption_is_stored_with_the_message(
    gateway: TelegramGateway,  # noqa: F811
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
    telegram: FakeTelegram,  # noqa: F811
) -> None:
    await pair(gateway, engine, tenant)
    no_followers(gateway)
    telegram.files.update({"small": b"x", "large": JPEG})
    sizes = photo(("small", 90, 90, 1), ("large", 1280, 960, len(JPEG)))
    await gateway.handle_update(message(None, photo=sizes, caption="what is this?"))
    (row,) = await stored(engine)
    assert (row.name, row.mime_type, row.size_bytes) == ("photo.jpg", "image/jpeg", len(JPEG))
    session = await bound_session(engine, DM)
    assert row.session_id == session.id
    text = await user_text(engine, row.message_id)
    assert text.startswith("what is this?\n[image attached: ")
    assert str(row.id) in text
    assert telegram.method_calls("getFile")[-1]["file_id"] == "large"


async def test_a_photo_without_text_is_a_message(
    gateway: TelegramGateway,  # noqa: F811
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
    telegram: FakeTelegram,  # noqa: F811
) -> None:
    await pair(gateway, engine, tenant)
    no_followers(gateway)
    telegram.files["p"] = JPEG
    await gateway.handle_update(message(None, photo=photo(("p", 100, 100, len(JPEG)))))
    (row,) = await stored(engine)
    assert (await user_text(engine, row.message_id)).startswith("[image attached: ")
    assert await run_count(engine) == 1


async def test_an_image_document_keeps_its_type_and_name(
    gateway: TelegramGateway,  # noqa: F811
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
    telegram: FakeTelegram,  # noqa: F811
) -> None:
    await pair(gateway, engine, tenant)
    no_followers(gateway)
    telegram.files["d"] = PNG
    document = {"file_id": "d", "mime_type": "image/png", "file_name": "chart.png"}
    await gateway.handle_update(message(None, document=document))
    (row,) = await stored(engine)
    assert (row.name, row.mime_type) == ("chart.png", "image/png")


async def test_the_best_photo_size_that_fits_is_taken(
    gateway: TelegramGateway,  # noqa: F811
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
    telegram: FakeTelegram,  # noqa: F811
) -> None:
    await pair(gateway, engine, tenant)
    no_followers(gateway)
    telegram.files["fits"] = JPEG
    sizes = photo(("fits", 800, 800, len(JPEG)), ("huge", 5000, 5000, MAX_IMAGE_BYTES + 1))
    await gateway.handle_update(message(None, photo=sizes))
    assert len(await stored(engine)) == 1
    assert telegram.method_calls("getFile")[-1]["file_id"] == "fits"


async def test_a_document_that_is_not_an_image_is_ignored(
    gateway: TelegramGateway,  # noqa: F811
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
    telegram: FakeTelegram,  # noqa: F811
) -> None:
    await pair(gateway, engine, tenant)
    sent = len(telegram.texts(DM))
    document = {"file_id": "d", "mime_type": "application/pdf", "file_name": "a.pdf"}
    await gateway.handle_update(message(None, document=document))
    assert await stored(engine) == []
    assert await run_count(engine) == 0
    assert len(telegram.texts(DM)) == sent


async def test_an_image_that_is_too_large_is_refused(
    gateway: TelegramGateway,  # noqa: F811
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
    telegram: FakeTelegram,  # noqa: F811
) -> None:
    await pair(gateway, engine, tenant)
    document = {
        "file_id": "big",
        "mime_type": "image/png",
        "file_name": "big.png",
        "file_size": MAX_IMAGE_BYTES + 1,
    }
    await gateway.handle_update(message(None, document=document, caption="look"))
    assert telegram.texts(DM)[-1] == TOO_LARGE
    # The size that Telegram states in getFile counts too.
    telegram.files["big2"] = PNG + b"\x00" * MAX_IMAGE_BYTES
    document = {"file_id": "big2", "mime_type": "image/png", "file_name": "big.png"}
    await gateway.handle_update(message(None, document=document))
    assert telegram.texts(DM)[-1] == TOO_LARGE
    assert await stored(engine) == []
    assert await run_count(engine) == 0


async def test_another_image_type_is_refused(
    gateway: TelegramGateway,  # noqa: F811
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
    telegram: FakeTelegram,  # noqa: F811
) -> None:
    await pair(gateway, engine, tenant)
    document = {"file_id": "h", "mime_type": "image/heic", "file_name": "a.heic"}
    await gateway.handle_update(message(None, document=document))
    assert telegram.texts(DM)[-1] == UNSUPPORTED
    assert telegram.method_calls("getFile") == []
    assert await run_count(engine) == 0


async def test_bytes_that_do_not_match_the_type_are_refused(
    gateway: TelegramGateway,  # noqa: F811
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
    telegram: FakeTelegram,  # noqa: F811
) -> None:
    await pair(gateway, engine, tenant)
    telegram.files["fake"] = NOT_AN_IMAGE
    document = {"file_id": "fake", "mime_type": "image/png", "file_name": "a.png"}
    await gateway.handle_update(message(None, document=document))
    assert telegram.texts(DM)[-1] == UNREADABLE
    assert await stored(engine) == []
    assert await run_count(engine) == 0


async def test_a_failed_download_is_refused(
    gateway: TelegramGateway,  # noqa: F811
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
    telegram: FakeTelegram,  # noqa: F811
) -> None:
    await pair(gateway, engine, tenant)
    telegram.failing.add("getFile")
    await gateway.handle_update(message(None, photo=photo(("p", 10, 10, 5))))
    assert telegram.texts(DM)[-1] == UNREADABLE
    assert await run_count(engine) == 0


async def test_too_many_images_are_refused(
    gateway: TelegramGateway,  # noqa: F811
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
    telegram: FakeTelegram,  # noqa: F811
) -> None:
    from sama.gateway import telegram as module

    await pair(gateway, engine, tenant)
    refs = [
        module._ImageRef(f"f{n}", "image/png", "a.png", 1)
        for n in range(MAX_IMAGES_PER_MESSAGE + 1)
    ]
    assert await gateway._download_images(DM, 1, refs) is None
    assert telegram.texts(DM)[-1] == TOO_MANY
    assert await stored(engine) == []


async def test_a_group_image_needs_a_mention_in_the_caption(
    gateway: TelegramGateway,  # noqa: F811
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
    telegram: FakeTelegram,  # noqa: F811
) -> None:
    await pair(gateway, engine, tenant)
    telegram.files["p"] = JPEG
    sizes = photo(("p", 100, 100, len(JPEG)))
    await gateway.handle_update(message(None, chat=GROUP, photo=sizes))
    assert telegram.method_calls("getFile") == []
    assert await run_count(engine) == 0
