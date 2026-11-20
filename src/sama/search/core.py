"""The rules of session search that need no database (docs/architecture.md, Section 7.1)."""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final

MAX_QUERY_CHARS: Final = 400
DEFAULT_LIMIT: Final = 5
MAX_LIMIT: Final = 10
SNIPPETS_PER_SESSION: Final = 2
CANDIDATES: Final = 50
"""Each ranked list (full-text, meaning) gives this many messages to the fusion."""
RRF_K: Final = 60
"""The constant of reciprocal rank fusion. 60 is the value of the original paper."""
MAX_DISTANCE: Final = 0.7
"""A message with a larger cosine distance is not a meaning match. Text that has nothing to
do with the query has a distance near 1."""
READ_DEFAULT_MESSAGES: Final = 20
READ_MAX_MESSAGES: Final = 50
READ_MESSAGE_CHARS: Final = 4_000
READ_TOTAL_CHARS: Final = 20_000
HEADLINE_SOURCE_CHARS: Final = 20_000
"""The snippet comes from the first characters of a message."""
SNIPPET_START: Final = "<<"
SNIPPET_STOP: Final = ">>"


class SessionSearchError(Exception):
    """A search or a read failed in a way that the agent can fix. The message is safe to show
    to the agent."""


@dataclass(frozen=True, slots=True)
class Snippet:
    message_id: uuid.UUID
    role: str
    created_at: datetime
    text: str


@dataclass(frozen=True, slots=True)
class SessionHit:
    session_id: uuid.UUID
    workspace_id: uuid.UUID
    title: str | None
    audience: str
    created_at: datetime
    updated_at: datetime
    snippets: tuple[Snippet, ...] = ()


@dataclass(frozen=True, slots=True)
class PageMessage:
    seq: int
    role: str
    created_at: datetime
    text: str


@dataclass(frozen=True, slots=True)
class SessionPage:
    session: SessionHit
    total: int
    offset: int
    messages: tuple[PageMessage, ...] = field(default=())


@dataclass(frozen=True, slots=True)
class Candidate:
    """A message that one ranked list found."""

    message_id: uuid.UUID
    session_id: uuid.UUID


def vector_literal(values: Sequence[float]) -> str:
    """The text form of a pgvector value."""
    return "[" + ",".join(repr(float(value)) for value in values) + "]"


def fuse(rankings: Sequence[Sequence[Candidate]]) -> list[tuple[Candidate, float]]:
    """Reciprocal rank fusion: a message scores ``1 / (RRF_K + rank)`` in each list that has
    it, and the scores add up. The result is best first. A message that two lists found comes
    before a message that only one list found at the same rank."""
    scores: dict[uuid.UUID, float] = defaultdict(float)
    found: dict[uuid.UUID, Candidate] = {}
    for ranking in rankings:
        for rank, candidate in enumerate(ranking, start=1):
            scores[candidate.message_id] += 1.0 / (RRF_K + rank)
            found.setdefault(candidate.message_id, candidate)
    ordered = sorted(scores, key=lambda message_id: (-scores[message_id], str(message_id)))
    return [(found[message_id], scores[message_id]) for message_id in ordered]


def pick(
    fused: Sequence[tuple[Candidate, float]], limit: int
) -> list[tuple[uuid.UUID, list[uuid.UUID]]]:
    """The best ``limit`` sessions, each with its best messages. A session ranks as its best
    message. The result is a list of ``(session_id, message_ids)``, best session first."""
    sessions: dict[uuid.UUID, list[uuid.UUID]] = {}
    for candidate, _score in fused:
        messages = sessions.get(candidate.session_id)
        if messages is None:
            if len(sessions) >= limit:
                continue
            messages = sessions[candidate.session_id] = []
        if len(messages) < SNIPPETS_PER_SESSION:
            messages.append(candidate.message_id)
    return list(sessions.items())


def clip(text: str, limit: int) -> str:
    """Cuts a text to ``limit`` characters and marks the cut."""
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + " [cut]"
