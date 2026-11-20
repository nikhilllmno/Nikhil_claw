"""The schedule of a job (docs/architecture.md, Section 12.1).

A schedule is a JSON object in ``jobs.schedule``. It has one of three kinds:

- ``{"kind": "cron", "expr": "0 9 * * 1-5", "tz": "Asia/Kolkata"}``: a cron expression with
  five fields (minute, hour, day of month, month, day of week) in a time zone.
- ``{"kind": "interval", "every_s": 3600}``: a fixed interval after the last fire.
- ``{"kind": "once", "at": "2026-12-01T09:00:00+05:30"}``: one time.

``parse`` turns a short English text ("every weekday at 9am", "in 2 hours") or a cron
expression into a schedule. ``validate`` checks a schedule, and ``next_fire`` gives the next
fire time after a time. The code uses ``zoneinfo`` and no other library.
"""

from __future__ import annotations

import itertools
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MIN_INTERVAL_S: Final = 300
"""The shortest time between two fires. A shorter schedule is not valid."""
MAX_INTERVAL_S: Final = 366 * 24 * 3600
SEARCH_DAYS: Final = 366 * 5
"""How far ``next_fire`` looks for a cron match. ``0 0 29 2 *`` matches once in 4 years."""
PREVIEW_COUNT: Final = 3
DST_CHECK_DAYS: Final = 400
"""How far ``validate`` looks for daylight saving changes when it checks the gaps of a cron
schedule."""
DEFAULT_TZ: Final = "UTC"

MONTHS: Final = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
DAYS: Final = ("sun", "mon", "tue", "wed", "thu", "fri", "sat")
DAY_WORDS: Final = {
    "sunday": 0,
    "monday": 1,
    "tuesday": 2,
    "wednesday": 3,
    "thursday": 4,
    "friday": 5,
    "saturday": 6,
    **{name: index for index, name in enumerate(DAYS)},
    "tues": 2,
    "thur": 4,
    "thurs": 4,
}


class ScheduleError(ValueError):
    """The schedule is not valid. The message is safe to show to the user."""


@dataclass(frozen=True, slots=True)
class _Field:
    low: int
    high: int
    names: tuple[str, ...] = ()
    name_base: int = 0


_FIELDS: Final = (
    _Field(0, 59),
    _Field(0, 23),
    _Field(1, 31),
    _Field(1, 12, MONTHS, 1),
    _Field(0, 7, DAYS, 0),
)
_FIELD_NAMES: Final = ("minute", "hour", "day of month", "month", "day of week")


@dataclass(frozen=True, slots=True)
class Cron:
    """A parsed cron expression. ``dom_any`` and ``dow_any`` keep the classic rule: when both
    day fields are restricted, a day matches when one of them matches."""

    minutes: tuple[int, ...]
    hours: tuple[int, ...]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]
    dom_any: bool
    dow_any: bool

    def day_matches(self, day: date) -> bool:
        if day.month not in self.months:
            return False
        dom = day.day in self.days
        dow = (day.isoweekday() % 7) in self.weekdays
        if self.dom_any and self.dow_any:
            return True
        if self.dom_any:
            return dow
        if self.dow_any:
            return dom
        return dom or dow


def _value(text: str, spec: _Field, position: str) -> int:
    lowered = text.lower()
    if spec.names and lowered in spec.names:
        return spec.names.index(lowered) + spec.name_base
    if not text.isdigit():
        raise ScheduleError(f"the {position} field has a value that is not valid: {text!r}")
    number = int(text)
    if not spec.low <= number <= spec.high:
        raise ScheduleError(
            f"the {position} field must be from {spec.low} to {spec.high}, not {number}"
        )
    return number


