from difflib import get_close_matches
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .constants import AlertState, Severity, ERROR
from .models import Alert, Evaluation, SourceState
from .units import format_rate, format_time, second


@dataclass(slots=True)
class RuleContext:
    now: datetime
    source_id: str
    source_state: SourceState
    previous_alert: Alert | None = None

    @property
    def current(self) -> dict[str, Any]:
        return self.source_state.current.payload

    @property
    def previous(self) -> dict[str, Any]:
        return self.source_state.previous.payload

    def get_current(self, path: str, default: Any = None) -> Any:
        return get_by_path(self.current, path, default=default)

    def get_previous(self, path: str, default: Any = None) -> Any:
        return get_by_path(self.previous, path, default=default)

    def measurement(self, name: str, *, previous: bool = False) -> dict[str, Any]:
        snapshot = self.previous if previous else self.current
        channels = snapshot.get("channels", {})
        if isinstance(channels, Mapping) and name in channels:
            measurement = channels[name]
        else:
            measurement = get_by_path(snapshot, f"channels.{name}", default={})
        if isinstance(measurement, Mapping):
            return dict(measurement)
        return {}

    def value(self, name: str, default: Any = None, *, previous: bool = False) -> Any:
        measurement = self.measurement(name, previous=previous)
        return measurement.get("value", default)

    def timestamp(self, name: str, default: Any = None, *, previous: bool = False) -> Any:
        measurement = self.measurement(name, previous=previous)
        return measurement.get("timestamp", default)

    def metadata(self, name: str, default: Any = None, *, previous: bool = False) -> Any:
        measurement = self.measurement(name, previous=previous)
        return measurement.get("metadata", default)

    def has_measurement(self, name: str, *, previous: bool = False) -> bool:
        snapshot = self.previous if previous else self.current
        channels = snapshot.get("channels", {})
        return isinstance(channels, Mapping) and name in channels

    def available_measurements(self, *, previous: bool = False) -> list[str]:
        snapshot = self.previous if previous else self.current
        channels = snapshot.get("channels", {})
        if not isinstance(channels, Mapping):
            return []
        return [str(name) for name in channels.keys()]

    @property
    def previous_state(self) -> AlertState | None:
        if self.previous_alert is None:
            return None
        return self.previous_alert.state

    @property
    def previous_severity(self) -> Severity | None:
        if self.previous_alert is None:
            return None
        return self.previous_alert.severity

    def was_alerting(self) -> bool:
        return self.previous_state not in {None, AlertState.OK, AlertState.RESOLVED}


class Rule:
    rule_id: str
    source: str
    measurement: str | None = None
    severity: Severity = ERROR
    tags: list[str] = []
    owner: str | None = None
    description: str | None = None
    runbook: str | None = None
    depends_on: list[str] = []
    suppressed_by: list[str] = []

    def evaluate(self, payload: dict[str, Any], ctx: RuleContext) -> Evaluation:
        raise NotImplementedError

    def normalize_evaluation(
        self,
        result: Evaluation,
        payload: dict[str, Any],
    ) -> Evaluation:
        if isinstance(result, Evaluation):
            return result
        raise TypeError(f"{type(self).__name__}.evaluate() must return kanary.Evaluation")

    @classmethod
    def default_rule_id(cls) -> str | None:
        return None

    @classmethod
    def measurement_value_path(cls) -> str | None:
        measurement = getattr(cls, "measurement", None)
        if not measurement:
            return None
        return f"channels.{measurement}.value"

    @classmethod
    def measurement_timestamp_path(cls) -> str | None:
        measurement = getattr(cls, "measurement", None)
        if not measurement:
            return None
        return f"channels.{measurement}.timestamp"


