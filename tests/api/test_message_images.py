"""Tests of the images that the web composer sends (Section 11.7, roadmap M8.2)."""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.api.uploads import MAX_UPLOAD_BYTES
from sama.db.models import Attachment, Message, Run, RunSteer
from sama.media.images import MAX_IMAGE_BYTES, TOO_MANY
from sama.orchestrator.queue import RunQueue
from tests.api.conftest import bearer
from tests.gateway.media_support import PNG
from tests.orchestrator.conftest import Tenant, add_tenant

pytestmark = pytest.mark.usefixtures("tenant")

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 20
GIF = b"GIF89a" + b"\x00" * 20
WEBP = b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 20


def mia(tenant: Tenant) -> dict[str, str]:
    return bearer(tenant.ids["mia_auth"])


def url(tenant: Tenant, label: str = "s1") -> str:
    return f"/sessions/{tenant.ids[label]}/messages/images"


def image(
    data: bytes = PNG, name: str = "shot.png", mime: str = "image/png"
) -> tuple[str, tuple[str, bytes, str]]:
    return ("images", (name, data, mime))


async def post(
    client: httpx.AsyncClient,
    tenant: Tenant,
    *files: tuple[str, tuple[str, bytes, str]],
    text: str | None = "look at this",
    label: str = "s1",
    **fields: str,
) -> httpx.Response:
    data = dict(fields)
    if text is not None:
        data["text"] = text
    return await client.post(
        url(tenant, label), headers=mia(tenant), data=data, files=list(files) or None
    )


async def stored(engine: AsyncEngine, tenant: Tenant) -> list[Any]:
    async with engine.connect() as connection:
        return list(
            (
                await connection.execute(
                    select(
                        Attachment.id,
                        Attachment.name,
                        Attachment.mime_type,
                        Attachment.size_bytes,
                        Attachment.message_id,
                    )
                    .where(Attachment.org_id == tenant.org)
                    .order_by(Attachment.name.desc())
                )
            ).all()
        )


async def count(engine: AsyncEngine, model: type[Any], tenant: Tenant) -> int:
    async with engine.connect() as connection:
        return int(
            (
                await connection.execute(
                    select(func.count()).select_from(model).where(model.org_id == tenant.org)
                )
            ).scalar_one()
        )