def _field(text: str, spec: _Field, position: str) -> tuple[set[int], bool]:
    values: set[int] = set()
    for part in text.split(","):
        base, slash, step_text = part.partition("/")
        step = 1
        if slash:
            if not step_text.isdigit() or int(step_text) < 1:
                raise ScheduleError(f"the {position} field has a step that is not valid")
            step = int(step_text)
        if base == "*":
            low, high = spec.low, spec.high
        elif "-" in base:
            first, _, last = base.partition("-")
            low, high = _value(first, spec, position), _value(last, spec, position)
            if low > high:
                raise ScheduleError(f"the {position} field has a range that is not valid")
        else:
            low = _value(base, spec, position)
            high = spec.high if slash else low
        values.update(range(low, high + 1, step))
    return values, text == "*"


def parse_cron(expr: str) -> Cron:
    parts = expr.split()
    if len(parts) != 5:
        raise ScheduleError("a cron expression has 5 fields: minute hour day month weekday")
    parsed = [_field(p, s, n) for p, s, n in zip(parts, _FIELDS, _FIELD_NAMES, strict=True)]
    weekdays = {day % 7 for day in parsed[4][0]}
    return Cron(
        minutes=tuple(sorted(parsed[0][0])),
        hours=tuple(sorted(parsed[1][0])),
        days=frozenset(parsed[2][0]),
        months=frozenset(parsed[3][0]),
        weekdays=frozenset(weekdays),
        dom_any=parsed[2][1],
        dow_any=parsed[4][1],
    )


def zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ScheduleError(f"the time zone {name!r} is not known") from None


def _local(day: date, hour: int, minute: int, tz: ZoneInfo) -> datetime | None:
    """The time in ``tz``, or ``None`` when the clock skips it (a daylight saving gap)."""
    wall = datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz)
    back = wall.astimezone(UTC).astimezone(tz)
    if (back.hour, back.minute, back.date()) != (hour, minute, day):
        return None
    return wall


def _next_cron(cron: Cron, tz: ZoneInfo, after: datetime) -> datetime | None:
    start = after.astimezone(tz).date()
    for offset in range(SEARCH_DAYS):
        day = start + timedelta(days=offset)
        if not cron.day_matches(day):
            continue
        for hour in cron.hours:
            for minute in cron.minutes:
                when = _local(day, hour, minute, tz)
                if when is not None and when > after:
                    return when.astimezone(UTC)
    return None


def _aware(value: str, tz: ZoneInfo) -> datetime:
    try:
        when = datetime.fromisoformat(value)
    except ValueError:
        raise ScheduleError(f"the time {value!r} is not an ISO 8601 time") from None
    if when.tzinfo is None:
        when = when.replace(tzinfo=tz)
    return when.astimezone(UTC)


def validate(schedule: Any, *, now: datetime | None = None) -> dict[str, Any]:
    """The schedule in its stored form. Raises ``ScheduleError`` with a reason for the user.

    A ``once`` schedule must be in the future. Every schedule must leave ``MIN_INTERVAL_S``
    between its fires, and must have a next fire."""
    if not isinstance(schedule, dict):
        raise ScheduleError("the schedule must be an object")
    now = now or datetime.now(UTC)
    kind = schedule.get("kind")
    tz_name = schedule.get("tz") or DEFAULT_TZ
    if not isinstance(tz_name, str):
        raise ScheduleError("tz must be a time zone name")
    tz = zone(tz_name)
    stored: dict[str, Any]
    if kind == "cron":
        expr = schedule.get("expr")
        if not isinstance(expr, str):
            raise ScheduleError("a cron schedule needs expr")
        parse_cron(expr)
        stored = {"kind": "cron", "expr": " ".join(expr.split()), "tz": tz_name}
    elif kind == "interval":
        every = schedule.get("every_s")
        if not isinstance(every, int) or isinstance(every, bool):
            raise ScheduleError("an interval schedule needs every_s, a number of seconds")
        if every > MAX_INTERVAL_S:
            raise ScheduleError("the interval must be one year or less")
        stored = {"kind": "interval", "every_s": every}
    elif kind == "once":
        at = schedule.get("at")
        if not isinstance(at, str):
            raise ScheduleError("a once schedule needs at, an ISO 8601 time")
        when = _aware(at, tz)
        if when <= now:
            raise ScheduleError("the time of a once schedule must be in the future")
        stored = {"kind": "once", "at": when.isoformat(), "tz": tz_name}
    else:
        raise ScheduleError("the schedule kind must be cron, interval, or once")
    if stored["kind"] == "interval" and stored["every_s"] < MIN_INTERVAL_S:
        raise ScheduleError(f"jobs can fire at most once in {MIN_INTERVAL_S // 60} minutes")
    first = next_fire(stored, now)
    if first is None:
        raise ScheduleError("the schedule never fires")
    if stored["kind"] == "cron":
        _check_spacing(stored, first)
    return stored


