from collections import deque
from datetime import datetime, timedelta
from typing import Any

from .models import Measurement, SourceResult


class Source:
    source_id: str
    interval: float = 60.0

    def init(self, ctx: dict[str, Any]) -> None:
        return None

    def poll(self, ctx: dict[str, Any]) -> SourceResult:
        raise NotImplementedError

    def terminate(self, ctx: dict[str, Any]) -> None:
        return None


class BufferedSource(Source):
    history_limit: int = 1024
    history_window_seconds: float | None = None

    def init(self, ctx: dict[str, Any]) -> None:
        self._measurement_history: dict[str, deque[Measurement]] = {}

    def fetch(self, ctx: dict[str, Any]) -> SourceResult:
        raise NotImplementedError

    def poll(self, ctx: dict[str, Any]) -> SourceResult:
        result = self.fetch(ctx)
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


def prepare_source_class(cls: type[Any]) -> type[Any]:
    _setdefault(cls, "interval", 60.0)
    if "init" not in cls.__dict__ and getattr(cls, "init", None) in {None, Source.init}:
        cls.init = Source.init
    if "terminate" not in cls.__dict__ and getattr(cls, "terminate", None) in {None, Source.terminate}:
        cls.terminate = Source.terminate

    source_id = getattr(cls, "source_id", None)
    if not isinstance(source_id, str) or not source_id:
        raise ValueError(f"source '{cls.__name__}' must define non-empty string source_id")
    if not callable(getattr(cls, "poll", None)):
        raise ValueError(f"source '{source_id}' must implement poll(ctx)")
    return cls


def _setdefault(cls: type[Any], attr_name: str, value: Any) -> None:
    if hasattr(cls, attr_name):
        return
    setattr(cls, attr_name, value)
