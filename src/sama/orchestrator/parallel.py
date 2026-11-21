"""The text blocks that join parallel runs to the transcript of their session (docs/
architecture.md, Section 4.3, M7.1).

A side run starts while another chat run of its session is open. It starts a new runtime
session, so its input carries the transcript: the messages of the session before it started,
and the requests that the other open runs still work on.

A main run resumes the runtime state of the session. The side runs that completed after that
state was made are not in it, so the input of the main run carries their turns (the catch-up).

The functions here only make text. The messages are redacted when they are stored, and each
text is redacted again here. A text cannot close its block.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from sama.vault.redact import redact

SIDE_MESSAGES: Final = 30
"""The most messages of the session that the input of a side run carries."""
SIDE_CHARS: Final = 24_000
"""The most characters of the transcript block of a side run."""
MESSAGE_CHARS: Final = 2_000
"""The most characters of one message in a block. A longer message is cut."""
CATCH_UP_TURNS: Final = 10
"""The most side-run turns in the catch-up block of a main run."""
OPEN_REQUEST_CHARS: Final = 500
"""The most characters of one open request in the block of a side run."""

_TRANSCRIPT_TAG: Final = "session_transcript"
_OPEN_TAG: Final = "open_runs"
_CATCH_UP_TAG: Final = "parallel_answers"


@dataclass(frozen=True, slots=True)
class Line:
    """One message of the transcript."""

    role: str
    text: str


@dataclass(frozen=True, slots=True)
class Turn:
    """The request and the answer of one completed side run."""

    request: str
    answer: str


def _clip(text: str, limit: int) -> str:
    text = redact(text).strip()
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def _safe(text: str, tag: str) -> str:
    return text.replace(f"</{tag}", f"<\\/{tag}")


def side_block(lines: Sequence[Line], open_requests: Sequence[str]) -> str:
    """The text in front of the request of a side run. ``lines`` are the messages of the
    session before the run started, oldest first. The newest messages that fit stay.
    ``open_requests`` are the requests of the other open runs. Empty if both are empty."""
    parts: list[str] = []
    kept: list[str] = []
    size = 0
    for line in reversed(lines[-SIDE_MESSAGES:]):
        entry = f"[{line.role}] {_safe(_clip(line.text, MESSAGE_CHARS), _TRANSCRIPT_TAG)}"
        if size + len(entry) > SIDE_CHARS:
            break
        kept.append(entry)
        size += len(entry) + 2
    if kept:
        skipped = len(lines) - len(kept)
        note = f"\n({skipped} older messages are not shown.)" if skipped > 0 else ""
        parts.append(
            f"<{_TRANSCRIPT_TAG}>\n"
            "The messages of this session before the new request, oldest first. Use them as"
            f" context.{note}\n\n" + "\n\n".join(reversed(kept)) + f"\n</{_TRANSCRIPT_TAG}>"
        )
    if open_requests:
        items = "\n".join(
            f"- {_safe(' '.join(_clip(text, OPEN_REQUEST_CHARS).split()), _OPEN_TAG)}"
            for text in open_requests
        )
        parts.append(
            f"<{_OPEN_TAG}>\n"
            "Other runs of this session work on these requests at the same time, in the same"
            " files. Their answers come separately. Do not do their work, and do not change a"
            f" file that they work on.\n{items}\n</{_OPEN_TAG}>"
        )
    if not parts:
        return ""
    return "\n\n".join(parts) + "\n\nThe new request:\n"


def catch_up_block(turns: Sequence[Turn]) -> str:
    """The text in front of the message of a main run: the side runs that completed after the
    runtime state of the session was made, in the order that they completed. Only the newest
    ``CATCH_UP_TURNS`` stay. Empty if there are none."""
    if not turns:
        return ""
    shown = turns[-CATCH_UP_TURNS:]
    entries = [
        f"[user] {_safe(_clip(turn.request, MESSAGE_CHARS), _CATCH_UP_TAG)}\n"
        f"[assistant] {_safe(_clip(turn.answer, MESSAGE_CHARS), _CATCH_UP_TAG)}"
        for turn in shown
    ]
    skipped = len(turns) - len(shown)
    note = f" ({skipped} older ones are not shown.)" if skipped > 0 else ""
    return (
        f"<{_CATCH_UP_TAG}>\n"
        "While you worked, parallel runs of this session answered these requests. They are"
        f" part of the conversation now.{note}\n\n"
        + "\n\n".join(entries)
        + f"\n</{_CATCH_UP_TAG}>\n\n"
    )