def _check_spacing(schedule: dict[str, Any], first: datetime) -> None:
    """Checks that a cron schedule leaves ``MIN_INTERVAL_S`` between each two fires.

    The minute and hour fields give the same wall-clock times on each day that matches. The
    check assumes that each day matches, so it also checks the gap from the last time of a
    day to the first time of the next day. This is strict when two days in a row never
    match, but no calendar case (for example day 31 or a Monday) can get past it. A daylight
    saving change can make a wall-clock gap shorter in real time, so the check also takes
    the real times of the days around each change in the next ``DST_CHECK_DAYS`` days."""
    cron = parse_cron(schedule["expr"])
    tz = zone(schedule["tz"])
    times = sorted(hour * 60 + minute for hour in cron.hours for minute in cron.minutes)
    gaps = [later - earlier for earlier, later in itertools.pairwise(times)]
    gaps.append(times[0] + 24 * 60 - times[-1])
    if min(gaps) * 60 < MIN_INTERVAL_S:
        raise ScheduleError(f"jobs can fire at most once in {MIN_INTERVAL_S // 60} minutes")
    start = first.astimezone(tz).date()
    for offset in range(DST_CHECK_DAYS):
        day = start + timedelta(days=offset)
        if _noon_offset(day, tz) == _noon_offset(day + timedelta(days=1), tz):
            continue
        moments = sorted(
            when.astimezone(UTC)
            for near in (day, day + timedelta(days=1))
            for hour in cron.hours
            for minute in cron.minutes
            if (when := _local(near, hour, minute, tz)) is not None
        )
        for earlier, later in itertools.pairwise(moments):
            if (later - earlier).total_seconds() < MIN_INTERVAL_S:
                raise ScheduleError(f"jobs can fire at most once in {MIN_INTERVAL_S // 60} minutes")


def _noon_offset(day: date, tz: ZoneInfo) -> timedelta | None:
    return datetime(day.year, day.month, day.day, 12, tzinfo=tz).utcoffset()


def next_fire(
    schedule: dict[str, Any], after: datetime, *, last: datetime | None = None
) -> datetime | None:
    """The first fire time after ``after``, in UTC, or ``None`` when there is no more fire.
    For an interval schedule, ``last`` is the time of the last fire."""
    kind = schedule["kind"]
    if kind == "cron":
        return _next_cron(parse_cron(schedule["expr"]), zone(schedule["tz"]), after)
    if kind == "interval":
        step = timedelta(seconds=int(schedule["every_s"]))
        if last is None:
            return after + step
        when = last + step
        # After a missed fire (the scheduler was down), the interval starts again now. The
        # scheduler fires a missed time once, so a long stop gives one fire and not many.
        return when if when > after else after + step
    when = datetime.fromisoformat(schedule["at"]).astimezone(UTC)
    return when if when > after else None


def next_fires(
    schedule: dict[str, Any], after: datetime, count: int = PREVIEW_COUNT
) -> list[datetime]:
    """The next ``count`` fire times after ``after``."""
    fires: list[datetime] = []
    current = after
    last: datetime | None = None
    while len(fires) < count:
        when = next_fire(schedule, current, last=last)
        if when is None:
            break
        fires.append(when)
        current, last = when, when
    return fires


# -- natural language --------------------------------------------------------------------------

