"""The schedule of a job: natural language, cron, checks, and fire times (Section 12.1)."""

from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta

import pytest

from sama.scheduler import schedule as sched
from sama.scheduler.jobs import preview
from sama.scheduler.spec import JobError

NOW = datetime(2026, 11, 20, 10, 30, tzinfo=UTC)  # a Friday
IST = "Asia/Kolkata"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("every weekday at 9am", {"kind": "cron", "expr": "0 9 * * 1-5", "tz": IST}),
        ("0 9 * * 1-5", {"kind": "cron", "expr": "0 9 * * 1-5", "tz": IST}),
        ("daily at noon", {"kind": "cron", "expr": "0 12 * * *", "tz": IST}),
        ("every monday, wednesday at 18:30", {"kind": "cron", "expr": "30 18 * * 1,3", "tz": IST}),
        ("monthly on the 1st at 8am", {"kind": "cron", "expr": "0 8 1 * *", "tz": IST}),
        ("weekends at 10", {"kind": "cron", "expr": "0 10 * * 0,6", "tz": IST}),
        ("every 2 hours", {"kind": "interval", "every_s": 7200}),
        ("hourly", {"kind": "interval", "every_s": 3600}),
    ],
)
def test_natural_language_becomes_a_schedule(text: str, expected: dict[str, object]) -> None:
    assert sched.parse(text, tz=IST, now=NOW) == expected


def test_a_one_time_phrase_gives_a_fixed_time() -> None:
    in_30 = sched.parse("in 30 minutes", tz=IST, now=NOW)
    assert in_30["kind"] == "once"
    assert datetime.fromisoformat(in_30["at"]) == NOW + timedelta(minutes=30)
    tomorrow = sched.parse("tomorrow at 7pm", tz=IST, now=NOW)
    # 19:00 in Kolkata is 13:30 UTC.
    assert datetime.fromisoformat(tomorrow["at"]) == datetime(2026, 11, 21, 13, 30, tzinfo=UTC)


def test_the_next_three_fire_times_are_in_the_time_zone() -> None:
    schedule = sched.parse("every weekday at 9am", tz=IST, now=NOW)
    fires = sched.next_fires(schedule, NOW)
    # Friday 10:30 UTC is after 9:00 in Kolkata, so the next fire is on Monday.
    assert fires == [
        datetime(2026, 11, 23, 3, 30, tzinfo=UTC),
        datetime(2026, 11, 24, 3, 30, tzinfo=UTC),
        datetime(2026, 11, 25, 3, 30, tzinfo=UTC),
    ]


def test_preview_shows_the_text_and_three_fire_times() -> None:
    shown = preview("every day at 8am", IST)
    assert shown["schedule"] == {"kind": "cron", "expr": "0 8 * * *", "tz": IST}
    assert len(shown["next_fires"]) == sched.PREVIEW_COUNT
    assert "0 8 * * *" in shown["text"]


@pytest.mark.parametrize(
    "text",
    [
        "every 1 minute",
        "*/1 * * * *",
        "61 * * * *",
        "0 9 * * 8",
        "whenever you like",
        "0 0 31 2 *",
    ],
)
def test_a_bad_schedule_is_refused(text: str) -> None:
    with pytest.raises(sched.ScheduleError):
        sched.parse(text, tz=IST, now=NOW)


def test_an_unknown_time_zone_is_refused() -> None:
    with pytest.raises(sched.ScheduleError):
        sched.parse("daily at 9am", tz="Mars/Olympus", now=NOW)
    with pytest.raises(JobError):
        preview("daily at 9am", "Mars/Olympus")


def test_a_time_in_the_past_is_refused() -> None:
    with pytest.raises(sched.ScheduleError):
        sched.validate({"kind": "once", "at": "2020-01-01T00:00:00+00:00"}, now=NOW)


def test_an_interval_counts_from_the_last_fire() -> None:
    schedule = {"kind": "interval", "every_s": 3600}
    last = NOW - timedelta(minutes=10)
    assert sched.next_fire(schedule, NOW, last=last) == last + timedelta(hours=1)
    # A missed fire catches up once: the next fire is now, not one for each missed hour.
    long_ago = NOW - timedelta(hours=5)
    caught_up = sched.next_fire(schedule, NOW, last=long_ago)
    assert caught_up is not None
    assert caught_up <= NOW + timedelta(hours=1)


def test_a_one_time_schedule_has_no_fire_after_its_time() -> None:
    schedule = sched.validate({"kind": "once", "at": "2026-12-01T09:00:00+00:00"}, now=NOW)
    at = datetime(2026, 12, 1, 9, tzinfo=UTC)
    assert sched.next_fire(schedule, NOW) == at
    assert sched.next_fire(schedule, at, last=at) is None


def test_the_clock_change_does_not_skip_a_day() -> None:
    schedule = {"kind": "cron", "expr": "30 2 * * *", "tz": "Europe/Berlin"}
    # 2:30 does not exist on 2027-03-28 in Berlin. The job fires on the days around it.
    fires = sched.next_fires(schedule, datetime(2027, 3, 26, 12, tzinfo=UTC), 3)
    assert len(fires) == 3
    assert all(later > earlier for earlier, later in itertools.pairwise(fires))
