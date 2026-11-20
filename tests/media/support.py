"""Small image bytes for the tests. Only the first bytes matter: sama checks the signature."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.db.models import Attachment, Message

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 24
GIF = b"GIF89a" + b"\x00" * 24
WEBP = b"RIFF\x1c\x00\x00\x00WEBPVP8 " + b"\x00" * 16
NOT_AN_IMAGE = b"<html>sign in</html>"


async def add_image(
    engine: AsyncEngine,
    org: uuid.UUID,
    session: uuid.UUID,
    *,
    data: bytes = PNG,
    mime_type: str = "image/png",
    name: str = "chart.png",
    created_at: Any = None,
) -> uuid.UUID:
    """Writes a user message with one image. Gives the id of the image."""
    image_id = uuid.uuid4()
    async with engine.begin() as connection:
        message_id = (
            await connection.execute(
                insert(Message)
                .values(org_id=org, session_id=session, role="user", content={"text": "see"})
                .returning(Message.id)
            )
        ).scalar_one()
        values: dict[str, Any] = {
            "id": image_id,
            "org_id": org,
            "session_id": session,
            "message_id": message_id,
            "name": name,
            "mime_type": mime_type,
            "size_bytes": len(data),
            "sha256": "0" * 64,
            "data": data,
        }
        if created_at is not None:
            values["created_at"] = created_at
        await connection.execute(insert(Attachment).values(**values))
    return image_id