_UNITS: Final = {
    "s": 1,
    "sec": 1,
    "secs": 1,
    "second": 1,
    "seconds": 1,
    "m": 60,
    "min": 60,
    "mins": 60,
    "minute": 60,
    "minutes": 60,
    "h": 3600,
    "hr": 3600,
    "hrs": 3600,
    "hour": 3600,
    "hours": 3600,
    "d": 86400,
    "day": 86400,
    "days": 86400,
    "w": 604800,
    "week": 604800,
    "weeks": 604800,
}
_TIME: Final = r"(?P<time>noon|midnight|\d{1,2}(?::\d{2})?\s*(?:am|pm)?)"
_AMOUNT: Final = r"(?P<n>\d+|an?|one)?\s*(?P<unit>[a-z]+)"


def _time_of(text: str | None) -> tuple[int, int]:
    if text is None:
        return 9, 0
    text = text.strip().lower().replace(" ", "")
    if text == "noon":
        return 12, 0
    if text == "midnight":
        return 0, 0
    match = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?(am|pm)?", text)
    if match is None:
        raise ScheduleError(f"the time {text!r} is not valid")
    hour, minute, half = int(match[1]), int(match[2] or 0), match[3]
    if half is not None:
        if not 1 <= hour <= 12:
            raise ScheduleError(f"the time {text!r} is not valid")
        hour = hour % 12 + (12 if half == "pm" else 0)
    if hour > 23 or minute > 59:
        raise ScheduleError(f"the time {text!r} is not valid")
    return hour, minute


def _seconds(amount: str | None, unit: str) -> int:
    if unit not in _UNITS:
        raise ScheduleError(f"the unit {unit!r} is not known")
    count = 1 if amount in (None, "a", "an", "one") else int(str(amount))
    return count * _UNITS[unit]


def _days(text: str) -> list[int]:
    found: list[int] = []
    for raw in re.split(r"[\s,]+", text):
        word = raw if raw in DAY_WORDS else raw.removesuffix("s")
        if not word or word == "and":
            continue
        if word not in DAY_WORDS:
            raise ScheduleError(f"{word!r} is not a day of the week")
        found.append(DAY_WORDS[word])
    if not found:
        raise ScheduleError("name a day of the week")
    return sorted(set(found))


def parse(text: str, *, tz: str = DEFAULT_TZ, now: datetime | None = None) -> dict[str, Any]:
    """A schedule from a short English text or a cron expression. The result is valid.

    Examples: ``every 30 minutes``, ``hourly``, ``daily at 9am``, ``every weekday at 9:30``,
    ``every monday and thursday at 18:00``, ``monthly on the 1st at 8am``, ``in 2 hours``,
    ``tomorrow at 7pm``, ``at 2026-12-01 09:00``, ``0 9 * * 1-5``."""
    now = now or datetime.now(UTC)
    zone(tz)
    phrase = " ".join(text.strip().lower().split())
    if not phrase:
        raise ScheduleError("the schedule is empty")
    schedule = _parse_phrase(phrase, tz, now)
    return validate(schedule, now=now)


def _cron(minute: int, hour: int, day: str = "*", weekday: str = "*") -> str:
    return f"{minute} {hour} {day} * {weekday}"


