from __future__ import annotations


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
