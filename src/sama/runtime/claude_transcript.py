"""Reads the transcript file of the Claude Code CLI for the compaction archive
(docs/architecture.md, Section 6.3).

The CLI writes one JSON object for each line to ``<config>/projects/<cwd>/<session>.jsonl``. A
line of the type ``user`` or ``assistant`` has a ``message`` with ``content``: a text, or a
list of blocks (``text``, ``thinking``, ``tool_use``, ``tool_result``, and others). The format is
not a public contract, so the parser reads what it knows and skips the rest. It never raises
for a line that it does not understand.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any, Final, Literal

from sama.runtime.archive import ArchiveEntry

MAX_TRANSCRIPT_BYTES: Final = 64 * 1024 * 1024
"""A larger file keeps its last bytes. The archive cuts long transcripts anyway."""


def read_transcript(path: str) -> tuple[list[ArchiveEntry], int]:
    """The entries of a transcript file, and how many entries stay out (whole). An unreadable
    file gives no entries."""
    try:
        file = Path(path)
        size = file.stat().st_size
        with file.open("rb") as handle:
            if size > MAX_TRANSCRIPT_BYTES:
                handle.seek(size - MAX_TRANSCRIPT_BYTES)
                handle.readline()  # the first line is cut
            raw = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return [], 0
    entries, omitted = parse_transcript(raw)
    if size > MAX_TRANSCRIPT_BYTES:
        marker = ArchiveEntry(role="system", text="[the start of the transcript is not here]")
        return [marker, *entries], omitted + 1
    return entries, omitted


def parse_transcript(raw: str) -> tuple[list[ArchiveEntry], int]:
    """The entries of a transcript, and how many entries stay out. An entry that is too large
    for the wire (or has a tool name that is too long) stays out whole, and a marker takes its
    place: the control plane redacts a whole entry, so the parser must not cut one. The check
    is for each content block, so the other blocks of the same message stay in.
    """
    entries: list[ArchiveEntry] = []
    omitted = 0
    for line in raw.splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if not isinstance(item, dict):
            continue
        for entry in _entries(item):
            if entry is None:
                omitted += 1
                entry = ArchiveEntry(role="system", text=_LARGE_ENTRY)
            entries.append(entry)
    return entries, omitted


_LARGE_ENTRY: Final = "[a large entry is not in this archive]"


def _entry(**fields: Any) -> ArchiveEntry | None:
    """The entry, or None if it is over the limits of the model. The marker has no part of the
    entry: its text could hold a secret."""
    try:
        return ArchiveEntry(**fields)
    except ValueError:
        return None


def _entries(item: Mapping[str, Any]) -> Iterator[ArchiveEntry | None]:
    """The entries of one line. None stands for an entry that is too large."""
    kind = item.get("type")
    message = item.get("message")
    if kind not in ("user", "assistant") or not isinstance(message, dict):
        return
    role: Literal["user", "assistant"] = "assistant" if kind == "assistant" else "user"
    content = message.get("content")
    if isinstance(content, str):
        if content.strip():
            yield _entry(role=role, text=content)
        return
    if not isinstance(content, list):
        return
    for block in content:
        if isinstance(block, dict):
            yield from _block(role, block)


def _block(
    role: Literal["user", "assistant"], block: Mapping[str, Any]
) -> Iterator[ArchiveEntry | None]:
    match block.get("type"):
        case "text":
            text = block.get("text")
            if isinstance(text, str) and text.strip():
                yield _entry(role=role, text=text)
        case "tool_use":
            # The input stays a JSON value and whole. The control plane redacts it, and then it
            # makes the text and cuts it (a cut before the redaction could split a secret). The
            # name stays whole too: a name that is too long makes the entry stay out.
            yield _entry(
                role="assistant",
                tool_name=str(block.get("name", "?")),
                tool_input=block.get("input"),
            )
        case "tool_result":
            text = _result_text(block.get("content"))
            if text.strip():
                yield _entry(role="tool", text=text)


def _result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                parts.append(str(part.get("text", "")))
            elif isinstance(part, dict) and part.get("type") == "image":
                parts.append("[image]")
        return "\n".join(parts)
    return ""
