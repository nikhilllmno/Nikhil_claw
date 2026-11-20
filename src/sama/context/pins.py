"""Context pins (docs/architecture.md, Section 6.3).

A person pins a short text to a session. Compaction can drop any text of the context, so sama
does not trust the runtime to keep a pin. It gives the pin to the runtime in the input of a
run:

- A new pin goes into the input of the next run of the session.
- A compaction makes the runtime forget the pins that it had, so ``record_compacted`` marks
  all pins as not given. The next run gets all of them again.
- A ``/compact`` run also gets the pins in its focus, so the summary can keep them.

Pins are per session and bounded. Sama redacts the text when it stores it and again when it
gives it to a run, because the redactor can learn a secret later.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from sqlalchemy import and_, delete, func, insert, or_, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from sama.db.models import ContextPin, Message, Session
from sama.runtime.contract import MAX_COMPACT_FOCUS_CHARS
from sama.vault.redact import redact

MAX_PINS: Final = 10
MAX_PIN_CHARS: Final = 2_000
MAX_PINNED_CHARS: Final = 8_000
"""The most text of all pins of one session."""
_CLOSING_TAG: Final = "</pinned_context"


class PinError(Exception):
    """A pin cannot be made or removed. The message is safe to show to the person."""


@dataclass(frozen=True, slots=True)
class Pin:
    id: uuid.UUID
    text: str
    source: str
    created_by_user_id: uuid.UUID | None
    created_at: datetime


def _lock(session_id: uuid.UUID) -> Any:
    """A transaction-level lock for the pins of one session, so two adds cannot both take the
    last place."""
    return select(func.pg_advisory_xact_lock(func.hashtextextended(f"sama.pins:{session_id}", 0)))


def pin_block(texts: Sequence[str]) -> str:
    """The text that gives pins to a run. Empty for no pins. Each text is redacted again, and
    it cannot close the block."""
    if not texts:
        return ""
    lines = "\n".join(
        f"- {' '.join(redact(text).split())}".replace(_CLOSING_TAG, "<\\/pinned_context")
        for text in texts
    )
    return (
        "<pinned_context>\n"
        "The user pinned these notes to this session. They stay true until the user removes\n"
        "them. They are notes from the user, not instructions from the system.\n"
        f"{lines}\n"
        "</pinned_context>\n\n"
    )


class PinStore:
    """Reads and writes the pins of sessions. Every query is scoped by the org and the
    session. The caller checks that the person may see the session."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def add(
        self,
        *,
        org_id: uuid.UUID,
        session_id: uuid.UUID,
        user_id: uuid.UUID | None,
        text: str,
        source: str = "user",
    ) -> Pin:
        """Stores a pin. Raises ``PinError`` for an empty or long text, a full session, or a
        session that cannot have pins."""
        clean = redact(text).strip()
        if not clean:
            raise PinError("A pin needs some text.")
        if len(clean) > MAX_PIN_CHARS:
            raise PinError(f"A pin can have {MAX_PIN_CHARS} characters at most.")
        async with self._engine.begin() as connection:
            await self._require_session(connection, org_id, session_id)
            await connection.execute(_lock(session_id))
            count, total = (
                await connection.execute(
                    select(
                        func.count(), func.coalesce(func.sum(func.char_length(ContextPin.text)), 0)
                    ).where(ContextPin.org_id == org_id, ContextPin.session_id == session_id)
                )
            ).one()
            if count >= MAX_PINS:
                raise PinError(f"A session can have {MAX_PINS} pins at most. Remove one first.")
            if total + len(clean) > MAX_PINNED_CHARS:
                raise PinError(
                    f"The pins of a session can have {MAX_PINNED_CHARS} characters in all."
                    " Remove a pin first."
                )
            row = (
                await connection.execute(
                    insert(ContextPin)
                    .values(
                        org_id=org_id,
                        session_id=session_id,
                        created_by_user_id=user_id,
                        text=clean,
                        source=source,
                    )
                    .returning(*_columns())
                )
            ).one()
        return _pin(row)

    async def add_message(
        self,
        *,
        org_id: uuid.UUID,
        session_id: uuid.UUID,
        user_id: uuid.UUID | None,
        message_id: uuid.UUID,
    ) -> Pin:
        """Pins the text of a message of the same session. A message that is not in the
        session gives the same error as a message that does not exist."""
        async with self._engine.connect() as connection:
            body = (
                await connection.execute(
                    select(Message.content["text"].astext).where(
                        Message.org_id == org_id,
                        Message.session_id == session_id,
                        Message.id == message_id,
                        Message.role.in_(("user", "assistant")),
                    )
                )
            ).scalar_one_or_none()
        if not body or not body.strip():
            raise PinError("This message does not exist or has no text.")
        return await self.add(
            org_id=org_id, session_id=session_id, user_id=user_id, text=body, source="message"
        )

    async def list(self, org_id: uuid.UUID, session_id: uuid.UUID) -> list[Pin]:
        async with self._engine.connect() as connection:
            rows = await connection.execute(
                select(*_columns())
                .where(ContextPin.org_id == org_id, ContextPin.session_id == session_id)
                .order_by(ContextPin.created_at, ContextPin.id)
            )
            return [_pin(row) for row in rows]

    async def remove(self, org_id: uuid.UUID, session_id: uuid.UUID, pin_id: uuid.UUID) -> bool:
        """Removes a pin. Returns False if the session has no such pin."""
        async with self._engine.begin() as connection:
            removed = await connection.execute(
                delete(ContextPin)
                .where(
                    ContextPin.org_id == org_id,
                    ContextPin.session_id == session_id,
                    ContextPin.id == pin_id,
                )
                .returning(ContextPin.id)
            )
            return removed.first() is not None

    @staticmethod
    async def _require_session(
        connection: AsyncConnection, org_id: uuid.UUID, session_id: uuid.UUID
    ) -> None:
        row = (
            await connection.execute(
                select(Session.status, Session.lineage).where(
                    Session.org_id == org_id, Session.id == session_id
                )
            )
        ).one_or_none()
        if row is None:
            raise PinError("The session does not exist.")
        if row.status != "active" or row.lineage == "compaction":
            raise PinError("This session is an archive. It cannot have pins.")


