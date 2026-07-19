from collections import deque
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta, timezone
import threading
from typing import Any, Callable

from .models import Measurement, SourceResult
from .schedule import CronSchedule, parse_schedule
from .signature_compat import detect_instance_method_style, invoke_compat


class Source:
    source_id: str
    description: str | None = None
    interval: float | None = None
    schedule: str | None = None
    max_retry: int = 1
    max_reinit: int = 1

    def init(self) -> None:
        return None

    def poll(self) -> SourceResult:
        raise NotImplementedError

    def terminate(self) -> None:
        return None


class PushSource(Source):
    """A source that accepts its latest result from external code."""

    wake_on_push: bool = True

    def init(self) -> None:
        self._push_lock = threading.Lock()
        self._pending_push: SourceResult | None = None
        self._push_wakeup: Callable[[], None] | None = None

    def push(self, result: Any, *, now: datetime | None = None) -> None:
        """Store one input snapshot for the next poll."""
        normalized = normalize_source_output(result, now=now)
        with self._push_lock:
            self._pending_push = normalized
        if self.wake_on_push and self._push_wakeup is not None:
            self._push_wakeup()

    def _set_push_wakeup(self, wakeup: Callable[[], None] | None) -> None:
        self._push_wakeup = wakeup

    def poll(self) -> SourceResult:
        with self._push_lock:
            result = self._pending_push
            self._pending_push = None
        return result or no_update(reason="waiting for pushed inputs")

class BufferedSource(Source):
    history_limit: int = 1024
    history_window_seconds: float | None = None

    def init(self) -> None:
        self._measurement_history: dict[str, deque[Measurement]] = {}

    def fetch(self) -> SourceResult:
        raise NotImplementedError

    def poll(self) -> SourceResult:
        result = invoke_compat(
            self.fetch,
            style=getattr(self.__class__, "__kanary_fetch_style__", "new"),
            new_args=(),
            legacy_args=({"engine": getattr(self, "_kanary_engine", None), "now": getattr(self, "_kanary_poll_now", None)},),
        )
        self.record_result(result)
        return result

    def record_result(self, result: SourceResult) -> None:
        for measurement in result.measurements:
            history = self._measurement_history.setdefault(
                measurement.name,
                deque(maxlen=self.history_limit),
            )
            history.append(measurement)
            self._prune_history(history, measurement.timestamp)

    def history(self, name: str, *, window_seconds: float | None = None) -> list[Measurement]:
        history = list(self._measurement_history.get(name, ()))
        if window_seconds is None:
            return history
        if not history:
            return []
        cutoff = history[-1].timestamp - timedelta(seconds=window_seconds)
        return [measurement for measurement in history if measurement.timestamp >= cutoff]

    def latest(self, name: str) -> Measurement | None:
        history = self._measurement_history.get(name)
        if not history:
            return None
        return history[-1]

    def average_value(self, name: str, *, window_seconds: float | None = None) -> float | None:
        measurements = self.history(name, window_seconds=window_seconds)
        values = [measurement.value for measurement in measurements if isinstance(measurement.value, (int, float))]
        if not values:
            return None
        return sum(values) / len(values)

    def min_value(self, name: str, *, window_seconds: float | None = None) -> float | None:
        measurements = self.history(name, window_seconds=window_seconds)
        values = [measurement.value for measurement in measurements if isinstance(measurement.value, (int, float))]
        if not values:
            return None
        return min(values)

    def max_value(self, name: str, *, window_seconds: float | None = None) -> float | None:
        measurements = self.history(name, window_seconds=window_seconds)
        values = [measurement.value for measurement in measurements if isinstance(measurement.value, (int, float))]
        if not values:
            return None
        return max(values)

    def rate(self, name: str, *, window_seconds: float | None = None, per_seconds: float = 1.0) -> float | None:
        measurements = self.history(name, window_seconds=window_seconds)
        if len(measurements) < 2:
            return None
        first = measurements[0]
        last = measurements[-1]
        if not isinstance(first.value, (int, float)) or not isinstance(last.value, (int, float)):
            return None
        delta_seconds = (last.timestamp - first.timestamp).total_seconds()
        if delta_seconds <= 0:
            return None
        return (last.value - first.value) / delta_seconds * per_seconds

    def count(self, name: str, *, window_seconds: float | None = None) -> int:
        return len(self.history(name, window_seconds=window_seconds))

    def _prune_history(self, history: deque[Measurement], latest_timestamp: datetime) -> None:
        if self.history_window_seconds is None:
            return
        cutoff = latest_timestamp - timedelta(seconds=self.history_window_seconds)
        while history and history[0].timestamp < cutoff:
            history.popleft()


