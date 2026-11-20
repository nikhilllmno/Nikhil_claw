"""The ``sessions`` toolset: ``session_search`` and ``session_read`` (docs/architecture.md,
Section 7.1 and Section 16.7).

Past messages and session titles are text that people and tools wrote. The result is
untrusted content, like a web page.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from sama.search.core import (
    READ_MESSAGE_CHARS,
    READ_TOTAL_CHARS,
    SNIPPET_START,
    SNIPPET_STOP,
    SessionHit,
    SessionPage,
    clip,
)
from sama.tools.output import untrusted


def _when(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def _header(hit: SessionHit) -> str:
    title = " ".join((hit.title or "(no title)").split())
    return (
        f"session_id: {hit.session_id}\n"
        f"title: {title}\n"
        f"audience: {hit.audience}\n"
        f"started: {_when(hit.created_at)}\n"
        f"last activity: {_when(hit.updated_at)}"
    )


def format_sessions(hits: Sequence[SessionHit], *, query: str | None) -> str:
    """The result of ``session_search``."""
    if not hits:
        body = "No sessions found." if query else "There are no other sessions."
        return untrusted(body, source="session_search")
    parts = []
    for number, hit in enumerate(hits, start=1):
        lines = [f"[{number}] {_header(hit)}"]
        for snippet in hit.snippets:
            text = " ".join(snippet.text.split())
            lines.append(f"  {snippet.role} ({_when(snippet.created_at)}): {text}")
        parts.append("\n".join(lines))
    heading = (
        f"{len(hits)} sessions that match the query. Matching words are between "
        f"{SNIPPET_START} and {SNIPPET_STOP}. Use session_read with a session_id for more."
        if query
        else f"The {len(hits)} latest sessions. Use session_read with a session_id for more."
    )
    return untrusted(heading + "\n\n" + "\n\n".join(parts), source="session_search")


def format_page(page: SessionPage) -> str:
    """The result of ``session_read``. Long messages are cut. The page stops when it reaches
    the size limit, and it says where the next page starts."""
    shown: list[str] = []
    used = 0
    for message in page.messages:
        entry = (
            f"#{message.seq} {message.role} ({_when(message.created_at)}):\n"
            f"{clip(message.text, READ_MESSAGE_CHARS)}"
        )
        if shown and used + len(entry) > READ_TOTAL_CHARS:
            break
        shown.append(entry)
        used += len(entry)
    end = page.offset + len(shown)
    lines = [_header(page.session)]
    lines.append(f"messages: {page.offset + 1 if shown else 0} to {end} of {page.total}")
    if end < page.total:
        lines.append(f"more: call session_read again with offset {end}")
    body = "\n".join(lines)
    if shown:
        body += "\n\n" + "\n\n".join(shown)
    return untrusted(body, source="session_read")
