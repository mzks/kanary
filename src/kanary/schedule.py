from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta


_MACROS = {
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
    "@monthly": "0 0 1 * *",
    "@weekly": "0 0 * * 0",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
}


@dataclass(frozen=True, slots=True)
class CronSchedule:
    expression: str
    minutes: frozenset[int]
    hours: frozenset[int]
    days_of_month: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]

    @classmethod
    def parse(cls, expression: str) -> "CronSchedule":
        if not isinstance(expression, str) or not expression.strip():
            raise ValueError("schedule must be a non-empty cron-like string")

        normalized = _MACROS.get(expression.strip().lower(), expression.strip())
        fields = normalized.split()
        if len(fields) != 5:
            raise ValueError("schedule must use 5 cron fields: minute hour day month weekday")

        minute, hour, day_of_month, month, weekday = fields
        return cls(
            expression=expression.strip(),
            minutes=frozenset(_parse_field(minute, 0, 59)),
            hours=frozenset(_parse_field(hour, 0, 23)),
            days_of_month=frozenset(_parse_field(day_of_month, 1, 31)),
            months=frozenset(_parse_field(month, 1, 12)),
            weekdays=frozenset(_parse_field(weekday, 0, 7, normalize_weekday=True)),
        )

    def matches(self, dt: datetime) -> bool:
        weekday = (dt.weekday() + 1) % 7
        return (
            dt.minute in self.minutes
            and dt.hour in self.hours
            and dt.day in self.days_of_month
            and dt.month in self.months
            and weekday in self.weekdays
        )

    def next_after(self, dt: datetime) -> datetime:
        candidate = dt.replace(second=0, microsecond=0) + timedelta(minutes=1)
        deadline = candidate + timedelta(days=366 * 5)
        while candidate <= deadline:
            if self.matches(candidate):
                return candidate
            candidate += timedelta(minutes=1)
        raise ValueError(f"schedule {self.expression!r} does not produce a future run within 5 years")


def parse_schedule(expression: str) -> CronSchedule:
    return CronSchedule.parse(expression)


def _parse_field(spec: str, minimum: int, maximum: int, *, normalize_weekday: bool = False) -> set[int]:
    values: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            raise ValueError("empty cron field element is not allowed")
        values.update(_parse_part(part, minimum, maximum, normalize_weekday=normalize_weekday))
    if not values:
        raise ValueError("cron field did not produce any values")
    return values


def _parse_part(part: str, minimum: int, maximum: int, *, normalize_weekday: bool) -> set[int]:
    base, step = _split_step(part)
    if step <= 0:
        raise ValueError("cron step must be a positive integer")

    if base == "*":
        numbers = list(range(minimum, maximum + 1))
    elif "-" in base:
        start_text, end_text = base.split("-", 1)
        start = _parse_number(start_text, minimum, maximum, normalize_weekday=normalize_weekday)
        end = _parse_number(end_text, minimum, maximum, normalize_weekday=normalize_weekday)
        if start > end:
            raise ValueError(f"invalid cron range {base!r}")
        numbers = list(range(start, end + 1))
    else:
        numbers = [_parse_number(base, minimum, maximum, normalize_weekday=normalize_weekday)]

    return set(numbers[::step])


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


def _parse_number(text: str, minimum: int, maximum: int, *, normalize_weekday: bool) -> int:
    try:
        value = int(text)
    except ValueError as exc:
        raise ValueError(f"invalid cron value {text!r}") from exc
    if normalize_weekday and value == 7:
        value = 0
    if value < minimum or value > maximum:
        raise ValueError(f"cron value {value} is outside {minimum}-{maximum}")
    return value
