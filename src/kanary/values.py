"""Turn text and JSON from external systems into numeric inputs.

Inputs may carry any Python object, but numbers are what RangeRule,
ThresholdRule, and friends compare. The helper sources therefore convert
values with ``to_number()`` and keep whatever does not convert as text in
the result metadata.
"""

from collections.abc import Mapping
from datetime import datetime, timedelta, timezone, tzinfo
from decimal import Decimal
import json
import numbers
import re
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_TRUE = {"true", "yes", "on"}
_FALSE = {"false", "no", "off"}


def to_number(value: Any) -> float | None:
    """Return value as a float, or None when it is not a number.

    Accepts ints, floats, bools (1.0 / 0.0), numeric strings, strings with a
    trailing ``%`` (as printed by ``df``), and true/false/yes/no/on/off.
    """
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (numbers.Real, Decimal)):  # includes Decimal from SQL NUMERIC, numpy scalars
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if text.lower() in _TRUE:
            return 1.0
        if text.lower() in _FALSE:
            return 0.0
        try:
            return float(text[:-1] if text.endswith("%") else text)
        except ValueError:
            return None
    return None


# Abbreviations that name one zone only; others (CST, IST, ...) mean different zones.
_ZONE_ABBREVIATIONS = {"UTC": "UTC", "GMT": "UTC", "Z": "UTC", "JST": "Asia/Tokyo", "KST": "Asia/Seoul"}
_OFFSET = re.compile(r"^(?:UTC|GMT)?\s*([+-])\s*(\d{1,2})(?:(?::?)(\d{2}))?$", re.IGNORECASE)


def to_timezone(value: Any) -> tzinfo:
    """A time zone from the ways people usually write one.

    - an IANA name: "Asia/Tokyo", "Europe/Berlin", "America/New_York", "UTC"
    - an offset from UTC: "+9", "+09", "+0900", "+09:00", "-05:30", "UTC+9", "GMT-3",
      or a number of hours (9, -5.5)
    - "JST", "KST", "UTC", "GMT", "Z", or "local" (the host's current offset)
    - a datetime.tzinfo, as it is

    An offset never changes; use an IANA name where daylight saving time applies.
    """
    if isinstance(value, tzinfo):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return _fixed_offset(timedelta(hours=value), value)
    if isinstance(value, timedelta):
        return _fixed_offset(value, value)
    text = str(value).strip() if isinstance(value, str) else None
    if not text:
        raise ValueError(f"time zone must be a name such as 'Asia/Tokyo' or an offset such as '+09:00', not {value!r}")
    if text.lower() == "local":
        local = datetime.now().astimezone().tzinfo
        assert local is not None
        return local
    name = _ZONE_ABBREVIATIONS.get(text.upper(), text)
    if name == "UTC":
        return timezone.utc
    match = _OFFSET.match(name)
    if match:
        sign, hours, minutes = match.groups()
        offset = timedelta(hours=int(hours), minutes=int(minutes or 0))
        return _fixed_offset(-offset if sign == "-" else offset, value)
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(
            f"unknown time zone {value!r}: use an IANA name such as 'Asia/Tokyo', "
            "an offset such as '+09:00', or 'UTC'"
        ) from exc


def _fixed_offset(offset: timedelta, original: Any) -> tzinfo:
    if abs(offset) >= timedelta(hours=24):
        raise ValueError(f"time zone offset {original!r} is not within 24 hours")
    return timezone(offset) if offset else timezone.utc


def to_datetime(value: Any, *, naive_timezone: Any = None) -> datetime | None:
    """Return a timezone-aware datetime from a datetime, ISO string, or unix time.

    A value without a timezone is read in ``naive_timezone`` (anything
    to_timezone() accepts, such as "Asia/Tokyo" or "+09:00"), or as UTC when
    that is None.
    """
    if value is None:
        return None
    zone = to_timezone(naive_timezone) if naive_timezone else timezone.utc
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=zone)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=zone)
    raise TypeError(f"cannot convert {value!r} to a datetime")


def flatten(document: Any, prefix: str = "") -> dict[str, Any]:
    """Flatten nested mappings into dotted names: {"a": {"b": 1}} -> {"a.b": 1}."""
    if not isinstance(document, Mapping):
        return {prefix or "value": document}
    flat: dict[str, Any] = {}
    for key, value in document.items():
        name = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, Mapping):
            flat.update(flatten(value, name))
        else:
            flat[name] = value
    return flat


def get_path(document: Any, path: str, default: Any = None) -> Any:
    """Look up a dotted path such as "data.sensors.0.value" in JSON-like data."""
    current = document
    for part in path.split("."):
        if isinstance(current, Mapping) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.lstrip("-").isdigit() and -len(current) <= int(part) < len(current):
            current = current[int(part)]
        else:
            return default
    return current


def parse_values(
    text: str,
    output_format: str = "auto",
    *,
    default_name: str = "value",
) -> tuple[dict[str, float], dict[str, str]]:
    """Parse command or file output into (numbers, texts).

    Formats:
      "number"   - the whole text is one number, named default_name
      "keyvalue" - "name=value" pairs, separated by spaces or newlines
      "json"     - a JSON number or object (nested keys become "a.b")
      "auto"     - JSON if it starts with "{" or "[", key=value if it has "=",
                   otherwise a single number
    Values that are not numbers are returned in texts.
    """
    stripped = text.strip()
    if output_format == "auto":
        if stripped[:1] in {"{", "["}:
            output_format = "json"
        elif "=" in stripped:
            output_format = "keyvalue"
        else:
            output_format = "number"

    if output_format == "number":
        raw: dict[str, Any] = {default_name: stripped}
    elif output_format == "keyvalue":
        raw = {}
        for token in stripped.split():
            if "=" in token:
                name, _, value = token.partition("=")
                raw[name] = value
    elif output_format == "json":
        raw = flatten(json.loads(stripped), "" if stripped[:1] == "{" else default_name)
    else:
        raise ValueError(f"unknown output_format {output_format!r}")

    numbers: dict[str, float] = {}
    texts: dict[str, str] = {}
    for name, value in raw.items():
        number = to_number(value)
        if number is None:
            texts[name] = value if isinstance(value, str) else json.dumps(value)
        else:
            numbers[name] = number
    return numbers, texts


def is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def coerce_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    raise TypeError("timestamp must be datetime or unix timestamp")