class StaleRule(Rule):
    timeout: float
    timestamp_field: str | None = None

    def evaluate(self, payload: dict[str, Any], ctx: RuleContext) -> Evaluation:
        result_payload = dict(payload)
        timestamp_field = self._timestamp_field()
        timestamp_value = self._current_timestamp_value(payload, ctx)

        if timestamp_value is None:
            return Evaluation(
                state=AlertState.FIRING,
                payload=result_payload,
                message=_missing_field_message(
                    ctx,
                    measurement=self.measurement,
                    field_label="timestamp",
                    field_path=timestamp_field,
                    field_is_measurement_derived=self.timestamp_field is None and self.measurement is not None,
                ),
            )

        observed_at = _coerce_datetime(timestamp_value)
        age_seconds = (ctx.now - observed_at).total_seconds()
        result_payload["age_seconds"] = age_seconds

        if age_seconds > self.timeout:
            return Evaluation(
                state=AlertState.FIRING,
                payload=result_payload,
                message=f"stale for {format_time(age_seconds)} (> {format_time(self.timeout)})",
            )

        return Evaluation(
            state=AlertState.OK,
            payload=result_payload,
            message=f"age {format_time(age_seconds)}",
        )

    @classmethod
    def default_rule_id(cls) -> str | None:
        source_id = getattr(cls, "source", None)
        variable = getattr(cls, "measurement", None) or _field_variable_name(getattr(cls, "timestamp_field", None))
        if not source_id or not variable:
            return None
        return f"{source_id}.{variable}.stale"

    def _timestamp_field(self) -> str:
        return self.timestamp_field or self.measurement_timestamp_path() or "timestamp"

    def _current_timestamp_value(self, payload: dict[str, Any], ctx: RuleContext) -> Any:
        if self.timestamp_field is None and self.measurement is not None:
            return ctx.timestamp(self.measurement)
        return get_by_path(payload, self._timestamp_field())


class RangeRule(Rule):
    field: str | None = None
    low: float | None = None
    high: float | None = None
    hysteresis: float = 0.0
    lower_inclusive: bool = True
    upper_inclusive: bool = True

    def evaluate(self, payload: dict[str, Any], ctx: RuleContext) -> Evaluation:
        field = self._field()
        value = self._current_field_value(payload, ctx)
        result_payload = dict(payload)

        if value is None:
            return Evaluation(
                state=AlertState.OK,
                payload=result_payload,
                message=_missing_field_message(
                    ctx,
                    measurement=self.measurement,
                    field_label="value",
                    field_path=field,
                    field_is_measurement_derived=self.field is None and self.measurement is not None,
                ),
            )

        previous_value = self._previous_field_value(ctx)
        if self._should_fire(value, previous_value, ctx):
            return Evaluation(
                state=AlertState.FIRING,
                payload=result_payload,
                message=self._build_out_of_range_message(value, field),
            )

        return Evaluation(
            state=AlertState.OK,
            payload=result_payload,
            message=self._build_in_range_message(value, field),
        )

    def _should_fire(self, value: Any, previous_value: Any, ctx: RuleContext) -> bool:
        current_breach = self._breach_side(value)
        if current_breach is not None:
            return True
        if self.hysteresis <= 0 or not ctx.was_alerting():
            return False
        previous_breach = self._breach_side(previous_value)
        if previous_breach == "low":
            return self._still_low_after_hysteresis(value)
        if previous_breach == "high":
            return self._still_high_after_hysteresis(value)
        return False

    def _is_out_of_range(self, value: Any) -> bool:
        if self.low is not None:
            if self.lower_inclusive and value < self.low:
                return True
            if not self.lower_inclusive and value <= self.low:
                return True

        if self.high is not None:
            if self.upper_inclusive and value > self.high:
                return True
            if not self.upper_inclusive and value >= self.high:
                return True

        return False

    def _breach_side(self, value: Any) -> str | None:
        if value is None:
            return None
        if self.low is not None:
            if self.lower_inclusive and value < self.low:
                return "low"
            if not self.lower_inclusive and value <= self.low:
                return "low"
        if self.high is not None:
            if self.upper_inclusive and value > self.high:
                return "high"
            if not self.upper_inclusive and value >= self.high:
                return "high"
        return None

    def _still_low_after_hysteresis(self, value: Any) -> bool:
        if self.low is None:
            return False
        clear_threshold = self.low + self.hysteresis
        if self.lower_inclusive:
            return value < clear_threshold
        return value <= clear_threshold

    def _still_high_after_hysteresis(self, value: Any) -> bool:
        if self.high is None:
            return False
        clear_threshold = self.high - self.hysteresis
        if self.upper_inclusive:
            return value > clear_threshold
        return value >= clear_threshold

    def _build_out_of_range_message(self, value: Any, field: str) -> str:
        return f"{field}={value} out of range {self._format_range()}"

    def _build_in_range_message(self, value: Any, field: str) -> str:
        return f"{field}={value} within range {self._format_range()}"

    def _format_range(self) -> str:
        left = "[" if self.lower_inclusive else "("
        right = "]" if self.upper_inclusive else ")"
        low = "-inf" if self.low is None else self.low
        high = "inf" if self.high is None else self.high
        return f"{left}{low}, {high}{right}"

    @classmethod
    def default_rule_id(cls) -> str | None:
        source_id = getattr(cls, "source", None)
        variable = getattr(cls, "measurement", None) or _field_variable_name(getattr(cls, "field", None))
        if not source_id or not variable:
            return None
        return f"{source_id}.{variable}.range"

    def _field(self) -> str:
        return self.field or self.measurement_value_path() or "value"

    def _previous_field_value(self, ctx: RuleContext) -> Any:
        if self.field is None and self.measurement is not None:
            return ctx.value(self.measurement, previous=True)
        if ctx.previous_alert is None:
            return None
        return get_by_path(ctx.previous_alert.payload, self._field())

    def _current_field_value(self, payload: dict[str, Any], ctx: RuleContext) -> Any:
        if self.field is None and self.measurement is not None:
            return ctx.value(self.measurement)
        return get_by_path(payload, self._field())


