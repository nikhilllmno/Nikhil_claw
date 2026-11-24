"""Helpers for the tests of media delivery (Section 11.7; roadmap M4.9)."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass

from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.db.models import GeneratedMedia, Run

PNG = b"\x89PNG\r\n\x1a\n" + b"\x01" * 20
OGG = b"OggS" + b"\x02" * 20
CSV = b"rank,country\n1,India\n"


@dataclass(frozen=True)
class RunRef:
    org_id: uuid.UUID
    session_id: uuid.UUID
    id: uuid.UUID


async def latest_run(engine: AsyncEngine, org_id: uuid.UUID) -> RunRef:
    """The newest run of the org."""
    async with engine.connect() as connection:
        row = (
            await connection.execute(
                select(Run.org_id, Run.session_id, Run.id)
                .where(Run.org_id == org_id)
                .order_by(Run.created_at.desc())
            )
        ).first()
    assert row is not None
    return RunRef(org_id=row.org_id, session_id=row.session_id, id=row.id)


async def add_media(
    engine: AsyncEngine,
    run: RunRef,
    kind: str,
    data: bytes,
    *,
    name: str = "",
    mime_type: str | None = None,
) -> uuid.UUID:
    """Stores a file of the run. A ``file`` (``file_send``) is a CSV unless ``mime_type``
    says otherwise."""
    media_id = uuid.uuid4()
    default = {"image": "image/png", "audio": "audio/ogg"}.get(kind, "text/csv")
    async with engine.begin() as connection:
        await connection.execute(
            insert(GeneratedMedia).values(
                id=media_id,
                org_id=run.org_id,
                session_id=run.session_id,
                run_id=run.id,
                kind=kind,
                mime_type=mime_type or default,
                name=name,
                size_bytes=len(data),
                sha256=hashlib.sha256(data).hexdigest(),
                data=data,
            )
        )
    return media_id
