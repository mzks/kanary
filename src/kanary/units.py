from __future__ import annotations

import math
import re

nanosecond = 1e-9
microsecond = 1e-6
millisecond = 1e-3
second = 1.0
minute = 60.0 * second
hour = 60.0 * minute
day = 24.0 * hour

Hz = 1.0 / second
kHz = 1_000.0 * Hz
MHz = 1_000_000.0 * Hz
cps = 1.0 / second


_DURATION_PART = re.compile(r"(\d+(?:\.\d*)?|\.\d+)([a-zµ]*)", re.IGNORECASE)
_DURATION_UNITS = {
    "": 1.0, "s": 1.0, "sec": 1.0, "secs": 1.0, "second": 1.0, "seconds": 1.0,
    "ms": 0.001, "us": 1e-6, "µs": 1e-6, "ns": 1e-9,
    "m": 60.0, "min": 60.0, "mins": 60.0, "minute": 60.0, "minutes": 60.0,
    "h": 3600.0, "hr": 3600.0, "hrs": 3600.0, "hour": 3600.0, "hours": 3600.0,
    "d": 86400.0, "day": 86400.0, "days": 86400.0,
}


def parse_duration(value: object) -> float:
    """Seconds from a number (30, 1.5) or text: "30s", "5m", "1h30m", "1.5 h", "2 days", "500ms".

    A number without a unit is seconds; "m" is minutes, as in Go and robfig/cron.
    """
    if isinstance(value, bool):
        raise ValueError(f"a duration is a number of seconds or text such as '30s' or '5m', not {value!r}")
    if isinstance(value, (int, float)):
        seconds = float(value)
    elif isinstance(value, str):
        text, seconds, position = value.strip().replace(" ", ""), 0.0, 0
        for part in _DURATION_PART.finditer(text):
            unit = part.group(2).lower()
            if part.start() != position or unit not in _DURATION_UNITS:
                break
            seconds += float(part.group(1)) * _DURATION_UNITS[unit]
            position = part.end()
        if not text or position != len(text):
            raise ValueError(f"a duration is a number of seconds or text such as '30s', '5m', or '1h30m', not {value!r}")
    else:
        raise ValueError(f"a duration is a number of seconds or text such as '30s' or '5m', not {value!r}")
    if not math.isfinite(seconds):
        raise ValueError(f"a duration must be finite, not {value!r}")
    return seconds


def best_time_unit(seconds: float) -> tuple[float, str]:
    absolute = abs(seconds)
    if absolute >= day:
        return day, "d"
    if absolute >= hour:
        return hour, "h"
    if absolute >= minute:
        return minute, "min"
    if absolute >= second:
        return second, "s"
    if absolute >= millisecond:
        return millisecond, "ms"
    if absolute >= microsecond:
        return microsecond, "us"
    return nanosecond, "ns"


def format_time(seconds: float, *, precision: int = 1) -> str:
    scale, label = best_time_unit(seconds)
    value = seconds / scale
    return f"{_format_number(value, precision=precision)} {label}"


def best_per_time_unit(per_second: float) -> tuple[float, str]:
    """(seconds, label) of the time unit in which a rate per second reads as 1 or more."""
    units = ((second, "s"), (minute, "min"), (hour, "h"), (day, "d"))
    if per_second == 0:
        return units[0]
    for scale, label in units:
        if abs(per_second) * scale >= 1:
            return scale, label
    return units[-1]


def format_per_time(per_second: float, *, unit: tuple[float, str] | None = None, precision: int = 3) -> str:
    """A rate per second as "2 /s", "1.5 /min", or "3 /h" (unit: from best_per_time_unit())."""
    scale, label = unit or best_per_time_unit(per_second)
    return f"{_format_number(per_second * scale, precision=precision)} /{label}"


def best_rate_unit(per_second: float, *, kind: str = "frequency") -> tuple[float, str]:
    absolute = abs(per_second)
    if kind == "count":
        if absolute >= 1_000_000.0:
            return MHz, "Mcps"
        if absolute >= 1_000.0:
            return kHz, "kcps"
        return cps, "cps"

    if absolute >= 1_000_000.0:
        return MHz, "MHz"
    if absolute >= 1_000.0:
        return kHz, "kHz"
    return Hz, "Hz"


def format_rate(per_second: float, *, precision: int = 1, kind: str = "frequency") -> str:
    scale, label = best_rate_unit(per_second, kind=kind)
    value = per_second / scale
    return f"{_format_number(value, precision=precision)} {label}"


def _format_number(value: float, *, precision: int) -> str:
    text = f"{value:.{precision}f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if text == "-0":
        return "0"
    return text
