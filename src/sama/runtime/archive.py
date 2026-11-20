"""The transcript archive of a compaction (docs/architecture.md, Section 6.3).

Before a runtime compacts its context, it gives the transcript to an ``Archiver``. The runner
in the sandbox has no database, so ``RemoteArchiver`` sends the transcript to the sama tool
server (``POST /archive``) with the run token. The control plane stores it as an archive
session.

This module has no SDK import: every runtime adapter can use it. An adapter turns the
transcript of its SDK into ``ArchiveEntry`` objects. The runner has no redactor, so it never
cuts inside an entry: ``bound_for_wire`` leaves whole entries out. The control plane redacts
each entry, and then ``fit`` cuts what is too long, and it keeps the start and the end.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any, Final, Literal, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator

from sama.runtime.contract import RunScope

ARCHIVE_PATH: Final = "/archive"
MAX_ARCHIVE_BODY_BYTES: Final = 8_000_000
"""The body limit of ``POST /archive``. The tool server refuses a larger body at once."""
MAX_WIRE_ENTRIES: Final = 5_000
"""The most entries of one request. The model refuses a longer list."""
MAX_WIRE_ENTRY_BYTES: Final = 1_000_000
"""The largest entry that the runner sends, in bytes of its JSON text in UTF-8
(``entry_wire_bytes``). The model of the entry and ``bound_for_wire`` use the same definition.
A larger entry stays out whole (a marker takes its place): the control plane redacts the
whole value, so a cut before the redaction could split a secret into parts that the redactor
does not know."""
MAX_WIRE_BYTES: Final = 6_000_000
"""The most bytes of all entries of one request. The runner leaves whole entries out."""
MAX_TOOL_NAME_CHARS: Final = 200
"""The longest tool name. The model refuses a longer name. Nobody cuts it: the control plane
redacts the whole name, and a cut before could split a secret."""
MAX_ENTRY_CHARS: Final = 20_000
"""The longest text of one stored entry. A longer text keeps its start (70%) and its end (30%).
The control plane cuts after the redaction."""
MAX_ENTRIES: Final = 4_000
MAX_ARCHIVE_CHARS: Final = 2_000_000
"""The most text of one archive. A transcript over this limit keeps its start (30%) and its end
(70%), and a marker entry shows the cut."""
ARCHIVE_TIMEOUT_S: Final = 60.0

Role = Literal["user", "assistant", "tool", "system"]


class ArchiveEntry(BaseModel):
    """One turn of the transcript: a user message, an assistant message, a tool call (an
    ``assistant`` entry with ``tool_name`` and ``tool_input``), or a tool result (a ``tool``
    entry).

    The input of a tool call stays a JSON value on the wire. The control plane redacts the
    value, and then it makes the text. A redaction of a JSON text would miss a secret that has
    quotes, backslashes, or new lines, because the JSON text escapes them.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    role: Role
    text: str = ""
    tool_name: str | None = Field(default=None, max_length=MAX_TOOL_NAME_CHARS)
    tool_input: Any = None

    @model_validator(mode="after")
    def _bounded(self) -> ArchiveEntry:
        # One limit for the whole entry, in bytes of the JSON text in UTF-8, the same value that
        # ``bound_for_wire`` uses. A limit for each field would count characters, and it would
        # miss an entry whose fields are each small but large together.
        if entry_wire_bytes(self) > MAX_WIRE_ENTRY_BYTES:
            raise ValueError("the entry is too large")
        return self


def entry_wire_bytes(entry: ArchiveEntry) -> int:
    """The size of an entry on the wire: the bytes of its JSON text in UTF-8. The size limit of
    an entry and ``bound_for_wire`` both use it."""
    return len(entry.model_dump_json(exclude_defaults=True).encode())


