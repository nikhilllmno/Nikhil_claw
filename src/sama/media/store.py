"""Reads the images of a session (docs/architecture.md, Section 11.7)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.db.models import Attachment


@dataclass(frozen=True, slots=True)
class StoredImage:
    id: uuid.UUID
    name: str
    mime_type: str
    size_bytes: int
    data: bytes = field(repr=False)


class AttachmentStore:
    """Gives the bytes of an image to the tool server. The read is scoped by org and session:
    an image of another session is the same as an image that does not exist."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def load(
        self, *, org_id: uuid.UUID, session_id: uuid.UUID, attachment_id: uuid.UUID | None
    ) -> StoredImage | None:
        """The image with ``attachment_id``, or the newest image of the session if the id is
        ``None``."""
        query = select(
            Attachment.id,
            Attachment.name,
            Attachment.mime_type,
            Attachment.size_bytes,
            Attachment.data,
        ).where(Attachment.org_id == org_id, Attachment.session_id == session_id)
        if attachment_id is None:
            query = query.order_by(Attachment.created_at.desc(), Attachment.id).limit(1)
        else:
            query = query.where(Attachment.id == attachment_id)
        async with self._engine.connect() as connection:
            row = (await connection.execute(query)).first()
        if row is None:
            return None
        return StoredImage(row.id, row.name, row.mime_type, row.size_bytes, bytes(row.data))