def _parse_phrase(phrase: str, tz: str, now: datetime) -> dict[str, Any]:
    if len(phrase.split()) == 5 and re.fullmatch(r"[\w*/,\-]+( [\w*/,\-]+){4}", phrase):
        return {"kind": "cron", "expr": phrase, "tz": tz}
    phrase = re.sub(r"^(run |fire )", "", phrase)
    at = rf"(?:\s+at\s+{_TIME})?"
    if match := re.fullmatch(rf"in {_AMOUNT}", phrase):
        when = now + timedelta(seconds=_seconds(match["n"], match["unit"]))
        return {"kind": "once", "at": when.isoformat(), "tz": tz}
    if match := re.fullmatch(rf"every {_AMOUNT}", phrase):
        unit = match["unit"]
        if unit in DAY_WORDS or unit in ("weekday", "weekdays", "day", "weekend"):
            pass
        else:
            return {"kind": "interval", "every_s": _seconds(match["n"], unit)}
    if phrase in ("hourly", "every hour"):
        return {"kind": "interval", "every_s": 3600}
    if match := re.fullmatch(rf"(?:daily|every day|each day){at}", phrase):
        hour, minute = _time_of(match["time"])
        return {"kind": "cron", "expr": _cron(minute, hour), "tz": tz}
    if match := re.fullmatch(rf"(?:every |on )?weekdays?{at}", phrase):
        hour, minute = _time_of(match["time"])
        return {"kind": "cron", "expr": _cron(minute, hour, weekday="1-5"), "tz": tz}
    if match := re.fullmatch(rf"(?:every |on )?weekends?{at}", phrase):
        hour, minute = _time_of(match["time"])
        return {"kind": "cron", "expr": _cron(minute, hour, weekday="0,6"), "tz": tz}
    if match := re.fullmatch(rf"(?:weekly|every week)(?: on (?P<days>[a-z ,]+?))?{at}", phrase):
        hour, minute = _time_of(match["time"])
        days = _days(match["days"]) if match["days"] else [1]
        weekday = ",".join(str(day) for day in days)
        return {"kind": "cron", "expr": _cron(minute, hour, weekday=weekday), "tz": tz}
    if match := re.fullmatch(rf"(?:every |on )(?P<days>[a-z ,]+?){at}", phrase):
        weekday = ",".join(str(day) for day in _days(match["days"]))
        hour, minute = _time_of(match["time"])
        return {"kind": "cron", "expr": _cron(minute, hour, weekday=weekday), "tz": tz}
    if match := re.fullmatch(
        rf"(?:monthly|every month)(?: on (?:the |day )?(?P<dom>\d{{1,2}})(?:st|nd|rd|th)?)?{at}",
        phrase,
    ):
        hour, minute = _time_of(match["time"])
        dom = int(match["dom"] or 1)
        if not 1 <= dom <= 31:
            raise ScheduleError("the day of the month must be from 1 to 31")
        return {"kind": "cron", "expr": _cron(minute, hour, day=str(dom)), "tz": tz}
    if match := re.fullmatch(rf"(?P<day>today|tomorrow){at}", phrase):
        hour, minute = _time_of(match["time"])
        local = now.astimezone(zone(tz)).date()
        day = local + timedelta(days=1 if match["day"] == "tomorrow" else 0)
        moment = _local(day, hour, minute, zone(tz))
        if moment is None:
            raise ScheduleError("that time does not exist in the time zone")
        return {"kind": "once", "at": moment.isoformat(), "tz": tz}
    if match := re.fullmatch(
        rf"(?:at |on )?(?P<date>\d{{4}}-\d{{2}}-\d{{2}})(?:[ t](?:at )?{_TIME})?", phrase
    ):
        try:
            day = date.fromisoformat(match["date"])
        except ValueError:
            raise ScheduleError(f"the date {match['date']!r} is not valid") from None
        hour, minute = _time_of(match["time"])
        moment = _local(day, hour, minute, zone(tz))
        if moment is None:
            raise ScheduleError("that time does not exist in the time zone")
        return {"kind": "once", "at": moment.isoformat(), "tz": tz}
    raise ScheduleError(
        "sama did not understand the schedule. Try 'every 2 hours', 'daily at 9am', "
        "'every weekday at 9:30', 'every monday at 18:00', 'in 30 minutes', "
        "'at 2026-12-01 09:00', or a cron expression such as '0 9 * * 1-5'."
    )


def describe(schedule: dict[str, Any]) -> str:
    """A short text for the user."""
    kind = schedule.get("kind")
    if kind == "cron":
        return f"cron {schedule['expr']} ({schedule['tz']})"
    if kind == "interval":
        every = int(schedule["every_s"])
        for size, name in ((86400, "day"), (3600, "hour"), (60, "minute")):
            if every % size == 0:
                count = every // size
                return f"every {count} {name}{'s' if count != 1 else ''}"
        return f"every {every} seconds"
    if kind == "once":
        return f"once at {schedule['at']}"
    return "not valid"