class TranscriptArchive(BaseModel):
    """The body of ``POST /archive``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    trigger: Literal["auto", "manual"]
    entries: list[ArchiveEntry] = Field(default_factory=list, max_length=MAX_WIRE_ENTRIES)
    omitted: int = Field(default=0, ge=0, le=1_000_000_000)
    """How many entries the runner left out, whole."""


def parse_archive(raw: bytes) -> TranscriptArchive:
    """The model of a request body. Raises ``ValueError`` (a ``ValidationError`` too) for a
    body that is not valid.

    Pydantic checks the length of a list after it built all items, and 200,000 small entries
    need hundreds of MB. So the list is counted first, and it does not matter how the body
    spells its keys (``"role"`` and ``"\\u0072ole"`` are the same key). The count is a parse
    of the body in which every JSON object becomes ``0`` (``object_pairs_hook``): the items of
    the list cost one pointer each, and the check needs no copy of any entry. Only a body with
    at most ``MAX_WIRE_ENTRIES`` entries goes to the model.
    """
    seen: list[list[tuple[str, Any]]] = []

    def hook(pairs: list[tuple[str, Any]]) -> int:
        # The last object that the decoder finishes is the outer object of the body.
        seen[:] = [pairs]
        return 0

    try:
        json.loads(raw, object_pairs_hook=hook)
    except RecursionError as error:
        raise ValueError("the archive is nested too deeply") from error
    for key, value in seen[0] if seen else ():
        if key == "entries" and isinstance(value, list) and len(value) > MAX_WIRE_ENTRIES:
            raise ValueError("the archive has too many entries")
    return TranscriptArchive.model_validate_json(raw)


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
    """The entries cut to the stored limits, and whether anything was cut. The control plane
    calls it after the redaction, on entries that have text only. A long entry keeps its start
    and its end. A long transcript keeps its first 30% and its last 70% of the text budget,
    with a ``system`` entry between them that says how many entries are not here."""
    clipped = [e.model_copy(update={"text": _clip(e.text)}) for e in entries]
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


def bound_for_wire(
    entries: Sequence[ArchiveEntry], omitted: int = 0
) -> tuple[list[ArchiveEntry], int]:
    """The entries for ``POST /archive``, and how many entries stay out. ``omitted`` is the
    number that the caller left out before (the total includes it).

    The runner has no redactor, so it must not cut inside an entry: a cut could split a secret.
    It leaves whole entries out. An entry over ``MAX_WIRE_ENTRY_BYTES`` stays out, and a marker
    takes its place. If there are too many entries, or too many bytes, the runner keeps the
    first 30% and the last 70% and leaves the middle out, with one marker. The control plane
    clips the long text after it redacted the text."""
    sized: list[tuple[ArchiveEntry, int]] = []
    for entry in entries:
        size = entry_wire_bytes(entry)
        if size > MAX_WIRE_ENTRY_BYTES:
            omitted += 1
            marker = ArchiveEntry(
                role="system", text=f"[an entry of {size} bytes is not in this archive]"
            )
            sized.append((marker, entry_wire_bytes(marker)))
        else:
            sized.append((entry, size))
    if len(sized) <= MAX_WIRE_ENTRIES and sum(size for _, size in sized) <= MAX_WIRE_BYTES:
        return [entry for entry, _ in sized], omitted
    head_bytes, tail_bytes = MAX_WIRE_BYTES * 3 // 10, MAX_WIRE_BYTES * 7 // 10
    head_limit, tail_limit = (MAX_WIRE_ENTRIES - 1) * 3 // 10, (MAX_WIRE_ENTRIES - 1) * 7 // 10
    head: list[ArchiveEntry] = []
    used = 0
    for entry, size in sized:
        if len(head) >= head_limit or used + size > head_bytes:
            break
        head.append(entry)
        used += size
    tail: list[ArchiveEntry] = []
    used = 0
    for entry, size in reversed(sized[len(head) :]):
        if len(tail) >= tail_limit or used + size > tail_bytes:
            break
        tail.append(entry)
        used += size
    tail.reverse()
    left_out = len(sized) - len(head) - len(tail)
    marker = ArchiveEntry(role="system", text=f"[{left_out} entries are not in this archive]")
    return [*head, marker, *tail], omitted + left_out


class RemoteArchiver:
    """An ``Archiver`` that sends the transcript to ``POST <url>`` with the run token. The
    transcript is in the limits of the wire already (``bound_for_wire``): the model of the
    transcript refuses more entries."""

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
        async with httpx.AsyncClient(
            timeout=self._timeout_s, transport=self._transport, follow_redirects=False
        ) as client:
            response = await client.post(
                self._url,
                content=archive.model_dump_json(exclude_defaults=True),
                headers={
                    "authorization": f"Bearer {self._token}",
                    "content-type": "application/json",
                },
            )
        response.raise_for_status()