def inputs(
    *items: Any,
    timestamp: datetime | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> SourceResult:
    normalized_measurements = _normalize_input_items(items, default_timestamp=timestamp)
    return SourceResult(
        measurements=normalized_measurements,
        metadata=dict(metadata or {}),
    )


def no_data(
    *,
    reason: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> SourceResult:
    return SourceResult(
        measurements=[],
        status="empty",
        reason=reason,
        metadata=dict(metadata or {}),
    )


def no_update(
    *,
    reason: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> SourceResult:
    return SourceResult(
        measurements=[],
        status="no_update",
        reason=reason,
        metadata=dict(metadata or {}),
    )


def skip(
    *,
    reason: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> SourceResult:
    return SourceResult(
        measurements=[],
        status="skip",
        reason=reason,
        metadata=dict(metadata or {}),
    )


def normalize_source_output(
    result: Any,
    *,
    now: datetime | None = None,
) -> SourceResult:
    if isinstance(result, SourceResult):
        return result
    default_timestamp = now
    if isinstance(result, Mapping):
        return SourceResult(
            measurements=_measurements_from_mapping(result, default_timestamp=default_timestamp),
        )
    if isinstance(result, list):
        return SourceResult(
            measurements=_measurements_from_iterable(result, default_timestamp=default_timestamp),
        )
    if isinstance(result, tuple):
        return SourceResult(
            measurements=_normalize_input_items(result, default_timestamp=default_timestamp),
        )
    raise TypeError(
        "source poll() must return kanary.SourceResult, kanary.inputs(...), "
        "a dict[name, value], or a list/tuple of input tuples"
    )


def _normalize_input_items(
    items: tuple[Any, ...],
    *,
    default_timestamp: datetime | None,
) -> list[Measurement]:
    if not items:
        return []
    if len(items) >= 2 and isinstance(items[0], str):
        return [_measurement_from_named_item(tuple(items), default_timestamp=default_timestamp)]
    if len(items) == 1:
        first = items[0]
        if isinstance(first, Mapping):
            return _measurements_from_mapping(first, default_timestamp=default_timestamp)
        if isinstance(first, list):
            return _measurements_from_iterable(first, default_timestamp=default_timestamp)
        if isinstance(first, tuple):
            return [_measurement_from_named_item(first, default_timestamp=default_timestamp)]
    return _measurements_from_iterable(items, default_timestamp=default_timestamp)


def _measurements_from_mapping(
    mapping: Mapping[str, Any],
    *,
    default_timestamp: datetime | None,
) -> list[Measurement]:
    measurements: list[Measurement] = []
    for name, raw in mapping.items():
        if not isinstance(name, str) or not name:
            raise ValueError("inputs mapping keys must be non-empty strings")
        measurements.append(
            _measurement_from_mapping_value(name, raw, default_timestamp=default_timestamp)
        )
    return measurements


def _measurements_from_iterable(
    items: Iterable[Any],
    *,
    default_timestamp: datetime | None,
) -> list[Measurement]:
    measurements: list[Measurement] = []
    for item in items:
        if isinstance(item, Measurement):
            measurements.append(item)
            continue
        if not isinstance(item, tuple):
            raise ValueError("inputs iterable items must be tuples or Measurement objects")
        measurements.append(_measurement_from_named_item(item, default_timestamp=default_timestamp))
    return measurements


def _measurement_from_mapping_value(
    name: str,
    raw: Any,
    *,
    default_timestamp: datetime | None,
) -> Measurement:
    if isinstance(raw, Mapping):
        value = raw.get("value")
        timestamp = _resolve_timestamp(raw.get("timestamp"), default_timestamp)
        metadata = _coerce_item_metadata(raw.get("metadata"))
        return Measurement(name=name, value=value, timestamp=timestamp, metadata=metadata)
    if isinstance(raw, tuple):
        length = len(raw)
        if length == 0 or length > 3:
            raise ValueError("mapping tuple values must be (value), (value, timestamp), or (value, timestamp, metadata)")
        value = raw[0]
        timestamp = _resolve_timestamp(raw[1] if length >= 2 else None, default_timestamp)
        metadata = _coerce_item_metadata(raw[2] if length >= 3 else None)
        return Measurement(name=name, value=value, timestamp=timestamp, metadata=metadata)
    return Measurement(
        name=name,
        value=raw,
        timestamp=_resolve_timestamp(None, default_timestamp),
        metadata={},
    )


def _measurement_from_named_item(
    item: tuple[Any, ...],
    *,
    default_timestamp: datetime | None,
) -> Measurement:
    if len(item) < 2 or len(item) > 4:
        raise ValueError("input tuples must be (name, value), (name, value, timestamp), or (name, value, timestamp, metadata)")
    name = item[0]
    if not isinstance(name, str) or not name:
        raise ValueError("input tuple name must be a non-empty string")
    value = item[1]
    timestamp = _resolve_timestamp(item[2] if len(item) >= 3 else None, default_timestamp)
    metadata = _coerce_item_metadata(item[3] if len(item) >= 4 else None)
    return Measurement(name=name, value=value, timestamp=timestamp, metadata=metadata)


def _coerce_item_metadata(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    return {"value": value}


def _resolve_timestamp(
    value: datetime | None,
    default_timestamp: datetime | None,
) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(default_timestamp, datetime):
        return default_timestamp
    return datetime.now(timezone.utc)


def prepare_source_class(cls: type[Any]) -> type[Any]:
    _setdefault(cls, "description", None)
    _setdefault(cls, "max_retry", 1)
    _setdefault(cls, "max_reinit", 1)
    if issubclass(cls, PushSource):
        _setdefault(cls, "wake_on_push", True)
        if not isinstance(cls.wake_on_push, bool):
            raise ValueError(
                f"source '{getattr(cls, 'source_id', cls.__name__)}' wake_on_push must be a boolean"
            )
    if "init" not in cls.__dict__ and getattr(cls, "init", None) in {None, Source.init}:
        cls.init = Source.init
    if "terminate" not in cls.__dict__ and getattr(cls, "terminate", None) in {None, Source.terminate}:
        cls.terminate = Source.terminate

    source_id = getattr(cls, "source_id", None)
    if not isinstance(source_id, str) or not source_id:
        raise ValueError(f"source '{cls.__name__}' must define non-empty string source_id")
    if not callable(getattr(cls, "poll", None)):
        raise ValueError(f"source '{source_id}' must implement poll()")

    try:
        init_style = detect_instance_method_style(
            getattr(cls, "init"),
            new_arity=0,
            legacy_arity=1,
            new_signature="init(self)",
            legacy_signature="init(self, ctx)",
        )
    except ValueError as exc:
        raise ValueError(f"source '{source_id}' {exc}") from exc
    try:
        poll_style = detect_instance_method_style(
            getattr(cls, "poll"),
            new_arity=0,
            legacy_arity=1,
            new_signature="poll(self)",
            legacy_signature="poll(self, ctx)",
        )
    except ValueError as exc:
        raise ValueError(f"source '{source_id}' {exc}") from exc
    try:
        terminate_style = detect_instance_method_style(
            getattr(cls, "terminate"),
            new_arity=0,
            legacy_arity=1,
            new_signature="terminate(self)",
            legacy_signature="terminate(self, ctx)",
        )
    except ValueError as exc:
        raise ValueError(f"source '{source_id}' {exc}") from exc
    cls.__kanary_init_style__ = init_style
    cls.__kanary_poll_style__ = poll_style
    cls.__kanary_terminate_style__ = terminate_style

    if issubclass(cls, BufferedSource):
        try:
            fetch_style = detect_instance_method_style(
                getattr(cls, "fetch"),
                new_arity=0,
                legacy_arity=1,
                new_signature="fetch(self)",
                legacy_signature="fetch(self, ctx)",
            )
        except ValueError as exc:
            raise ValueError(f"source '{source_id}' {exc}") from exc
        cls.__kanary_fetch_style__ = fetch_style

    interval = getattr(cls, "interval", None)
    schedule = getattr(cls, "schedule", None)
    if interval is None and schedule is None:
        interval = 60.0
        cls.interval = interval
    if interval is not None and schedule is not None:
        raise ValueError(f"source '{source_id}' must not define both interval and schedule")
    if interval is not None:
        if not isinstance(interval, (int, float)) or interval <= 0:
            raise ValueError(f"source '{source_id}' interval must be a positive number")
        cls._kanary_compiled_schedule = None
    else:
        if not isinstance(schedule, str) or not schedule.strip():
            raise ValueError(f"source '{source_id}' schedule must be a non-empty cron-like string")
        try:
            cls._kanary_compiled_schedule = parse_schedule(schedule)
        except ValueError as exc:
            raise ValueError(f"source '{source_id}' schedule is invalid: {exc}") from exc
    for attr_name in ("max_retry", "max_reinit"):
        value = getattr(cls, attr_name, None)
        if not isinstance(value, int) or value < 0:
            raise ValueError(f"source '{source_id}' {attr_name} must be a non-negative integer")
    return cls


def _setdefault(cls: type[Any], attr_name: str, value: Any) -> None:
    if hasattr(cls, attr_name):
        return
    setattr(cls, attr_name, value)


def compiled_schedule(source: Source) -> CronSchedule | None:
    return getattr(source.__class__, "_kanary_compiled_schedule", None)
