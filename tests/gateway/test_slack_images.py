"""Images in Slack (docs/architecture.md, Section 11.7)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.db.models import Attachment, Message, Run
from sama.gateway.slack import SlackGateway
from sama.media.images import MAX_IMAGE_BYTES, TOO_LARGE, UNREADABLE, UNSUPPORTED
from tests.gateway.test_slack import (  # noqa: F401
    CHANNEL,
    DM,
    MIA,
    bound,
    dm,
    gateway,
    link_channel,
    mention,
    no_followers,
    pair,
    slack,
)
from tests.integrations.support import SLACK_TOKEN, FakeSlack
from tests.media.support import NOT_AN_IMAGE, PNG, WEBP
from tests.orchestrator.conftest import (  # noqa: F401
    Tenant,
    engine,
    queue_database_url,
    tenant,
)

URL = "https://files.slack.com/files-pri/T0SAMA-F1/download/chart.png"


def file(
    url: str = URL, *, mimetype: str = "image/png", name: str = "chart.png", **extra: Any
) -> dict[str, Any]:
    return {
        "id": "F1",
        "name": name,
        "mimetype": mimetype,
        "url_private_download": url,
        "url_private": url,
        **extra,
    }


async def stored(engine: AsyncEngine) -> list[Any]:  # noqa: F811
    async with engine.connect() as connection:
        rows = await connection.execute(
            select(
                Attachment.id, Attachment.name, Attachment.mime_type, Attachment.message_id
            ).order_by(Attachment.created_at)
        )
        return list(rows)


async def run_count(engine: AsyncEngine) -> int:  # noqa: F811
    async with engine.connect() as connection:
        return int((await connection.execute(select(func.count()).select_from(Run))).scalar_one())


async def text_of(engine: AsyncEngine, message_id: Any) -> str:  # noqa: F811
    async with engine.connect() as connection:
        return str(
            (
                await connection.execute(
                    select(Message.content["text"].as_string()).where(Message.id == message_id)
                )
            ).scalar_one()
        )


async def test_a_file_in_a_direct_message_is_stored_with_the_message(
    gateway: SlackGateway,  # noqa: F811
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
    slack: FakeSlack,  # noqa: F811
) -> None:
    await pair(gateway, engine, tenant)
    slack.files[URL] = PNG
    event = dm("what is in it?", subtype="file_share", files=[file()])
    await gateway.handle_event(event)
    (row,) = await stored(engine)
    assert (row.name, row.mime_type) == ("chart.png", "image/png")
    text = await text_of(engine, row.message_id)
    assert text.startswith("what is in it?\n[image attached: ")
    assert str(row.id) in text
    # The bot token goes with the download.
    assert slack.downloads == [(URL, SLACK_TOKEN)]


async def test_a_file_without_text_is_a_message(
    gateway: SlackGateway,  # noqa: F811
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
    slack: FakeSlack,  # noqa: F811
) -> None:
    await pair(gateway, engine, tenant)
    slack.files[URL] = WEBP
    await gateway.handle_event(dm("", files=[file(mimetype="image/webp", name="a.webp")]))
    (row,) = await stored(engine)
    assert row.mime_type == "image/webp"
    assert await run_count(engine) == 1


async def test_a_file_in_a_channel_mention_is_stored_in_the_thread_session(
    gateway: SlackGateway,  # noqa: F811
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
    slack: FakeSlack,  # noqa: F811
) -> None:
    await pair(gateway, engine, tenant)
    await link_channel(gateway, engine, tenant)
    slack.files[URL] = PNG
    event = mention("look at this", thread_ts="1690000000.000001")
    event["files"] = [file()]
    await gateway.handle_event(event)
    (row,) = await stored(engine)
    assert "look at this" in await text_of(engine, row.message_id)
    assert len(await bound(engine, f":thread:{CHANNEL}:1690000000.000001")) == 1


async def test_files_that_are_not_images_are_ignored(
    gateway: SlackGateway,  # noqa: F811
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
    slack: FakeSlack,  # noqa: F811
) -> None:
    await pair(gateway, engine, tenant)
    await gateway.handle_event(dm("", files=[file(mimetype="application/pdf", name="a.pdf")]))
    assert slack.downloads == []
    assert await run_count(engine) == 0


async def test_another_image_type_is_refused(
    gateway: SlackGateway,  # noqa: F811
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
    slack: FakeSlack,  # noqa: F811
) -> None:
    await pair(gateway, engine, tenant)
    await gateway.handle_event(dm("see", files=[file(mimetype="image/svg+xml", name="a.svg")]))
    assert slack.texts(DM)[-1] == UNSUPPORTED
    assert slack.downloads == []
    assert await run_count(engine) == 0


async def test_an_image_that_is_too_large_is_refused(
    gateway: SlackGateway,  # noqa: F811
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
    slack: FakeSlack,  # noqa: F811
) -> None:
    await pair(gateway, engine, tenant)
    await gateway.handle_event(dm("see", files=[file(size=MAX_IMAGE_BYTES + 1)]))
    assert slack.texts(DM)[-1] == TOO_LARGE
    assert slack.downloads == []
    # The size that the channel states can be wrong, so the download has a byte cap too.
    slack.files[URL] = PNG + b"\x00" * MAX_IMAGE_BYTES
    await gateway.handle_event(dm("see", files=[file(size=10)]))
    assert slack.texts(DM)[-1] == TOO_LARGE
    assert await stored(engine) == []
    assert await run_count(engine) == 0


async def test_bytes_that_do_not_match_the_type_and_failed_downloads_are_refused(
    gateway: SlackGateway,  # noqa: F811
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
    slack: FakeSlack,  # noqa: F811
) -> None:
    await pair(gateway, engine, tenant)
    # A page of the Slack sign-in comes when the scope is missing.
    slack.files[URL] = NOT_AN_IMAGE
    await gateway.handle_event(dm("see", files=[file()]))
    assert slack.texts(DM)[-1] == UNREADABLE
    other = URL.replace("chart", "missing")
    await gateway.handle_event(dm("see", files=[file(other)]))
    assert slack.texts(DM)[-1] == UNREADABLE
    assert await run_count(engine) == 0


async def test_a_url_of_another_host_gets_no_token(
    gateway: SlackGateway,  # noqa: F811
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
    slack: FakeSlack,  # noqa: F811
) -> None:
    await pair(gateway, engine, tenant)
    for url in (
        "https://evil.example/a.png",
        "http://files.slack.com/a.png",
        "https://slack.com.evil.io/a",
    ):
        await gateway.handle_event(dm("see", files=[file(url)]))
        assert slack.texts(DM)[-1] == UNREADABLE
    assert slack.downloads == []
    assert await run_count(engine) == 0