async def test_an_image_is_stored_with_the_message_and_named_in_the_text(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    response = await post(client, tenant, image(), image(JPEG, "a b.jpg", "image/jpeg"))
    assert response.status_code == 202
    assert response.json()["queued"] is False
    rows = await stored(engine, tenant)
    assert [(row.name, row.mime_type, row.size_bytes) for row in rows] == [
        ("shot.png", "image/png", len(PNG)),
        ("a_b.jpg", "image/jpeg", len(JPEG)),
    ]
    async with engine.connect() as connection:
        message = (await connection.execute(select(Message.id, Message.content))).one()
    assert {row.message_id for row in rows} == {message.id}
    lines = message.content["text"].split("\n")
    assert lines[0] == "look at this"
    assert lines[1] == (
        f"[image attached: {rows[0].id} shot.png; use vision_analyze to look at it]"
    )
    assert lines[2].startswith(f"[image attached: {rows[1].id} a_b.jpg;")


async def test_all_four_image_types_are_accepted(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    response = await post(
        client,
        tenant,
        image(PNG, "a.png", "image/png"),
        image(JPEG, "b.jpg", "image/jpeg"),
        image(GIF, "c.gif", "image/gif"),
        image(WEBP, "d.webp", "image/webp"),
    )
    assert response.status_code == 202
    assert len(await stored(engine, tenant)) == 4


async def test_an_image_alone_is_a_valid_message(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    response = await post(client, tenant, image(), text="")
    assert response.status_code == 202
    async with engine.connect() as connection:
        text = (await connection.execute(select(Message.content["text"].astext))).scalar_one()
    assert text.startswith("[image attached: ")
    # The field can be missing too.
    assert (await post(client, tenant, image(), text=None, label="s2")).status_code == 202


async def test_no_text_and_no_image_is_refused(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    response = await post(client, tenant, text="  ")
    assert response.status_code == 422
    assert await count(engine, Run, tenant) == 0


async def test_a_file_with_a_wrong_signature_is_refused_and_nothing_is_stored(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    response = await post(client, tenant, image(), image(b"<svg/>", "x.png", "image/png"))
    assert response.status_code == 422
    assert await count(engine, Attachment, tenant) == 0
    assert await count(engine, Message, tenant) == 0
    assert await count(engine, Run, tenant) == 0


async def test_a_png_that_claims_to_be_a_jpeg_is_refused(
    client: httpx.AsyncClient, tenant: Tenant
) -> None:
    assert (await post(client, tenant, image(PNG, "x.jpg", "image/jpeg"))).status_code == 422


async def test_an_unsupported_type_is_refused(client: httpx.AsyncClient, tenant: Tenant) -> None:
    for data, mime in ((b"<svg/>", "image/svg+xml"), (b"%PDF-1.4", "application/pdf")):
        response = await post(client, tenant, image(data, "x", mime))
        assert response.status_code == 422, mime


async def test_a_file_without_a_type_gets_it_from_the_first_bytes(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    response = await post(client, tenant, image(WEBP, "pic.webp", "application/octet-stream"))
    assert response.status_code == 202
    [row] = await stored(engine, tenant)
    assert row.mime_type == "image/webp"
    bad = await post(client, tenant, image(b"not an image", "pic", ""), label="s2")
    assert bad.status_code == 422


async def test_an_image_over_10_mb_is_refused(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    big = PNG + b"\x00" * (MAX_IMAGE_BYTES - len(PNG) + 1)
    response = await post(client, tenant, image(big))
    assert response.status_code == 413
    assert await count(engine, Attachment, tenant) == 0
    exact = PNG + b"\x00" * (MAX_IMAGE_BYTES - len(PNG))
    assert (await post(client, tenant, image(exact), label="s2")).status_code == 202


async def test_more_than_four_images_are_refused(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    response = await post(client, tenant, *[image(PNG, f"{n}.png") for n in range(5)])
    assert (response.status_code, response.json()["detail"]) == (422, TOO_MANY)
    many = await post(client, tenant, *[image(PNG, f"{n}.png") for n in range(30)])
    assert many.status_code == 422, many.text
    assert await count(engine, Attachment, tenant) == 0


async def test_a_body_over_the_limit_is_refused_before_it_is_read(
    client: httpx.AsyncClient, tenant: Tenant
) -> None:
    body = b"x" * (MAX_UPLOAD_BYTES + 1)
    headers = {**mia(tenant), "Content-Type": "multipart/form-data; boundary=b"}
    response = await client.post(url(tenant), headers=headers, content=body)
    assert response.status_code == 413


async def test_a_chunked_body_over_the_limit_is_refused(
    client: httpx.AsyncClient, tenant: Tenant
) -> None:
    async def chunks() -> Any:
        yield (
            b'--b\r\nContent-Disposition: form-data; name="images"; filename="a.png"\r\n'
            b"Content-Type: image/png\r\n\r\n"
        )
        for _ in range(MAX_UPLOAD_BYTES // 65_536 + 2):
            yield b"x" * 65_536

    headers = {**mia(tenant), "Content-Type": "multipart/form-data; boundary=b"}
    response = await client.post(url(tenant), headers=headers, content=chunks())
    assert response.status_code == 413


async def test_a_message_that_is_not_multipart_is_refused(
    client: httpx.AsyncClient, tenant: Tenant
) -> None:
    response = await client.post(url(tenant), headers=mia(tenant), json={"text": "hi"})
    assert response.status_code == 422


async def test_a_bad_parallel_value_and_a_long_text_are_refused(
    client: httpx.AsyncClient, tenant: Tenant
) -> None:
    assert (await post(client, tenant, image(), parallel="maybe")).status_code == 422
    assert (await post(client, tenant, image(), text="x" * 32_001)).status_code == 422


async def test_an_image_in_a_session_the_user_cannot_see_gets_404(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    other = await add_tenant(engine)
    response = await client.post(
        url(tenant),
        headers=bearer(other.ids["mia_auth"]),
        data={"text": "hi"},
        files=[image()],
    )
    assert response.status_code == 404
    unknown = await client.post(
        f"/sessions/{uuid.uuid4()}/messages/images",
        headers=mia(tenant),
        data={"text": "hi"},
    )
    assert unknown.status_code == 404
    assert await count(engine, Attachment, tenant) == 0


async def test_a_message_with_an_image_waits_behind_an_open_run_and_does_not_steer(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    session = tenant.ids["s1"]
    await client.post(
        f"/sessions/{session}/messages", headers=mia(tenant), json={"text": "write the report"}
    )
    run = await RunQueue(engine).claim("test-owner")
    assert run is not None
    response = await post(client, tenant, image())
    assert response.status_code == 202
    body = response.json()
    assert (body["queued"], body.get("steered", False)) == (True, False)
    assert body["run_id"] != str(run.run_id)
    async with engine.connect() as connection:
        notes = (await connection.execute(select(func.count()).select_from(RunSteer))).scalar_one()
    assert notes == 0


async def test_a_parallel_message_with_an_image_starts_a_second_run(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    session = tenant.ids["s1"]
    await client.post(f"/sessions/{session}/messages", headers=mia(tenant), json={"text": "slow"})
    queue = RunQueue(engine)
    assert await queue.claim("test-owner") is not None
    response = await post(client, tenant, image(), parallel="true")
    assert response.status_code == 202
    assert response.json()["queued"] is False
    assert await queue.claim("test-owner") is not None


async def test_a_text_message_still_steers_a_running_run(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    session = tenant.ids["s1"]
    url_ = f"/sessions/{session}/messages"
    await client.post(url_, headers=mia(tenant), json={"text": "go"})
    run = await RunQueue(engine).claim("test-owner")
    assert run is not None
    joined = await client.post(url_, headers=mia(tenant), json={"text": "and a chart"})
    assert joined.json() == {"run_id": str(run.run_id), "queued": False, "steered": True}


async def test_the_messages_list_the_images_without_the_bytes(
    client: httpx.AsyncClient, tenant: Tenant
) -> None:
    await client.post(
        f"/sessions/{tenant.ids['s1']}/messages", headers=mia(tenant), json={"text": "plain"}
    )
    response = await post(client, tenant, image(), label="s2")
    assert response.status_code == 202
    plain = (await client.get(f"/sessions/{tenant.ids['s1']}/messages", headers=mia(tenant))).json()
    assert plain[0]["images"] == []
    [message] = (
        await client.get(f"/sessions/{tenant.ids['s2']}/messages", headers=mia(tenant))
    ).json()
    [item] = message["images"]
    assert set(item) == {"id", "name", "mime_type", "size_bytes"}
    assert (item["name"], item["mime_type"], item["size_bytes"]) == (
        "shot.png",
        "image/png",
        len(PNG),
    )
    assert item["id"] in message["text"]


async def test_the_web_can_read_the_bytes_of_a_sent_image(
    client: httpx.AsyncClient, tenant: Tenant
) -> None:
    await post(client, tenant, image())
    [message] = (
        await client.get(f"/sessions/{tenant.ids['s1']}/messages", headers=mia(tenant))
    ).json()
    image_id = message["images"][0]["id"]
    path = f"/sessions/{tenant.ids['s1']}/attachments/{image_id}"
    got = await client.get(path, headers=mia(tenant))
    assert got.status_code == 200
    assert got.content == PNG
    assert got.headers["content-type"] == "image/png"
    assert got.headers["x-content-type-options"] == "nosniff"
    assert got.headers["cache-control"].startswith("private")
    # Without a token the route is closed.
    assert (await client.get(path)).status_code in (401, 403)
    # Another session of the same org does not give the image.
    other = f"/sessions/{tenant.ids['s2']}/attachments/{image_id}"
    assert (await client.get(other, headers=mia(tenant))).status_code == 404


async def test_another_org_cannot_read_a_sent_image(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    await post(client, tenant, image())
    [row] = await stored(engine, tenant)
    other = await add_tenant(engine)
    path = f"/sessions/{tenant.ids['s1']}/attachments/{row.id}"
    assert (await client.get(path, headers=bearer(other.ids["mia_auth"]))).status_code == 404
