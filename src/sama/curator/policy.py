"""The rules of the curator, without a database (docs/architecture.md, Section 9.7).

The curator manages only the skills that an agent wrote. A skill moves from ``active`` to
``stale`` after ``STALE_AFTER`` without a view, a use, or a change. A stale skill moves to
``archived`` after ``ARCHIVE_AFTER`` without activity, and only when it was stale for at least
``STALE_NOTICE``, so the owner gets one report before the archive. Any activity, a pin, or a
job or workflow that names the skill makes it ``active`` again. Nothing is deleted.

Memory is only reported. It is always in the prompt and has no usage signal of its own, so the
curator does not change it.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final, Literal

STALE_AFTER: Final = timedelta(days=30)
ARCHIVE_AFTER: Final = timedelta(days=90)
STALE_NOTICE: Final = timedelta(days=7)
"""The shortest time between ``stale`` and ``archived``: one weekly report."""
MEMORY_OLD_AFTER: Final = timedelta(days=90)
"""A memory entry that did not change for this long is in the report as old."""
MEMORY_FULL_RATIO: Final = 0.9
"""A memory scope that uses this part of its cap or more is in the report as nearly full."""
MAX_MEMORY_REVISIONS: Final = 200
"""The curator reads at most this many revisions of one memory to find the age of entries."""
CURATOR_NAME: Final = "Curator"

Decision = Literal["keep", "stale", "archive", "revive"]


@dataclass(frozen=True, slots=True)
class SkillState:
    """What the curator knows about one agent-written skill."""

    skill_id: uuid.UUID
    scope: str
    name: str
    status: str
    pinned: bool
    exempt: bool
    """A job or a workflow names the skill."""
    last_active: datetime
    stale_since: datetime | None


def decide(skill: SkillState, now: datetime) -> Decision:
    """The next status of a skill. ``archived`` skills are not given."""
    idle = now - skill.last_active
    protected = skill.pinned or skill.exempt
    if skill.status == "stale":
        if protected or idle < STALE_AFTER:
            return "revive"
        since = skill.stale_since or now
        if idle >= ARCHIVE_AFTER and now - since >= STALE_NOTICE:
            return "archive"
        return "keep"
    if not protected and idle >= STALE_AFTER:
        return "stale"
    return "keep"


def skill_names(value: Any) -> set[str]:
    """The skill names in a job or a workflow definition: each string in a ``skills`` list,
    at any depth."""
    found: set[str] = set()
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            for key, child in item.items():
                if key == "skills" and isinstance(child, list):
                    found.update(name for name in child if isinstance(name, str))
                else:
                    stack.append(child)
        elif isinstance(item, list):
            stack.extend(item)
    return found


def entry_ages(
    entries: Sequence[str], revisions: Sequence[tuple[datetime, Sequence[str]]]
) -> list[datetime | None]:
    """The time from which each current entry has been in memory without a change.
    ``revisions`` are the newest first: ``(created_at, entries)``. The time is the oldest
    revision of the unbroken run of revisions that have the entry. ``None`` means no revision
    has the entry."""
    ages: list[datetime | None] = []
    for entry in entries:
        since: datetime | None = None
        for created_at, listed in revisions:
            if entry not in listed:
                break
            since = created_at
        ages.append(since)
    return ages


def curator_schedule(workspace_id: uuid.UUID) -> dict[str, Any]:
    """A weekly cron on Monday. The minute and the hour come from the workspace id, so the
    curator jobs of many workspaces do not all fire at the same time."""
    digest = int.from_bytes(hashlib.sha256(workspace_id.bytes).digest()[:4], "big")
    minute, hour = digest % 60, (digest // 60) % 24
    return {"kind": "cron", "expr": f"{minute} {hour} * * 1", "tz": "UTC"}


def percent(used: int, cap: int) -> int:
    return round(100 * used / cap) if cap else 0


def names_text(names: Iterable[str], limit: int = 20) -> str:
    listed = sorted(names)
    text = ", ".join(listed[:limit])
    if len(listed) > limit:
        text += f", and {len(listed) - limit} more"
    return text
