"""The rules of the curator without a database (docs/architecture.md, Section 9.7)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from sama.curator.policy import (
    ARCHIVE_AFTER,
    STALE_AFTER,
    STALE_NOTICE,
    SkillState,
    curator_schedule,
    decide,
    entry_ages,
    names_text,
    percent,
    skill_names,
)
from sama.scheduler.schedule import next_fire

NOW = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)


def state(
    status: str = "active",
    *,
    idle: timedelta = timedelta(0),
    stale_for: timedelta | None = None,
    pinned: bool = False,
    exempt: bool = False,
) -> SkillState:
    return SkillState(
        skill_id=uuid.uuid4(),
        scope="workspace",
        name="notes",
        status=status,
        pinned=pinned,
        exempt=exempt,
        last_active=NOW - idle,
        stale_since=None if stale_for is None else NOW - stale_for,
    )


@pytest.mark.parametrize(
    ("skill", "expected"),
    [
        (state(idle=STALE_AFTER - timedelta(seconds=1)), "keep"),
        (state(idle=STALE_AFTER), "stale"),
        (state(idle=ARCHIVE_AFTER * 2), "stale"),
        (state(idle=ARCHIVE_AFTER, pinned=True), "keep"),
        (state(idle=ARCHIVE_AFTER, exempt=True), "keep"),
        (state("stale", idle=STALE_AFTER, stale_for=timedelta(days=1)), "keep"),
        (state("stale", idle=timedelta(days=1), stale_for=timedelta(days=40)), "revive"),
        (state("stale", idle=ARCHIVE_AFTER, stale_for=STALE_NOTICE, pinned=True), "revive"),
        (state("stale", idle=ARCHIVE_AFTER, stale_for=STALE_NOTICE, exempt=True), "revive"),
        (state("stale", idle=ARCHIVE_AFTER, stale_for=STALE_NOTICE), "archive"),
        (
            state("stale", idle=ARCHIVE_AFTER, stale_for=STALE_NOTICE - timedelta(seconds=1)),
            "keep",
        ),
        (
            state("stale", idle=ARCHIVE_AFTER - timedelta(seconds=1), stale_for=STALE_NOTICE),
            "keep",
        ),
    ],
)
def test_decide_moves_a_skill_from_active_to_stale_to_archived(
    skill: SkillState, expected: str
) -> None:
    assert decide(skill, NOW) == expected


def test_a_stale_skill_is_never_archived_in_the_week_that_it_became_stale() -> None:
    """The owner gets at least one report that names a skill as stale before the archive."""
    skill = state(idle=ARCHIVE_AFTER * 3)
    assert decide(skill, NOW) == "stale"
    marked = state("stale", idle=ARCHIVE_AFTER * 3, stale_for=timedelta(0))
    assert decide(marked, NOW) == "keep"
    week_later = state("stale", idle=ARCHIVE_AFTER * 3, stale_for=STALE_NOTICE)
    assert decide(week_later, NOW) == "archive"


def test_skill_names_finds_every_skills_list_at_any_depth() -> None:
    definition = {
        "steps": [
            {"id": "a", "skills": ["triage", "notes"]},
            {"id": "b", "agent": {"skills": ["deploy", 3]}},
            {"id": "c", "skills": "not-a-list"},
        ],
        "skills": ["top"],
    }
    assert skill_names(definition) == {"triage", "notes", "deploy", "top"}
    assert skill_names(None) == set()


def test_entry_ages_follow_the_unbroken_run_of_revisions() -> None:
    day = timedelta(days=1)
    revisions = [
        (NOW - day, ["a", "b", "c"]),
        (NOW - 2 * day, ["a", "b"]),
        (NOW - 3 * day, ["a"]),
        (NOW - 4 * day, ["b"]),
    ]
    assert entry_ages(["a", "b", "c", "d"], revisions) == [
        NOW - 3 * day,
        NOW - 2 * day,
        NOW - day,
        None,
    ]
    assert entry_ages(["a"], []) == [None]


def test_the_schedule_is_weekly_on_monday_and_differs_by_workspace() -> None:
    schedules = {curator_schedule(uuid.uuid4())["expr"] for _ in range(20)}
    assert len(schedules) > 1
    workspace = uuid.uuid4()
    schedule = curator_schedule(workspace)
    assert schedule == curator_schedule(workspace)
    assert schedule["tz"] == "UTC"
    assert schedule["expr"].endswith(" * * 1")
    first = next_fire(schedule, NOW)
    assert first is not None
    assert first.weekday() == 0
    second = next_fire(schedule, first)
    assert second is not None
    assert second - first == timedelta(days=7)


def test_helpers() -> None:
    assert percent(1100, 2200) == 50
    assert percent(1, 0) == 0
    assert names_text(["b", "a"]) == "a, b"
    assert names_text([str(n) for n in range(5)], limit=2) == "0, 1, and 3 more"
