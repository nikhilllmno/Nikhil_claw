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
_TOOL_INPUT_CHARS: Final = 4_000


def read_transcript(path: str) -> list[ArchiveEntry]:
    """The entries of a transcript file. An unreadable file gives no entries."""
    try:
        file = Path(path)
        size = file.stat().st_size
        with file.open("rb") as handle:
            if size > MAX_TRANSCRIPT_BYTES:
                handle.seek(size - MAX_TRANSCRIPT_BYTES)
                handle.readline()  # the first line is cut
            raw = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    return parse_transcript(raw)


def parse_transcript(raw: str) -> list[ArchiveEntry]:
    entries: list[ArchiveEntry] = []
    for line in raw.splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            entries.extend(_entries(item))
    return entries


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
            arguments = json.dumps(block.get("input"), ensure_ascii=False, default=str)
            if len(arguments) > _TOOL_INPUT_CHARS:
                arguments = arguments[:_TOOL_INPUT_CHARS] + " [cut]"
            return ArchiveEntry(
                role="assistant", text=f"[tool call {block.get('name', '?')}] {arguments}"
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
