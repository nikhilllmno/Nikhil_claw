"""Memory: short notes that persist between sessions (docs/architecture.md, Section 8)."""

from sama.memory.core import (
    CAPS,
    MAX_OPERATIONS,
    MAX_REASON_CHARS,
    Action,
    MemoryChangeError,
    MemoryDeniedError,
    Operation,
    Target,
    render_snapshot,
)
from sama.memory.service import MemoryService, scope_of
from sama.memory.store import (
    InProcessMemoryStore,
    MemoryScope,
    MemoryState,
    MemoryStore,
    PostgresMemoryStore,
    Revision,
    RunContext,
    Writer,
    load_snapshot,
)

__all__ = [
    "CAPS",
    "MAX_OPERATIONS",
    "MAX_REASON_CHARS",
    "Action",
    "InProcessMemoryStore",
    "MemoryChangeError",
    "MemoryDeniedError",
    "MemoryScope",
    "MemoryService",
    "MemoryState",
    "MemoryStore",
    "Operation",
    "PostgresMemoryStore",
    "Revision",
    "RunContext",
    "Target",
    "Writer",
    "load_snapshot",
    "render_snapshot",
    "scope_of",
]
