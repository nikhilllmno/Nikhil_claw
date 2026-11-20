"""The attachment store and the queue (docs/architecture.md, Section 11.7)."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import delete, func, insert, select
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.db.models import Attachment, Message, Run
from sama.media.images import NewAttachment
from sama.media.store import AttachmentStore
from sama.orchestrator.queue import RunQueue
from tests.media.support import GIF, JPEG, PNG, add_image
from tests.orchestrator.conftest import Tenant


async def count(engine: AsyncEngine, model: type) -> int:
    async with engine.connect() as connection:
        return int((await connection.execute(select(func.count()).select_from(model))).scalar_one())


async def test_load_gives_the_bytes_of_an_image_of_the_session(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    image_id = await add_image(
        engine, tenant.org, tenant.ids["s1"], data=JPEG, mime_type="image/jpeg"
    )
    found = await AttachmentStore(engine).load(
        org_id=tenant.org, session_id=tenant.ids["s1"], attachment_id=image_id
    )
    assert found is not None
    assert (found.id, found.mime_type, found.size_bytes, found.data) == (
        image_id,
        "image/jpeg",
        len(JPEG),
        JPEG,
    )
    assert JPEG not in repr(found).encode(), "the bytes are not in a log line"


async def test_none_is_the_newest_image_of_the_session(engine: AsyncEngine, tenant: Tenant) -> None:
    session = tenant.ids["s1"]
    await add_image(engine, tenant.org, session, data=PNG)
    newest = await add_image(engine, tenant.org, session, data=GIF, mime_type="image/gif")
    await add_image(engine, tenant.org, tenant.ids["s2"], data=JPEG, mime_type="image/jpeg")
    store = AttachmentStore(engine)
    found = await store.load(org_id=tenant.org, session_id=session, attachment_id=None)
    assert found is not None
    assert found.id == newest


async def test_an_image_of_another_session_or_org_is_not_found(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    image_id = await add_image(engine, tenant.org, tenant.ids["s1"])
    store = AttachmentStore(engine)
    other_session = await store.load(
        org_id=tenant.org, session_id=tenant.ids["s2"], attachment_id=image_id
    )
    other_org = await store.load(
        org_id=uuid.uuid4(), session_id=tenant.ids["s1"], attachment_id=image_id
    )
    nothing = await store.load(org_id=tenant.org, session_id=tenant.ids["s2"], attachment_id=None)
    assert (other_session, other_org, nothing) == (None, None, None)


async def test_enqueue_stores_the_message_the_run_and_the_images_together(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    first = NewAttachment(name="a.png", mime_type="image/png", data=PNG)
    second = NewAttachment(name="b.gif", mime_type="image/gif", data=GIF)
    await RunQueue(engine).enqueue(
        org_id=tenant.org,
        session_id=tenant.ids["s1"],
        text="two images",
        user_id=tenant.ids["mia"],
        attachments=[first, second],
    )
    async with engine.connect() as connection:
        rows = (
            await connection.execute(
                select(Attachment.id, Attachment.size_bytes, Attachment.sha256, Message.content)
                .join(Message, Message.id == Attachment.message_id)
                .order_by(Attachment.name)
            )
        ).all()
    assert [(row.id, row.size_bytes, row.sha256) for row in rows] == [
        (first.id, len(PNG), first.sha256),
        (second.id, len(GIF), second.sha256),
    ]
    assert rows[0].content == {"text": "two images"}


async def test_an_image_that_the_database_refuses_leaves_no_message_and_no_run(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    messages, runs = await count(engine, Message), await count(engine, Run)
    bad = NewAttachment(name="a.svg", mime_type="image/svg+xml", data=b"<svg/>")
    with pytest.raises(IntegrityError):
        await RunQueue(engine).enqueue(
            org_id=tenant.org,
            session_id=tenant.ids["s1"],
            text="x",
            user_id=tenant.ids["mia"],
            attachments=[NewAttachment(name="ok.png", mime_type="image/png", data=PNG), bad],
        )
    assert (await count(engine, Message), await count(engine, Run)) == (messages, runs)
    assert await count(engine, Attachment) == 0


async def test_the_database_checks_the_size_and_the_message(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    session = tenant.ids["s1"]
    await add_image(engine, tenant.org, session)
    async with engine.connect() as connection:
        message_id = (await connection.execute(select(Message.id).limit(1))).scalar_one()
    base = {
        "org_id": tenant.org,
        "session_id": session,
        "message_id": message_id,
        "name": "a",
        "mime_type": "image/png",
        "sha256": "h",
    }
    for values in (
        {"size_bytes": 99, "data": PNG},  # the size is not the size of the data
        {"size_bytes": 0, "data": b""},  # empty
        {"size_bytes": 10 * 1024 * 1024 + 1, "data": b"x" * (10 * 1024 * 1024 + 1)},  # large
    ):
        with pytest.raises(IntegrityError):
            async with engine.begin() as connection:
                await connection.execute(insert(Attachment).values(**base, **values))
    # A message of another session is not a parent: the key has the session.
    with pytest.raises(DBAPIError):
        async with engine.begin() as connection:
            await connection.execute(
                insert(Attachment).values(
                    **{**base, "session_id": tenant.ids["s2"]}, size_bytes=len(PNG), data=PNG
                )
            )


async def test_deleting_the_message_deletes_its_images(engine: AsyncEngine, tenant: Tenant) -> None:
    await add_image(engine, tenant.org, tenant.ids["s1"])
    assert await count(engine, Attachment) == 1
    async with engine.begin() as connection:
        await connection.execute(delete(Message).where(Message.session_id == tenant.ids["s1"]))
    assert await count(engine, Attachment) == 0
