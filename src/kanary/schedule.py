"""Cron schedules for sources: Unix cron, with the common extensions of other implementations.

Accepted, besides the five fields of Unix cron (minute hour day-of-month month day-of-week):

- names of months and weekdays, abbreviated or not, in any case: JAN, january, mon, Tues
- 0 and 7 are both Sunday; "5/15" is "5-59/15"; a range may wrap ("22-2", "FRI-MON")
- "?" in the day fields is "*"
- "L" (last day of the month) and "L-3" (three days before it) in the day of the month
- "5L" / "FRIL" (the last Friday of the month) and "MON#1" (the first Monday) in the weekday
- a time zone in front: "CRON_TZ=Asia/Tokyo 0 9 * * *" or "TZ=UTC ..." (anything kanary.to_timezone() reads)
- the macros @yearly, @annually, @monthly, @weekly, @daily, @midnight, @hourly

As in cron, when both day fields are restricted (neither starts with "*" or "?"), a day
matches when either does: "0 0 1 * 1" runs on the 1st and on every Monday. "@every 5m"
is not a schedule but an interval; the source decorator reads it as interval = 300.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
import re

from .units import parse_duration
from .values import to_timezone


_MACROS = {
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
    "@monthly": "0 0 1 * *",
    "@weekly": "0 0 * * 0",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
}
_MONTHS = ("JANUARY", "FEBRUARY", "MARCH", "APRIL", "MAY", "JUNE", "JULY", "AUGUST", "SEPTEMBER", "OCTOBER", "NOVEMBER", "DECEMBER")
_WEEKDAYS = ("SUNDAY", "MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY")
# The Gregorian calendar repeats every 400 years: a schedule that does not run within
# them never runs ("0 9 * FEB MON#5", a fifth Monday in February, runs next in 2044).
_HORIZON = timedelta(days=146_097)
_EVERY = re.compile(r"^@every(?:\s+(.*))?$", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class CronSchedule:
    expression: str
    minutes: frozenset[int]
    hours: frozenset[int]
    days_of_month: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]  # Sunday = 0
    any_day_of_month: bool = True
    any_weekday: bool = True
    days_before_last: frozenset[int] = frozenset()  # "L" is 0, "L-3" is 3
    last_weekdays: frozenset[int] = frozenset()  # "5L": the last Friday of the month
    nth_weekdays: frozenset[tuple[int, int]] = frozenset()  # "MON#1": (1, 1)
    timezone: tzinfo | None = None  # from CRON_TZ= / TZ=; None: the host's local time
    never: bool = False  # no such day (Feb 30): the source is not polled

    @classmethod
    def parse(cls, expression: str) -> "CronSchedule":
        if not isinstance(expression, str) or not expression.strip():
            raise ValueError("schedule must be a non-empty cron-like string")
        text = expression.strip()
        zone = None
        first, _, rest = text.partition(" ")
        key, equals, value = first.partition("=")
        if equals and key.strip().upper() in {"CRON_TZ", "TZ"}:
            try:
                zone = to_timezone(value)
            except ValueError as exc:
                raise ValueError(f"schedule time zone: {exc}") from None
            text = rest.strip()
        if _EVERY.match(text):
            raise ValueError(f"{text!r} is an interval: write it as the source's interval (the decorator reads schedule='@every ...' as one)")
        normalized = _MACROS.get(text.lower(), text)
        fields = normalized.split()
        if len(fields) != 5:
            raise ValueError("schedule must use 5 cron fields: minute hour day month weekday")

        minute, hour, day_of_month, month, weekday = fields
        days, days_before_last = _parse_days_of_month(day_of_month)
        weekdays, last_weekdays, nth_weekdays = _parse_weekdays(weekday)
        schedule = cls(
            expression=expression.strip(),
            minutes=frozenset(_parse_field(minute, 0, 59)),
            hours=frozenset(_parse_field(hour, 0, 23)),
            days_of_month=frozenset(days),
            months=frozenset(_parse_field(month, 1, 12, names=_MONTHS, first=1)),
            weekdays=frozenset(weekdays),
            any_day_of_month=day_of_month[0] in "*?",
            any_weekday=weekday[0] in "*?",
            days_before_last=frozenset(days_before_last),
            last_weekdays=frozenset(last_weekdays),
            nth_weekdays=frozenset(nth_weekdays),
            timezone=zone,
        )
        try:
            schedule.next_after(datetime.now().astimezone())
        except ValueError:
            object.__setattr__(schedule, "never", True)
        return schedule

    @property
    def both_day_fields(self) -> bool:
        """Both the day of the month and the weekday are given: either one matching is enough."""
        return not self.any_day_of_month and not self.any_weekday

    def matches(self, dt: datetime) -> bool:
        if self.timezone is not None and dt.tzinfo is not None:
            dt = dt.astimezone(self.timezone)
        return dt.minute in self.minutes and dt.hour in self.hours and self._day_matches(dt)

    def _day_matches(self, dt: datetime) -> bool:
        if dt.month not in self.months:
            return False
        last_day = calendar.monthrange(dt.year, dt.month)[1]
        day_of_month = dt.day in self.days_of_month or (last_day - dt.day) in self.days_before_last
        weekday_number = (dt.weekday() + 1) % 7  # cron counts from Sunday = 0
        weekday = (
            weekday_number in self.weekdays
            or (weekday_number in self.last_weekdays and dt.day + 7 > last_day)
            or (weekday_number, (dt.day - 1) // 7 + 1) in self.nth_weekdays
        )
        if self.any_day_of_month or self.any_weekday:
            return day_of_month and weekday
        return day_of_month or weekday

    def next_after(self, dt: datetime) -> datetime:
        """The first run after dt, in whole minutes.

        The fields are read on the wall clock: of the schedule's own time zone (CRON_TZ=),
        else of this host. A naive dt gives a naive result, read on the wall clock alone.
        An aware dt gives an aware result that is later than dt in real time, also around
        daylight saving time: on the day the clock goes back, a time that occurs twice
        runs once (its first occurrence); on the day it goes forward, a time the clock
        skips runs an hour later (also when KANARY starts within that hour).
        """
        if dt.tzinfo is None and self.timezone is None:
            return self._next_wall_time(dt.replace(second=0, microsecond=0))
        zone = self.timezone
        wall = (dt.astimezone(zone) if zone is not None else dt.astimezone()).replace(tzinfo=None)
        after = dt.astimezone(timezone.utc)
        # From a little earlier on the wall clock: a skipped time (02:30) runs at a wall time
        # after it (03:30), so at 03:10 it is still ahead. Earlier candidates are passed over
        # below, as not later than dt in real time.
        candidate = wall.replace(second=0, microsecond=0) - _CLOCK_CHANGE_LOOKBACK
        while True:
            candidate = self._next_wall_time(candidate)
            real = self._real_time(candidate)
            if real <= after:
                continue
            if not self._exists(candidate) and not self._first_skipped(candidate):
                continue  # of the times the clock skipped, only the first runs
            # As the clock shows it then: a time the clock skipped (02:30) is 03:30.
            return real.astimezone(self.timezone) if self.timezone is not None else real.astimezone()

    def _real_time(self, wall: datetime) -> datetime:
        """The wall time in real time: the zone's rules, or this host's (naive -> local)."""
        moment = wall.replace(tzinfo=self.timezone) if self.timezone is not None else wall.astimezone()
        return moment.astimezone(timezone.utc)

    def _exists(self, wall: datetime) -> bool:
        """False for a wall time the clock skips (02:30 on the day it goes forward)."""
        real = self._real_time(wall)
        shown = real.astimezone(self.timezone) if self.timezone is not None else real.astimezone()
        return shown.replace(tzinfo=None) == wall

    def _first_skipped(self, wall: datetime) -> bool:
        """The first time of the schedule in the stretch the clock skipped."""
        start = wall
        for _ in range(int(_CLOCK_CHANGE_LOOKBACK.total_seconds() // 60)):
            if self._exists(start - timedelta(minutes=1)):
                break
            start -= timedelta(minutes=1)
        return self._next_wall_time(start - timedelta(minutes=1)) == wall

    def _next_wall_time(self, after: datetime) -> datetime:
        """The first wall-clock minute after ``after`` (naive) that matches the fields."""
        candidate = after + timedelta(minutes=1)
        deadline = candidate + _HORIZON
        while candidate <= deadline:
            if candidate.month not in self.months:
                year, month = (candidate.year + 1, 1) if candidate.month == 12 else (candidate.year, candidate.month + 1)
                candidate = candidate.replace(year=year, month=month, day=1, hour=0, minute=0)
            elif not self._day_matches(candidate):
                candidate = candidate.replace(hour=0, minute=0) + timedelta(days=1)
            elif candidate.hour not in self.hours:
                candidate = candidate.replace(minute=0) + timedelta(hours=1)
            elif candidate.minute not in self.minutes:
                candidate += timedelta(minutes=1)
            else:
                return candidate
        raise ValueError(f"schedule {self.expression!r} never runs (no such day)")


# More than any change of a clock for daylight saving time (one hour; two at Troll station).
_CLOCK_CHANGE_LOOKBACK = timedelta(hours=3)


def parse_schedule(expression: str) -> CronSchedule:
    return CronSchedule.parse(expression)


def every_seconds(schedule: object) -> float | None:
    """The seconds of "@every 5m" (also "@every 1h30m", "@every 90s"); None for anything else."""
    if not isinstance(schedule, str):
        return None
    match = _EVERY.match(schedule.strip())
    if match is None:
        return None
    try:
        seconds = parse_duration(match.group(1) or "")
    except ValueError:
        seconds = 0.0
    if seconds <= 0:
        raise ValueError(f"{schedule!r}: write the interval as @every 30s, 5m, 1h30m, or 2d")
    return seconds


def _parse_days_of_month(spec: str) -> tuple[set[int], set[int]]:
    """Days of the month, and "L" / "L-n" as days before the last one."""
    if spec == "?":
        spec = "*"
    days: set[int] = set()
    before_last: set[int] = set()
    for part in _parts(spec):
        last = re.fullmatch(r"L(?:-(\d+))?", part, re.IGNORECASE)
        if last:
            offset = int(last.group(1) or 0)
            if offset > 30:
                raise ValueError(f"cron value {part!r}: L-n counts at most 30 days back")
            before_last.add(offset)
            continue
        days.update(_parse_part(part, 1, 31))
    return days, before_last


def _parse_weekdays(spec: str) -> tuple[set[int], set[int], set[tuple[int, int]]]:
    """Weekdays (Sunday = 0), "5L" / "FRIL" (the last one of the month), "MON#2" (the second one)."""
    if spec == "?":
        spec = "*"
    weekdays: set[int] = set()
    last: set[int] = set()
    nth: set[tuple[int, int]] = set()
    for part in _parts(spec):
        if "#" in part:
            day_text, _, count = part.partition("#")
            if not count.isdigit() or not 1 <= int(count) <= 5:
                raise ValueError(f"cron value {part!r}: write the n-th weekday as MON#1 to MON#5")
            nth.add((_parse_number(day_text, 0, 7, names=_WEEKDAYS, first=0) % 7, int(count)))
            continue
        if len(part) > 1 and part[-1] in "Ll" and "/" not in part and "-" not in part:
            last.add(_parse_number(part[:-1], 0, 7, names=_WEEKDAYS, first=0) % 7)
            continue
        if part.upper() == "L":
            raise ValueError("cron weekday 'L' alone is ambiguous: write the last Friday of the month as 5L or FRIL")
        weekdays.update(value % 7 for value in _parse_part(part, 0, 7, names=_WEEKDAYS, first=0, cycle=7))
    return weekdays, last, nth


def _parse_field(spec: str, minimum: int, maximum: int, *, names: tuple[str, ...] | None = None, first: int = 0) -> set[int]:
    values: set[int] = set()
    for part in _parts(spec):
        values.update(_parse_part(part, minimum, maximum, names=names, first=first))
    return values


def _parts(spec: str) -> list[str]:
    parts = [part.strip() for part in spec.split(",")]
    if not all(parts):
        raise ValueError("empty cron field element is not allowed")
    return parts


def _parse_part(
    part: str,
    minimum: int,
    maximum: int,
    *,
    names: tuple[str, ...] | None = None,
    first: int = 0,
    cycle: int | None = None,
) -> set[int]:
    """One element: "*", "5", "1-5", "22-2" (wraps), each with an optional "/step".

    cycle is the weekdays' 7, where 7 is Sunday again: a range that wraps goes round 0-6.
    """
    base, step = _split_step(part)
    if step <= 0:
        raise ValueError("cron step must be a positive integer")
    top = maximum if cycle is None else cycle - 1
    if base == "*":
        sequence = list(range(minimum, top + 1))
    elif "-" in base:
        start_text, end_text = base.split("-", 1)
        start = _parse_number(start_text, minimum, maximum, names=names, first=first)
        end = _parse_number(end_text, minimum, maximum, names=names, first=first)
        if cycle is not None and start > end:
            start, end = start % cycle, end % cycle  # "7-1" is Sunday to Monday
        if start <= end:
            sequence = list(range(start, end + 1))
        else:  # wraps round the end of the field: 22-2 is 22, 23, 0, 1, 2
            sequence = list(range(start, top + 1)) + list(range(minimum, end + 1))
    else:
        start = _parse_number(base, minimum, maximum, names=names, first=first)
        # "5/15" is "5-59/15", from the value to the end of the field; "5" alone is 5.
        sequence = list(range(start, top + 1)) if "/" in part else [start]
    return set(sequence[::step])


def _split_step(part: str) -> tuple[str, int]:
    if "/" not in part:
        return part, 1
    base, step_text = part.split("/", 1)
    if not step_text:
        raise ValueError("cron step is missing")
    try:
        return base, int(step_text)
    except ValueError as exc:
        raise ValueError(f"invalid cron step {step_text!r}") from exc


def _parse_number(text: str, minimum: int, maximum: int, *, names: tuple[str, ...] | None = None, first: int = 0) -> int:
    """A field value, or a month / weekday name: abbreviated or not, any case ("jan", "Tues")."""
    word = text.strip().upper()
    if names and len(word) >= 3 and word.isalpha():
        for index, name in enumerate(names):
            if name.startswith(word):
                return index + first
    try:
        value = int(text)
    except ValueError as exc:
        hint = f" (or a name: {', '.join(name[:3] for name in names)})" if names else ""
        raise ValueError(f"invalid cron value {text!r}{hint}") from exc
    if value < minimum or value > maximum:
        raise ValueError(f"cron value {value} is outside {minimum}-{maximum}")
    return value


def compiled_schedule(source: object) -> CronSchedule | None:
    return getattr(source.__class__, "_kanary_compiled_schedule", None)
