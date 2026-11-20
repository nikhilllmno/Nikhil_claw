"""Session search: full-text and meaning search over past sessions (docs/architecture.md,
Section 7.1)."""

from sama.search.core import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    MAX_QUERY_CHARS,
    READ_DEFAULT_MESSAGES,
    READ_MAX_MESSAGES,
    PageMessage,
    SessionHit,
    SessionPage,
    SessionSearchError,
    Snippet,
)
from sama.search.embed import (
    EMBEDDING_MODEL,
    Embedder,
    EmbedError,
    EmbedWorker,
    OpenAIEmbedder,
)
from sama.search.service import SessionSearch

__all__ = [
    "DEFAULT_LIMIT",
    "EMBEDDING_MODEL",
    "MAX_LIMIT",
    "MAX_QUERY_CHARS",
    "READ_DEFAULT_MESSAGES",
    "READ_MAX_MESSAGES",
    "EmbedError",
    "EmbedWorker",
    "Embedder",
    "OpenAIEmbedder",
    "PageMessage",
    "SessionHit",
    "SessionPage",
    "SessionSearch",
    "SessionSearchError",
    "Snippet",
]
