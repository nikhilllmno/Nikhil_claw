"""Context management: compaction records, the transcript archive, session lineage, and context
pins (docs/architecture.md, Section 6.3)."""

from sama.context.compaction import ArchiveError, ContextArchive, list_compactions, record_compacted
from sama.context.lineage import Link, ancestors, children
from sama.context.pins import (
    MAX_PIN_CHARS,
    MAX_PINNED_CHARS,
    MAX_PINS,
    Pin,
    PinError,
    PinStore,
    pin_block,
)

__all__ = [
    "MAX_PINNED_CHARS",
    "MAX_PINS",
    "MAX_PIN_CHARS",
    "ArchiveError",
    "ContextArchive",
    "Link",
    "Pin",
    "PinError",
    "PinStore",
    "ancestors",
    "children",
    "list_compactions",
    "pin_block",
    "record_compacted",
]
