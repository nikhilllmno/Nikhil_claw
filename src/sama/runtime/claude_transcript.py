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

from sama.runtime.archive import MAX_TOOL_NAME_CHARS, ArchiveEntry

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
    for the wire stays out whole, and a marker takes its place: the control plane redacts a
    whole entry, so the parser must not cut one."""
    entries: list[ArchiveEntry] = []
    omitted = 0
    for line in raw.splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if not isinstance(item, dict):
            continue
        try:
            entries.extend(_entries(item))
        except ValueError:  # an entry over the limits of the model
            omitted += 1
            entries.append(
                ArchiveEntry(role="system", text="[a large entry is not in this archive]")
            )
    return entries, omitted


def _entries(item: Mapping[str, Any]) -> Iterator[ArchiveEntry]:
    kind = item.get("type")
    message = item.get("message")
    if kind not in ("user", "assistant") or not isinstance(message, dict):
        return
    role: Literal["user", "assistant"] = "assistant" if kind == "assistant" else "user"
    content = message.get("content")
    if isinstance(content, str):
        if content.strip():
            yield ArchiveEntry(role=role, text=content)
        return
    if not isinstance(content, list):
        return
    for block in content:
        if isinstance(block, dict):
            entry = _block(role, block)
            if entry is not None:
                yield entry


def _block(role: Literal["user", "assistant"], block: Mapping[str, Any]) -> ArchiveEntry | None:
    match block.get("type"):
        case "text":
            text = block.get("text")
            if isinstance(text, str) and text.strip():
                return ArchiveEntry(role=role, text=text)
        case "tool_use":
            # The input stays a JSON value and whole. The control plane redacts it, and then it
            # makes the text and cuts it (a cut before the redaction could split a secret).
            return ArchiveEntry(
                role="assistant",
                tool_name=str(block.get("name", "?"))[:MAX_TOOL_NAME_CHARS],
                tool_input=block.get("input"),
            )
        case "tool_result":
            text = _result_text(block.get("content"))
            if text.strip():
                return ArchiveEntry(role="tool", text=text)
    return None


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