class ThresholdRule(Rule):
    field: str | None = None
    thresholds: list[tuple[float, Severity]] = []
    direction: str = "high"
    hysteresis: float = 0.0

    def evaluate(self, payload: dict[str, Any], ctx: RuleContext) -> Evaluation:
        field = self._field()
        value = self._current_field_value(payload, ctx)
        result_payload = dict(payload)

        if value is None:
            return Evaluation(
                state=AlertState.OK,
                payload=result_payload,
                message=_missing_field_message(
                    ctx,
                    measurement=self.measurement,
                    field_label="value",
                    field_path=field,
                    field_is_measurement_derived=self.field is None and self.measurement is not None,
                ),
            )
        if not isinstance(value, (int, float)):
            return Evaluation(
                state=AlertState.OK,
                payload=result_payload,
                message=f"{field} must be numeric",
            )

        matched_severity = self._match_threshold(value)
        if self.hysteresis > 0 and ctx.was_alerting():
            matched_severity = self._apply_hysteresis(value, matched_severity, ctx.previous_severity)
        result_payload["matched_severity"] = matched_severity.name if matched_severity is not None else None
        if matched_severity is None:
            return Evaluation(
                state=AlertState.OK,
                payload=result_payload,
                message=f"{field}={value} within thresholds {self._format_thresholds()}",
            )

        return Evaluation(
            state=AlertState.FIRING,
            payload=result_payload,
            message=f"{field}={value} reached {matched_severity.name} threshold {self._format_thresholds()}",
            severity=matched_severity,
        )

    @classmethod
    def default_rule_id(cls) -> str | None:
        source_id = getattr(cls, "source", None)
        variable = getattr(cls, "measurement", None) or _field_variable_name(getattr(cls, "field", None))
        if not source_id or not variable:
            return None
        return f"{source_id}.{variable}.threshold"

    def _field(self) -> str:
        return self.field or self.measurement_value_path() or "value"

    def _current_field_value(self, payload: dict[str, Any], ctx: RuleContext) -> Any:
        if self.field is None and self.measurement is not None:
            return ctx.value(self.measurement)
        return get_by_path(payload, self._field())

    def _match_threshold(self, value: float) -> Severity | None:
        if self.direction == "high":
            matched: Severity | None = None
            for threshold_value, severity in sorted(self.thresholds, key=lambda item: item[0]):
                if value >= threshold_value:
                    matched = severity
            return matched
        if self.direction == "low":
            matched = None
            for threshold_value, severity in sorted(self.thresholds, key=lambda item: item[0], reverse=True):
                if value <= threshold_value:
                    matched = severity
            return matched
        raise ValueError(f"{type(self).__name__}.direction must be 'high' or 'low'")

    def _apply_hysteresis(
        self,
        value: float,
        matched_severity: Severity | None,
        previous_severity: Severity | None,
    ) -> Severity | None:
        if previous_severity is None:
            return matched_severity
        previous_threshold = self._threshold_for_severity(previous_severity)
        if previous_threshold is None:
            return matched_severity
        if matched_severity is not None and matched_severity >= previous_severity:
            return matched_severity
        if self.direction == "high":
            if value >= previous_threshold - self.hysteresis:
                return previous_severity
            return matched_severity
        if self.direction == "low":
            if value <= previous_threshold + self.hysteresis:
                return previous_severity
            return matched_severity
        raise ValueError(f"{type(self).__name__}.direction must be 'high' or 'low'")

    def _threshold_for_severity(self, severity: Severity) -> float | None:
        for threshold_value, threshold_severity in self.thresholds:
            if threshold_severity == severity:
                return threshold_value
        return None

    def _format_thresholds(self) -> str:
        ordered = sorted(self.thresholds, key=lambda item: item[0], reverse=self.direction == "low")
        joined = ", ".join(f"{value:g}->{severity.name}" for value, severity in ordered)
        return f"{self.direction} [{joined}]"