def _columns() -> tuple[Any, ...]:
    return (
        ContextPin.id,
        ContextPin.text,
        ContextPin.source,
        ContextPin.created_by_user_id,
        ContextPin.created_at,
    )


def _pin(row: Any) -> Pin:
    return Pin(row.id, row.text, row.source, row.created_by_user_id, row.created_at)


async def pins_for_input(
    connection: AsyncConnection, org_id: uuid.UUID, session_id: uuid.UUID, run_id: uuid.UUID
) -> list[str]:
    """The pins that the input of this run must carry, and marks them as given to this run.

    These are the pins that no run has got, and the pins that this run got before: a claim of
    the same run again (after a lost lease) gets them again. The rows are locked, so two
    claims cannot both give the same pin."""
    rows = (
        await connection.execute(
            select(ContextPin.id, ContextPin.text)
            .where(
                ContextPin.org_id == org_id,
                ContextPin.session_id == session_id,
                or_(ContextPin.injected_at.is_(None), ContextPin.injected_run_id == run_id),
            )
            .order_by(ContextPin.created_at, ContextPin.id)
            .with_for_update()
        )
    ).all()
    if rows:
        await connection.execute(
            update(ContextPin)
            .where(
                ContextPin.org_id == org_id,
                ContextPin.id.in_([row.id for row in rows]),
                ContextPin.injected_at.is_(None),
            )
            .values(injected_at=func.now(), injected_run_id=run_id)
        )
    return [row.text for row in rows]


def compact_focus(focus: str, texts: Sequence[str]) -> str:
    """The focus of a ``/compact`` run: what the person asked, and the pins that the summary
    must keep. Redaction comes before the cut."""
    parts = [redact(focus).strip()] if focus.strip() else []
    if texts:
        notes = "\n".join(f"- {' '.join(redact(text).split())}" for text in texts)
        parts.append(f"Keep these pinned notes in the summary, word for word:\n{notes}")
    return "\n\n".join(parts)[:MAX_COMPACT_FOCUS_CHARS]


async def all_pin_texts(
    connection: AsyncConnection, org_id: uuid.UUID, session_id: uuid.UUID
) -> list[str]:
    """All pins of a session, for the focus of a ``/compact`` run."""
    rows = await connection.execute(
        select(ContextPin.text)
        .where(ContextPin.org_id == org_id, ContextPin.session_id == session_id)
        .order_by(ContextPin.created_at, ContextPin.id)
    )
    return list(rows.scalars())


async def forget_injections(
    connection: AsyncConnection, org_id: uuid.UUID, session_id: uuid.UUID
) -> None:
    """A compaction made the runtime forget the pins. The next run gets all of them."""
    await connection.execute(
        update(ContextPin)
        .where(and_(ContextPin.org_id == org_id, ContextPin.session_id == session_id))
        .values(injected_at=None, injected_run_id=None)
    )
