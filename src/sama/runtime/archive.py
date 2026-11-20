"""The transcript archive of a compaction (docs/architecture.md, Section 6.3).

Before a runtime compacts its context, it gives the transcript to an ``Archiver``. The runner
in the sandbox has no database, so ``RemoteArchiver`` sends the transcript to the sama tool
server (``POST /archive``) with the run token. The control plane stores it as an archive
session.

This module has no SDK import: every runtime adapter can use it. An adapter turns the
transcript of its SDK into ``ArchiveEntry`` objects. ``fit`` cuts a transcript that is too
long, and it keeps the start and the end.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final, Literal, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field

from sama.runtime.contract import RunScope

ARCHIVE_PATH: Final = "/archive"
MAX_ARCHIVE_BODY_BYTES: Final = 16_000_000
MAX_ENTRY_CHARS: Final = 20_000
"""The longest text of one entry. A longer text keeps its start (70%) and its end (30%)."""
MAX_ENTRIES: Final = 4_000
MAX_ARCHIVE_CHARS: Final = 2_000_000
"""The most text of one archive. A transcript over this limit keeps its start (30%) and its end
(70%), and a marker entry shows the cut."""
ARCHIVE_TIMEOUT_S: Final = 60.0

Role = Literal["user", "assistant", "tool", "system"]


class ArchiveEntry(BaseModel):
    """One turn of the transcript: a user message, an assistant message, a tool call (as an
    ``assistant`` entry), or a tool result (a ``tool`` entry)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    role: Role
    text: str


class TranscriptArchive(BaseModel):
    """The body of ``POST /archive``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    trigger: Literal["auto", "manual"]
    entries: list[ArchiveEntry] = Field(default_factory=list)


class ArchiveAnswer(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    entries: int
    """How many entries the control plane stored, with the marker of a cut."""
    truncated: bool = False


class Archiver(Protocol):
    """Takes the transcript of a session before the runtime compacts it. It can raise. The
    runtime then compacts anyway: an archive that fails must not block a session that has no
    room left."""

    async def archive(self, scope: RunScope, archive: TranscriptArchive) -> None: ...


def _clip(text: str) -> str:
    if len(text) <= MAX_ENTRY_CHARS:
        return text
    marker = f"\n[... {len(text) - MAX_ENTRY_CHARS} characters cut ...]\n"
    head = (MAX_ENTRY_CHARS - len(marker)) * 7 // 10
    tail = MAX_ENTRY_CHARS - len(marker) - head
    return text[:head] + marker + text[len(text) - tail :]


def fit(entries: Sequence[ArchiveEntry]) -> tuple[list[ArchiveEntry], bool]:
    """The entries cut to the limits, and whether anything was cut. A long entry keeps its
    start and its end. A long transcript keeps its first 30% and its last 70% of the text
    budget, with a ``system`` entry between them that says how many entries are not here."""
    clipped = [ArchiveEntry(role=e.role, text=_clip(e.text)) for e in entries]
    cut = any(len(c.text) != len(e.text) for c, e in zip(clipped, entries, strict=True))
    total = sum(len(e.text) for e in clipped)
    if len(clipped) <= MAX_ENTRIES and total <= MAX_ARCHIVE_CHARS:
        return clipped, cut
    head_chars, tail_chars = MAX_ARCHIVE_CHARS * 3 // 10, MAX_ARCHIVE_CHARS * 7 // 10
    head_limit, tail_limit = MAX_ENTRIES * 3 // 10, MAX_ENTRIES * 7 // 10 - 1
    head: list[ArchiveEntry] = []
    used = 0
    for entry in clipped:
        if len(head) >= head_limit or used + len(entry.text) > head_chars:
            break
        head.append(entry)
        used += len(entry.text)
    tail: list[ArchiveEntry] = []
    used = 0
    for entry in reversed(clipped[len(head) :]):
        if len(tail) >= tail_limit or used + len(entry.text) > tail_chars:
            break
        tail.append(entry)
        used += len(entry.text)
    tail.reverse()
    omitted = len(clipped) - len(head) - len(tail)
    marker = ArchiveEntry(role="system", text=f"[{omitted} entries are not in this archive]")
    return [*head, marker, *tail], True


class RemoteArchiver:
    """An ``Archiver`` that sends the transcript to ``POST <url>`` with the run token."""

    def __init__(
        self,
        url: str,
        token: str,
        *,
        timeout_s: float = ARCHIVE_TIMEOUT_S,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._url = url
        self._token = token
        self._timeout_s = timeout_s
        self._transport = transport

    def __repr__(self) -> str:
        return f"RemoteArchiver(url={self._url!r}, token=<hidden>)"

    async def archive(self, scope: RunScope, archive: TranscriptArchive) -> None:
        entries, _cut = fit(archive.entries)
        body = TranscriptArchive(trigger=archive.trigger, entries=entries)
        async with httpx.AsyncClient(
            timeout=self._timeout_s, transport=self._transport, follow_redirects=False
        ) as client:
            response = await client.post(
                self._url,
                content=body.model_dump_json(),
                headers={
                    "authorization": f"Bearer {self._token}",
                    "content-type": "application/json",
                },
            )
        response.raise_for_status()