class RateRule(RangeRule):
    timestamp_field: str | None = None
    previous_field: str | None = None
    previous_timestamp_field: str | None = None
    per_seconds: float = 1.0

    def evaluate(self, payload: dict[str, Any], ctx: RuleContext) -> Evaluation:
        field = self._field()
        timestamp_field = self._timestamp_field()
        previous_field = self._previous_field()
        previous_timestamp_field = self._previous_timestamp_field()

        current_value = self._current_field_value(payload, ctx)
        current_timestamp = self._current_timestamp_value(payload, ctx)
        previous_value = self._previous_field_value(ctx)
        previous_timestamp = self._previous_timestamp_value(ctx)
        result_payload = dict(payload)

        if current_value is None or current_timestamp is None:
            missing_parts: list[str] = []
            if current_value is None:
                missing_parts.append(
                    _missing_field_message(
                        ctx,
                        measurement=self.measurement,
                        field_label="value",
                        field_path=field,
                        field_is_measurement_derived=self.field is None and self.measurement is not None,
                    )
                )
            if current_timestamp is None:
                missing_parts.append(
                    _missing_field_message(
                        ctx,
                        measurement=self.measurement,
                        field_label="timestamp",
                        field_path=timestamp_field,
                        field_is_measurement_derived=self.timestamp_field is None and self.measurement is not None,
                    )
                )
            return Evaluation(
                state=AlertState.OK,
                payload=result_payload,
                message="; ".join(missing_parts),
            )
        if previous_value is None or previous_timestamp is None:
            return Evaluation(
                state=AlertState.OK,
                payload=result_payload,
                message=f"previous {field} sample is missing",
            )
        if not isinstance(current_value, (int, float)) or not isinstance(previous_value, (int, float)):
            return Evaluation(
                state=AlertState.OK,
                payload=result_payload,
                message=f"{field} samples must be numeric",
            )

        current_observed_at = _coerce_datetime(current_timestamp)
        previous_observed_at = _coerce_datetime(previous_timestamp)
        delta_seconds = (current_observed_at - previous_observed_at).total_seconds()
        if delta_seconds <= 0:
            return Evaluation(
                state=AlertState.OK,
                payload=result_payload,
                message=f"invalid rate interval {delta_seconds:.1f}s",
            )

        rate = (current_value - previous_value) / delta_seconds * self.per_seconds
        result_payload["rate"] = rate
        result_payload["rate_delta_seconds"] = delta_seconds

        rate_per_second = (current_value - previous_value) / delta_seconds
        result_payload["rate_per_second"] = rate_per_second

        if self._is_out_of_range(rate):
            return Evaluation(
                state=AlertState.FIRING,
                payload=result_payload,
                message=(
                    f"{field} rate={self._format_rate_message(rate, rate_per_second)} "
                    f"out of range {self._format_range()}"
                ),
            )

        return Evaluation(
            state=AlertState.OK,
            payload=result_payload,
            message=(
                f"{field} rate={self._format_rate_message(rate, rate_per_second)} "
                f"within range {self._format_range()}"
            ),
        )

    @classmethod
    def default_rule_id(cls) -> str | None:
        source_id = getattr(cls, "source", None)
        variable = getattr(cls, "measurement", None) or _field_variable_name(getattr(cls, "field", None))
        if not source_id or not variable:
            return None
        return f"{source_id}.{variable}.rate"

    def _timestamp_field(self) -> str:
        return self.timestamp_field or self.measurement_timestamp_path() or "timestamp"

    def _previous_field(self) -> str:
        return self.previous_field or self._field()

    def _previous_timestamp_field(self) -> str:
        return self.previous_timestamp_field or self._timestamp_field()

    def _current_timestamp_value(self, payload: dict[str, Any], ctx: RuleContext) -> Any:
        if self.timestamp_field is None and self.measurement is not None:
            return ctx.timestamp(self.measurement)
        return get_by_path(payload, self._timestamp_field())

    def _previous_timestamp_value(self, ctx: RuleContext) -> Any:
        if self.previous_timestamp_field is None and self.timestamp_field is None and self.measurement is not None:
            return ctx.timestamp(self.measurement, previous=True)
        return get_by_path(ctx.previous, self._previous_timestamp_field())

    def _format_rate_message(self, rate: float, rate_per_second: float) -> str:
        if self.per_seconds == second:
            return format_rate(rate_per_second)
        return f"{rate:g} / {format_time(self.per_seconds)}"


def _coerce_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    raise TypeError("timestamp must be datetime or unix timestamp")


def _missing_field_message(
    ctx: RuleContext,
    *,
    measurement: str | None,
    field_label: str,
    field_path: str,
    field_is_measurement_derived: bool,
) -> str:
    if not field_is_measurement_derived or not measurement:
        return f"{field_path} is missing"

    if ctx.has_measurement(measurement):
        return f"measurement '{measurement}' is present but {field_label} is missing"

    available = ctx.available_measurements()
    message = f"measurement '{measurement}' is missing"
    closest = get_close_matches(measurement, available, n=1)
    if closest:
        message += f"; closest available measurement: {closest[0]}"
    if available:
        shown = ", ".join(sorted(available)[:5])
        if len(available) > 5:
            shown += ", ..."
        message += f"; available measurements: {shown}"
    return message


def get_by_path(payload: Mapping[str, Any], path: str, *, default: Any = None) -> Any:
    if path == "":
        return payload

    current: Any = payload
    for part in path.split("."):
        if isinstance(current, Mapping):
            if part not in current:
                return default
            current = current[part]
            continue
        return default
    return current


def _field_variable_name(path: str | None) -> str | None:
    if not path:
        return None
    parts = [part for part in path.split(".") if part]
    if parts and parts[0] == "channels":
        parts = parts[1:]
    if parts and parts[-1] in {"value", "timestamp", "metadata"}:
        parts = parts[:-1]
    if not parts:
        return None
    return parts[-1]


def prepare_rule_class(cls: type[Any]) -> type[Any]:
    rule_id = getattr(cls, "rule_id", None)
    if not isinstance(rule_id, str) or not rule_id:
        raise ValueError(f"rule '{cls.__name__}' must define non-empty string rule_id")

    source = getattr(cls, "source", None)
    if not isinstance(source, str) or not source:
        raise ValueError(f"rule '{rule_id}' must define non-empty string source")

    severity = getattr(cls, "severity", None)
    if not isinstance(severity, Severity):
        raise ValueError(
            f"rule '{rule_id}' severity must be one of kanary.INFO/WARN/ERROR/CRITICAL"
        )

    if not hasattr(cls, "tags"):
        raise ValueError(f"rule '{rule_id}' must define tags")
    tags = getattr(cls, "tags")
    if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
        raise ValueError(f"rule '{rule_id}' tags must be list[str]")

    evaluate = getattr(cls, "evaluate", None)
    if not callable(evaluate):
        raise ValueError(f"rule '{rule_id}' must implement evaluate(payload, ctx)")

    _setdefault(cls, "measurement", None)
    _setdefault(cls, "owner", None)
    _setdefault(cls, "description", None)
    _setdefault(cls, "runbook", None)
    _setdefault(cls, "depends_on", [])
    _setdefault(cls, "suppressed_by", [])
    _setdefault(cls, "matched_outputs", [])

    for attr_name in ("depends_on", "suppressed_by"):
        value = getattr(cls, attr_name)
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ValueError(f"rule '{rule_id}' {attr_name} must be list[str]")

    thresholds = getattr(cls, "thresholds", None)
    if thresholds is not None and thresholds != []:
        if not isinstance(thresholds, list):
            raise ValueError(f"rule '{rule_id}' thresholds must be list[tuple[number, Severity]]")
        for item in thresholds:
            if (
                not isinstance(item, tuple)
                or len(item) != 2
                or not isinstance(item[0], (int, float))
                or not isinstance(item[1], Severity)
            ):
                raise ValueError(f"rule '{rule_id}' thresholds must be list[tuple[number, Severity]]")
        direction = getattr(cls, "direction", "high")
        if direction not in {"high", "low"}:
            raise ValueError(f"rule '{rule_id}' direction must be 'high' or 'low'")

    if "normalize_evaluation" not in cls.__dict__:
        cls.normalize_evaluation = Rule.normalize_evaluation
    if "measurement_value_path" not in cls.__dict__:
        cls.measurement_value_path = classmethod(Rule.measurement_value_path.__func__)
    if "measurement_timestamp_path" not in cls.__dict__:
        cls.measurement_timestamp_path = classmethod(Rule.measurement_timestamp_path.__func__)
    if "default_rule_id" not in cls.__dict__:
        cls.default_rule_id = classmethod(lambda inner_cls: getattr(inner_cls, "rule_id", None))
    return cls


def _setdefault(cls: type[Any], attr_name: str, value: Any) -> None:
    if hasattr(cls, attr_name):
        return
    setattr(cls, attr_name, value)
