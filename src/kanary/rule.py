from difflib import get_close_matches
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from fnmatch import fnmatch
from typing import Any

from .constants import AlertState, Severity, CRITICAL, ERROR, FIRING, INFO, OK, WARN
from .models import Alert, Evaluation, SourceState
from .signature_compat import detect_instance_method_style
from .units import format_rate, format_time, second


@dataclass(slots=True)
class InputView:
    name: str
    source_id: str
    input_name: str
    raw: dict[str, Any]

    @property
    def value(self) -> Any:
        return self.raw.get("value")

    @property
    def timestamp(self) -> Any:
        return self.raw.get("timestamp")

    @property
    def metadata(self) -> dict[str, Any]:
        metadata = self.raw.get("metadata", {})
        if isinstance(metadata, Mapping):
            return dict(metadata)
        return {}


@dataclass(slots=True)
class RuleContext:
    now: datetime
    source_id: str | None = None
    source_state: SourceState | None = None
    source_states: Mapping[str, SourceState] | None = None
    declared_inputs: tuple[str, ...] = ()
    resolved_sources: tuple[str, ...] = ()
    previous_alert: Alert | None = None

    def __post_init__(self) -> None:
        if self.source_states is None:
            if self.source_id is not None and self.source_state is not None:
                self.source_states = {self.source_id: self.source_state}
            else:
                self.source_states = {}
        if self.source_id is not None and self.source_state is None:
            self.source_state = self.source_states.get(self.source_id)
        if not self.resolved_sources:
            if self.source_id is not None:
                self.resolved_sources = (self.source_id,)
            else:
                self.resolved_sources = tuple(sorted(self.source_states.keys()))
        if not self.declared_inputs and self.source_id is not None:
            self.declared_inputs = (f"{self.source_id}:*",)

    @property
    def current(self) -> dict[str, Any]:
        if self.source_state is not None:
            return self.source_state.current.payload
        if len(self.resolved_sources) == 1:
            state = self.source_states.get(self.resolved_sources[0]) if self.source_states else None
            if state is not None:
                return state.current.payload
        return {}

    @property
    def previous(self) -> dict[str, Any]:
        if self.source_state is not None:
            return self.source_state.previous.payload
        if len(self.resolved_sources) == 1:
            state = self.source_states.get(self.resolved_sources[0]) if self.source_states else None
            if state is not None:
                return state.previous.payload
        return {}

    def inputs(
        self,
        selector: str | Iterable[str] | None = None,
        *,
        previous: bool = False,
    ) -> list[InputView]:
        selectors = _normalize_runtime_selectors(selector, self.declared_inputs)
        states = self.source_states or {}
        matched: dict[str, InputView] = {}
        for source_id in sorted(self.resolved_sources or tuple(states.keys())):
            state = states.get(source_id)
            if state is None:
                continue
            snapshot = state.previous.payload if previous else state.current.payload
            channels = snapshot.get("channels", {})
            if not isinstance(channels, Mapping):
                continue
            for input_name, raw in channels.items():
                full_name = _qualify_input_name(source_id, str(input_name))
                if not _matches_any_input_selector(full_name, selectors):
                    continue
                if isinstance(raw, Mapping):
                    matched[full_name] = InputView(
                        name=full_name,
                        source_id=source_id,
                        input_name=str(input_name),
                        raw=dict(raw),
                    )
        return [matched[name] for name in sorted(matched)]

    def names(
        self,
        selector: str | Iterable[str] | None = None,
        *,
        previous: bool = False,
    ) -> list[str]:
        return [item.name for item in self.inputs(selector, previous=previous)]

    def values(
        self,
        selector: str | Iterable[str] | None = None,
        *,
        previous: bool = False,
    ) -> list[Any]:
        return [item.value for item in self.inputs(selector, previous=previous)]

    def timestamps(
        self,
        selector: str | Iterable[str] | None = None,
        *,
        previous: bool = False,
    ) -> list[Any]:
        return [item.timestamp for item in self.inputs(selector, previous=previous)]

    def metadatas(
        self,
        selector: str | Iterable[str] | None = None,
        *,
        previous: bool = False,
    ) -> list[dict[str, Any]]:
        return [item.metadata for item in self.inputs(selector, previous=previous)]

    def value(self, name: str | None = None, default: Any = None, *, previous: bool = False) -> Any:
        match = self._single_input_match(name, previous=previous)
        if match is None:
            return default
        value = match.value
        if value is None:
            return default
        return value

    def timestamp(self, name: str | None = None, default: Any = None, *, previous: bool = False) -> Any:
        match = self._single_input_match(name, previous=previous)
        if match is None:
            return default
        value = match.timestamp
        if value is None:
            return default
        return value

    def metadata(self, name: str | None = None, default: Any = None, *, previous: bool = False) -> Any:
        match = self._single_input_match(name, previous=previous)
        if match is None:
            return default
        metadata = match.metadata
        if metadata == {} and default is not None and "metadata" not in match.raw:
            return default
        return metadata

    def prev_value(self, name: str | None = None, default: Any = None) -> Any:
        return self.value(name, default, previous=True)

    def prev_timestamp(self, name: str | None = None, default: Any = None) -> Any:
        return self.timestamp(name, default, previous=True)

    def prev_metadata(self, name: str | None = None, default: Any = None) -> Any:
        return self.metadata(name, default, previous=True)

    def source_payload(
        self,
        source_id: str | None = None,
        *,
        previous: bool = False,
    ) -> dict[str, Any]:
        target_source_id = source_id
        if target_source_id is None:
            if self.source_id is not None:
                target_source_id = self.source_id
            elif len(self.resolved_sources) == 1:
                target_source_id = self.resolved_sources[0]
            else:
                raise ValueError("source_payload() is ambiguous; specify source_id")
        state = (self.source_states or {}).get(target_source_id)
        if state is None:
            return {}
        snapshot = state.previous.payload if previous else state.current.payload
        return dict(snapshot)

    def _single_input_match(self, selector: str | None, *, previous: bool = False) -> InputView | None:
        matches = self.inputs(selector, previous=previous)
        if not matches:
            return None
        if len(matches) > 1:
            requested = selector or "<default>"
            resolved = ", ".join(item.name for item in matches)
            raise ValueError(f"input selector '{requested}' is ambiguous: {resolved}")
        return matches[0]

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
        return self.previous_state not in {None, AlertState.OK}


class Rule:
    rule_id: str
    source: str
    inputs: list[str] = []
    resolved_sources: list[str] = []
    severity: Severity = ERROR
    tags: list[str] = []
    owner: str | None = None
    description: str | None = None
    runbook: str | None = None
    depends_on: list[str] = []
    suppressed_by: list[str] = []

    def evaluate(self, ctx: RuleContext) -> Evaluation:
        raise NotImplementedError

    def normalize_evaluation(
        self,
        result: Any,
        payload: dict[str, Any],
    ) -> Evaluation:
        if isinstance(result, Evaluation):
            base_payload = dict(payload)
            if result.payload is None:
                normalized_payload = base_payload
            else:
                normalized_payload = dict(result.payload)
            if result.extra:
                if result.payload is None:
                    normalized_payload.update(result.extra)
                else:
                    normalized_payload.update(result.extra)
            result.payload = normalized_payload
            return result
        if result is None:
            return Evaluation(state=OK, payload=dict(payload))
        if isinstance(result, bool):
            return Evaluation(state=FIRING if result else OK, payload=dict(payload))
        if isinstance(result, tuple) and len(result) == 2:
            severity_like, message = result
            if not isinstance(message, str):
                raise TypeError(
                    f"{type(self).__name__}.evaluate() tuple return must be (severity_or_bool_or_none, message)"
                )
            if isinstance(severity_like, bool):
                return Evaluation(
                    state=FIRING if severity_like else OK,
                    payload=dict(payload),
                    message=message,
                )
            if severity_like is None:
                return Evaluation(state=OK, payload=dict(payload), message=message)
            return Evaluation(
                state=FIRING,
                payload=dict(payload),
                message=message,
                severity=_coerce_severity_value(severity_like),
            )
        raise TypeError(
            f"{type(self).__name__}.evaluate() must return kanary.Evaluation, None, bool, or (severity, message)"
        )

    @classmethod
    def default_rule_id(cls) -> str | None:
        return None

    @classmethod
    def normalized_inputs(cls) -> list[str]:
        return normalize_rule_inputs(
            getattr(cls, "inputs", None),
            source=getattr(cls, "source", None),
        )

    def primary_input_selector(self) -> str | None:
        inputs = normalize_rule_inputs(
            getattr(self, "inputs", None),
            source=getattr(self, "source", None),
        )
        if len(inputs) == 1 and ":" in inputs[0] and "*" not in inputs[0]:
            return inputs[0]
        return None


def ok(
    message: str | None = None,
    *,
    extra: Mapping[str, Any] | None = None,
    payload: Mapping[str, Any] | None = None,
) -> Evaluation:
    return Evaluation(
        state=OK,
        payload=dict(payload) if payload is not None else None,
        extra=dict(extra) if extra is not None else None,
        message=message,
    )


def firing(
    message: str | None = None,
    *,
    severity: Severity | str | None = None,
    extra: Mapping[str, Any] | None = None,
    payload: Mapping[str, Any] | None = None,
) -> Evaluation:
    return Evaluation(
        state=FIRING,
        payload=dict(payload) if payload is not None else None,
        extra=dict(extra) if extra is not None else None,
        message=message,
        severity=_coerce_severity_value(severity) if severity is not None else None,
    )


def info(message: str | None = None, *, extra: Mapping[str, Any] | None = None, payload: Mapping[str, Any] | None = None) -> Evaluation:
    return firing(message, severity=INFO, extra=extra, payload=payload)


def warn(message: str | None = None, *, extra: Mapping[str, Any] | None = None, payload: Mapping[str, Any] | None = None) -> Evaluation:
    return firing(message, severity=WARN, extra=extra, payload=payload)


def error(message: str | None = None, *, extra: Mapping[str, Any] | None = None, payload: Mapping[str, Any] | None = None) -> Evaluation:
    return firing(message, severity=ERROR, extra=extra, payload=payload)


def critical(message: str | None = None, *, extra: Mapping[str, Any] | None = None, payload: Mapping[str, Any] | None = None) -> Evaluation:
    return firing(message, severity=CRITICAL, extra=extra, payload=payload)


def ok_if(
    condition: Any,
    message: str | None = None,
    *,
    extra: Mapping[str, Any] | None = None,
    payload: Mapping[str, Any] | None = None,
) -> Evaluation | None:
    if not condition:
        return None
    return ok(message, extra=extra, payload=payload)


def fire_if(
    condition: Any,
    message: str | None = None,
    *,
    severity: Severity | str | None = None,
    extra: Mapping[str, Any] | None = None,
    payload: Mapping[str, Any] | None = None,
) -> Evaluation | None:
    if not condition:
        return None
    return firing(message, severity=severity, extra=extra, payload=payload)


def info_if(condition: Any, message: str | None = None, *, extra: Mapping[str, Any] | None = None, payload: Mapping[str, Any] | None = None) -> Evaluation | None:
    return fire_if(condition, message, severity=INFO, extra=extra, payload=payload)


def warn_if(condition: Any, message: str | None = None, *, extra: Mapping[str, Any] | None = None, payload: Mapping[str, Any] | None = None) -> Evaluation | None:
    return fire_if(condition, message, severity=WARN, extra=extra, payload=payload)


def error_if(condition: Any, message: str | None = None, *, extra: Mapping[str, Any] | None = None, payload: Mapping[str, Any] | None = None) -> Evaluation | None:
    return fire_if(condition, message, severity=ERROR, extra=extra, payload=payload)


def critical_if(condition: Any, message: str | None = None, *, extra: Mapping[str, Any] | None = None, payload: Mapping[str, Any] | None = None) -> Evaluation | None:
    return fire_if(condition, message, severity=CRITICAL, extra=extra, payload=payload)


class StaleRule(Rule):
    timeout: float
    timestamp_field: str | None = None

    def evaluate(self, ctx: RuleContext) -> Evaluation:
        source_payload = ctx.source_payload()
        selector = self.primary_input_selector()
        matches = ctx.inputs(selector) if self.timestamp_field is None else []
        if self.timestamp_field is None and selector is None:
            matches = ctx.inputs()
        if self.timestamp_field is None and selector is None:
            if not matches:
                return Evaluation(
                    state=AlertState.FIRING,
                    payload=source_payload,
                    message=_missing_field_message(
                        ctx,
                        input_selector=selector,
                        field_label="timestamp",
                        field_path=_selector_label(selector) or "timestamp",
                        field_is_input_derived=selector is not None,
                    ),
                )
            stale_inputs: list[dict[str, Any]] = []
            missing_inputs: list[str] = []
            for item in matches:
                if item.timestamp is None:
                    missing_inputs.append(item.name)
                    continue
                observed_at = _coerce_datetime(item.timestamp)
                age_seconds = (ctx.now - observed_at).total_seconds()
                if age_seconds > self.timeout:
                    stale_inputs.append({"name": item.name, "age_seconds": age_seconds})
            result_payload = dict(source_payload)
            result_payload["stale_inputs"] = stale_inputs
            result_payload["missing_inputs"] = missing_inputs
            if stale_inputs or missing_inputs:
                stale_parts = [f"{row['name']} ({format_time(row['age_seconds'])})" for row in stale_inputs]
                if missing_inputs:
                    stale_parts.extend(f"{name} (timestamp missing)" for name in missing_inputs)
                return Evaluation(
                    state=AlertState.FIRING,
                    payload=result_payload,
                    message=f"stale inputs: {', '.join(stale_parts)}",
                )
            result_payload["age_seconds"] = {
                item.name: (ctx.now - _coerce_datetime(item.timestamp)).total_seconds()
                for item in matches
                if item.timestamp is not None
            }
            return Evaluation(
                state=AlertState.OK,
                payload=result_payload,
                message=f"all inputs are fresh (<= {format_time(self.timeout)})",
            )

        result_payload = dict(source_payload)
        timestamp_field = self._timestamp_field()
        timestamp_value = self._current_timestamp_value(source_payload, ctx)
        selector_label = _selector_label(self.primary_input_selector())

        if timestamp_value is None:
            return Evaluation(
                state=AlertState.FIRING,
                payload=result_payload,
                message=_missing_field_message(
                    ctx,
                    input_selector=selector,
                    field_label="timestamp",
                    field_path=timestamp_field,
                    field_is_input_derived=self.timestamp_field is None and selector_label is not None,
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
        source_id, variable = _default_rule_source_and_variable(
            cls,
            fallback_variable=_field_variable_name(getattr(cls, "timestamp_field", None)),
        )
        if not source_id or not variable:
            return None
        return f"{source_id}.{variable}.stale"

    def _timestamp_field(self) -> str:
        return self.timestamp_field or "timestamp"

    def _current_timestamp_value(self, payload: dict[str, Any], ctx: RuleContext) -> Any:
        if self.timestamp_field is None:
            return ctx.timestamp(self.primary_input_selector())
        return get_by_path(payload, self._timestamp_field())


class RangeRule(Rule):
    field: str | None = None
    low: float | None = None
    high: float | None = None
    hysteresis: float = 0.0
    lower_inclusive: bool = True
    upper_inclusive: bool = True

    def evaluate(self, ctx: RuleContext) -> Evaluation:
        source_payload = ctx.source_payload()
        selector = self.primary_input_selector()
        matches = ctx.inputs(selector) if self.field is None else []
        if self.field is None and selector is None:
            matches = ctx.inputs()
        if self.field is None and selector is None:
            if not matches:
                return Evaluation(
                    state=AlertState.OK,
                    payload=source_payload,
                    message=_missing_field_message(
                        ctx,
                        input_selector=selector,
                        field_label="value",
                        field_path=_selector_label(selector) or "value",
                        field_is_input_derived=selector is not None,
                    ),
                )
            matched_inputs: list[dict[str, Any]] = []
            missing_inputs: list[str] = []
            for item in matches:
                if item.value is None:
                    missing_inputs.append(item.name)
                    continue
                if self._is_out_of_range(item.value):
                    matched_inputs.append({"name": item.name, "value": item.value})
            result_payload = dict(source_payload)
            result_payload["matched_inputs"] = matched_inputs
            result_payload["missing_inputs"] = missing_inputs
            if matched_inputs:
                parts = [f"{row['name']}={row['value']}" for row in matched_inputs]
                return Evaluation(
                    state=AlertState.FIRING,
                    payload=result_payload,
                    message=f"{', '.join(parts)} out of range {self._format_range()}",
                )
            if missing_inputs:
                return Evaluation(
                    state=AlertState.OK,
                    payload=result_payload,
                    message=f"missing values: {', '.join(missing_inputs)}",
                )
            if len(matches) == 1:
                return Evaluation(
                    state=AlertState.OK,
                    payload=result_payload,
                    message=self._build_in_range_message(matches[0].value, matches[0].name),
                )
            return Evaluation(
                state=AlertState.OK,
                payload=result_payload,
                message=f"all inputs within range {self._format_range()}",
            )

        field = self._field()
        field_label = self._field_label()
        value = self._current_field_value(source_payload, ctx)
        result_payload = dict(source_payload)
        selector_label = _selector_label(self.primary_input_selector())

        if value is None:
            return Evaluation(
                state=AlertState.OK,
                payload=result_payload,
                message=_missing_field_message(
                    ctx,
                    input_selector=selector,
                    field_label="value",
                    field_path=field_label,
                    field_is_input_derived=self.field is None and selector_label is not None,
                ),
            )

        previous_value = self._previous_field_value(ctx)
        if self._should_fire(value, previous_value, ctx):
            return Evaluation(
                state=AlertState.FIRING,
                payload=result_payload,
                message=self._build_out_of_range_message(value, field_label),
            )

        return Evaluation(
            state=AlertState.OK,
            payload=result_payload,
            message=self._build_in_range_message(value, field_label),
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
        source_id, variable = _default_rule_source_and_variable(
            cls,
            fallback_variable=_field_variable_name(getattr(cls, "field", None)),
        )
        if not source_id or not variable:
            return None
        return f"{source_id}.{variable}.range"

    def _field(self) -> str:
        return self.field or "value"

    def _field_label(self) -> str:
        return _selector_label(self.primary_input_selector()) or self._field()

    def _previous_field_value(self, ctx: RuleContext) -> Any:
        if self.field is None:
            return ctx.value(self.primary_input_selector(), previous=True)
        if ctx.previous_alert is None:
            return None
        return get_by_path(ctx.previous_alert.payload, self._field())

    def _current_field_value(self, payload: dict[str, Any], ctx: RuleContext) -> Any:
        if self.field is None:
            return ctx.value(self.primary_input_selector())
        return get_by_path(payload, self._field())


class ThresholdRule(Rule):
    field: str | None = None
    thresholds: list[tuple[float, Severity]] = []
    direction: str = "high"
    hysteresis: float = 0.0

    def evaluate(self, ctx: RuleContext) -> Evaluation:
        source_payload = ctx.source_payload()
        selector = self.primary_input_selector()
        matches = ctx.inputs(selector) if self.field is None else []
        if self.field is None and selector is None:
            matches = ctx.inputs()
        if self.field is None and selector is None:
            if not matches:
                return Evaluation(
                    state=AlertState.OK,
                    payload=source_payload,
                    message=_missing_field_message(
                        ctx,
                        input_selector=selector,
                        field_label="value",
                        field_path=_selector_label(selector) or "value",
                        field_is_input_derived=selector is not None,
                    ),
                )
            matched_inputs: list[dict[str, Any]] = []
            missing_inputs: list[str] = []
            highest: Severity | None = None
            for item in matches:
                if item.value is None:
                    missing_inputs.append(item.name)
                    continue
                if not isinstance(item.value, (int, float)):
                    missing_inputs.append(item.name)
                    continue
                matched_severity = self._match_threshold(item.value)
                if matched_severity is None:
                    continue
                matched_inputs.append(
                    {"name": item.name, "value": item.value, "severity": matched_severity.name}
                )
                if highest is None or matched_severity > highest:
                    highest = matched_severity
            result_payload = dict(source_payload)
            result_payload["matched_inputs"] = matched_inputs
            result_payload["missing_inputs"] = missing_inputs
            result_payload["matched_severity"] = highest.name if highest is not None else None
            if highest is not None:
                parts = [f"{row['name']}={row['value']}({row['severity']})" for row in matched_inputs]
                return Evaluation(
                    state=AlertState.FIRING,
                    payload=result_payload,
                    message=f"threshold exceeded: {', '.join(parts)}",
                    severity=highest,
                )
            if missing_inputs:
                return Evaluation(
                    state=AlertState.OK,
                    payload=result_payload,
                    message=f"missing or non-numeric values: {', '.join(missing_inputs)}",
                )
            return Evaluation(
                state=AlertState.OK,
                payload=result_payload,
                message=self._all_inputs_ok_message(),
            )

        field = self._field()
        field_label = self._field_label()
        value = self._current_field_value(source_payload, ctx)
        result_payload = dict(source_payload)
        selector_label = _selector_label(self.primary_input_selector())

        if value is None:
            return Evaluation(
                state=AlertState.OK,
                payload=result_payload,
                message=_missing_field_message(
                    ctx,
                    input_selector=selector,
                    field_label="value",
                    field_path=field_label,
                    field_is_input_derived=self.field is None and selector_label is not None,
                ),
            )
        if not isinstance(value, (int, float)):
            return Evaluation(
                state=AlertState.OK,
                payload=result_payload,
                message=f"{field_label} must be numeric",
            )

        matched_severity = self._match_threshold(value)
        if self.hysteresis > 0 and ctx.was_alerting():
            matched_severity = self._apply_hysteresis(value, matched_severity, ctx.previous_severity)
        result_payload["matched_severity"] = matched_severity.name if matched_severity is not None else None
        if matched_severity is None:
            return Evaluation(
                state=AlertState.OK,
                payload=result_payload,
                message=self._single_value_ok_message(field_label, value),
            )

        return Evaluation(
            state=AlertState.FIRING,
            payload=result_payload,
            message=f"{field_label}={value} reached {matched_severity.name} threshold {self._format_thresholds()}",
            severity=matched_severity,
        )

    @classmethod
    def default_rule_id(cls) -> str | None:
        source_id, variable = _default_rule_source_and_variable(
            cls,
            fallback_variable=_field_variable_name(getattr(cls, "field", None)),
        )
        if not source_id or not variable:
            return None
        return f"{source_id}.{variable}.threshold"

    def _field(self) -> str:
        return self.field or "value"

    def _field_label(self) -> str:
        return _selector_label(self.primary_input_selector()) or self._field()

    def _current_field_value(self, payload: dict[str, Any], ctx: RuleContext) -> Any:
        if self.field is None:
            return ctx.value(self.primary_input_selector())
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

    def _all_inputs_ok_message(self) -> str:
        if self.direction == "low":
            return f"all inputs are above configured low thresholds {self._format_thresholds()}"
        return f"all inputs are below configured high thresholds {self._format_thresholds()}"

    def _single_value_ok_message(self, field_label: str, value: float) -> str:
        if self.direction == "low":
            return f"{field_label}={value} is above configured low thresholds {self._format_thresholds()}"
        return f"{field_label}={value} is below configured high thresholds {self._format_thresholds()}"


class RateRule(RangeRule):
    timestamp_field: str | None = None
    previous_field: str | None = None
    previous_timestamp_field: str | None = None
    per_seconds: float = 1.0

    def evaluate(self, ctx: RuleContext) -> Evaluation:
        source_payload = ctx.source_payload()
        selector = self.primary_input_selector()
        matches = ctx.inputs(selector) if self.field is None and self.timestamp_field is None and self.previous_field is None and self.previous_timestamp_field is None else []
        if self.field is None and self.timestamp_field is None and self.previous_field is None and self.previous_timestamp_field is None and selector is None:
            matches = ctx.inputs()
        if self.field is None and self.timestamp_field is None and self.previous_field is None and self.previous_timestamp_field is None and selector is None:
            if not matches:
                return Evaluation(
                    state=AlertState.OK,
                    payload=source_payload,
                    message=_missing_field_message(
                        ctx,
                        input_selector=selector,
                        field_label="value",
                        field_path=_selector_label(selector) or "value",
                        field_is_input_derived=selector is not None,
                    ),
                )
            matched_inputs: list[dict[str, Any]] = []
            missing_inputs: list[str] = []
            result_payload = dict(source_payload)
            for item in matches:
                previous_value = ctx.prev_value(item.name)
                previous_timestamp = ctx.prev_timestamp(item.name)
                if item.value is None or item.timestamp is None or previous_value is None or previous_timestamp is None:
                    missing_inputs.append(item.name)
                    continue
                if not isinstance(item.value, (int, float)) or not isinstance(previous_value, (int, float)):
                    missing_inputs.append(item.name)
                    continue
                current_observed_at = _coerce_datetime(item.timestamp)
                previous_observed_at = _coerce_datetime(previous_timestamp)
                delta_seconds = (current_observed_at - previous_observed_at).total_seconds()
                if delta_seconds <= 0:
                    missing_inputs.append(item.name)
                    continue
                rate = (item.value - previous_value) / delta_seconds * self.per_seconds
                rate_per_second = (item.value - previous_value) / delta_seconds
                if self._is_out_of_range(rate):
                    matched_inputs.append(
                        {
                            "name": item.name,
                            "rate": rate,
                            "rate_per_second": rate_per_second,
                            "delta_seconds": delta_seconds,
                        }
                    )
            result_payload["matched_inputs"] = matched_inputs
            result_payload["missing_inputs"] = missing_inputs
            if matched_inputs:
                parts = [f"{row['name']}={self._format_rate_message(row['rate'], row['rate_per_second'])}" for row in matched_inputs]
                return Evaluation(
                    state=AlertState.FIRING,
                    payload=result_payload,
                    message=f"rate out of range: {', '.join(parts)}",
                )
            if missing_inputs:
                return Evaluation(
                    state=AlertState.OK,
                    payload=result_payload,
                    message=f"previous samples missing: {', '.join(missing_inputs)}",
                )
            return Evaluation(
                state=AlertState.OK,
                payload=result_payload,
                message=f"all input rates within range {self._format_range()}",
            )

        field = self._field()
        field_label = self._field_label()
        timestamp_field = self._timestamp_field()
        timestamp_label = self._timestamp_label()
        previous_field = self._previous_field()
        previous_timestamp_field = self._previous_timestamp_field()

        current_value = self._current_field_value(source_payload, ctx)
        current_timestamp = self._current_timestamp_value(source_payload, ctx)
        previous_value = self._previous_field_value(ctx)
        previous_timestamp = self._previous_timestamp_value(ctx)
        result_payload = dict(source_payload)
        selector_label = _selector_label(self.primary_input_selector())

        if current_value is None or current_timestamp is None:
            missing_parts: list[str] = []
            if current_value is None:
                missing_parts.append(
                    _missing_field_message(
                        ctx,
                        input_selector=selector,
                        field_label="value",
                        field_path=field_label,
                        field_is_input_derived=self.field is None and selector_label is not None,
                    )
                )
            if current_timestamp is None:
                missing_parts.append(
                    _missing_field_message(
                        ctx,
                        input_selector=selector,
                        field_label="timestamp",
                        field_path=timestamp_label,
                        field_is_input_derived=self.timestamp_field is None and selector_label is not None,
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
                message=f"previous {field_label} sample is missing",
            )
        if not isinstance(current_value, (int, float)) or not isinstance(previous_value, (int, float)):
            return Evaluation(
                state=AlertState.OK,
                payload=result_payload,
                message=f"{field_label} samples must be numeric",
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
                    f"{field_label} rate={self._format_rate_message(rate, rate_per_second)} "
                    f"out of range {self._format_range()}"
                ),
            )

        return Evaluation(
            state=AlertState.OK,
            payload=result_payload,
            message=(
                f"{field_label} rate={self._format_rate_message(rate, rate_per_second)} "
                f"within range {self._format_range()}"
            ),
        )

    @classmethod
    def default_rule_id(cls) -> str | None:
        source_id, variable = _default_rule_source_and_variable(
            cls,
            fallback_variable=_field_variable_name(getattr(cls, "field", None)),
        )
        if not source_id or not variable:
            return None
        return f"{source_id}.{variable}.rate"

    def _timestamp_field(self) -> str:
        return self.timestamp_field or "timestamp"

    def _timestamp_label(self) -> str:
        selector = self.primary_input_selector()
        if selector is not None and self.timestamp_field is None:
            return f"{_selector_label(selector)} timestamp"
        return self._timestamp_field()

    def _previous_field(self) -> str:
        return self.previous_field or self._field()

    def _previous_timestamp_field(self) -> str:
        return self.previous_timestamp_field or self._timestamp_field()

    def _current_timestamp_value(self, payload: dict[str, Any], ctx: RuleContext) -> Any:
        if self.timestamp_field is None:
            return ctx.timestamp(self.primary_input_selector())
        return get_by_path(payload, self._timestamp_field())

    def _previous_timestamp_value(self, ctx: RuleContext) -> Any:
        if self.previous_timestamp_field is None and self.timestamp_field is None:
            return ctx.timestamp(self.primary_input_selector(), previous=True)
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
    input_selector: str | None,
    field_label: str,
    field_path: str,
    field_is_input_derived: bool,
) -> str:
    if not field_is_input_derived or not input_selector:
        return f"{field_path} is missing"

    if ctx.inputs(input_selector):
        return f"input '{_selector_label(input_selector) or input_selector}' is present but {field_label} is missing"

    available = ctx.names()
    message = f"input '{_selector_label(input_selector) or input_selector}' is missing"
    closest = get_close_matches(input_selector, available, n=1)
    if closest:
        message += f"; closest available input: {closest[0]}"
    if available:
        shown = ", ".join(sorted(available)[:5])
        if len(available) > 5:
            shown += ", ..."
        message += f"; available inputs: {shown}"
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


def _default_rule_source_and_variable(
    cls: type[Any],
    *,
    fallback_variable: str | None = None,
) -> tuple[str | None, str | None]:
    source_id = getattr(cls, "source", None)
    variable = fallback_variable
    if source_id and variable:
        return source_id, variable
    inputs = normalize_rule_inputs(
        getattr(cls, "inputs", None),
        source=getattr(cls, "source", None),
    )
    if len(inputs) == 1 and ":" in inputs[0] and "*" not in inputs[0]:
        source_id, variable = inputs[0].split(":", 1)
        return source_id, variable
    return source_id, variable


def _selector_label(selector: str | None) -> str | None:
    if selector is None:
        return None
    if ":" in selector and "*" not in selector:
        return selector.split(":", 1)[1]
    return selector


def _split_input_selector(selector: str) -> tuple[str | None, str]:
    if ":" not in selector:
        return None, selector
    source_pattern, input_pattern = selector.split(":", 1)
    return source_pattern or None, input_pattern or "*"


def _qualify_input_name(source_id: str, input_name: str) -> str:
    return f"{source_id}:{input_name}"


def _normalize_runtime_selectors(
    selector: str | Iterable[str] | None,
    declared_inputs: tuple[str, ...],
) -> list[str]:
    if selector is None:
        return list(declared_inputs)
    if isinstance(selector, str):
        return [selector]
    return [str(item) for item in selector]


def _matches_input_selector(input_name: str, selector: str) -> bool:
    source_id, measurement_name = input_name.split(":", 1)
    source_pattern, measurement_pattern = _split_input_selector(selector)
    if source_pattern is None:
        return source_id == source_pattern if source_pattern is not None else fnmatch(measurement_name, measurement_pattern)
    return fnmatch(source_id, source_pattern) and fnmatch(measurement_name, measurement_pattern)


def _matches_any_input_selector(input_name: str, selectors: list[str]) -> bool:
    for selector in selectors:
        if _matches_input_selector(input_name, selector):
            return True
    return False


def _coerce_severity_value(value: Any) -> Severity:
    if isinstance(value, Severity):
        return value
    if isinstance(value, str):
        normalized = value.strip().upper()
        return Severity[normalized]
    raise TypeError("severity must be kanary.INFO/WARN/ERROR/CRITICAL or a matching string")


def normalize_rule_inputs(inputs: Any, *, source: str | None = None) -> list[str]:
    if inputs is None or inputs == []:
        if isinstance(source, str) and source:
            return [f"{source}:*"]
        return []
    if isinstance(inputs, str):
        return [inputs]
    if isinstance(inputs, list) and all(isinstance(item, str) for item in inputs):
        return list(inputs)
    raise ValueError("inputs must be a string or list[str]")


def resolve_rule_sources(inputs: Iterable[str], available_source_ids: Iterable[str]) -> list[str]:
    resolved: set[str] = set()
    source_ids = list(available_source_ids)
    for selector in inputs:
        source_pattern, _ = _split_input_selector(selector)
        for source_id in source_ids:
            if source_pattern is None or fnmatch(source_id, source_pattern):
                resolved.add(source_id)
    return sorted(resolved)


def prepare_rule_class(cls: type[Any]) -> type[Any]:
    rule_id = getattr(cls, "rule_id", None)
    if not isinstance(rule_id, str) or not rule_id:
        raise ValueError(f"rule '{cls.__name__}' must define non-empty string rule_id")

    if getattr(cls, "measurement", None) is not None:
        raise ValueError(
            f"rule '{rule_id}' measurement is no longer supported; use inputs='source_id:input_name'"
        )

    source = getattr(cls, "source", None)
    if source is not None and (not isinstance(source, str) or not source):
        raise ValueError(f"rule '{rule_id}' source must be non-empty string when set")

    try:
        inputs = normalize_rule_inputs(getattr(cls, "inputs", None), source=source)
    except ValueError as exc:
        raise ValueError(f"rule '{rule_id}' {exc}") from exc
    if not inputs:
        raise ValueError(f"rule '{rule_id}' must define source or inputs")

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
        raise ValueError(f"rule '{rule_id}' must implement evaluate(ctx)")
    try:
        cls.__kanary_evaluate_style__ = detect_instance_method_style(
            evaluate,
            new_arity=1,
            legacy_arity=2,
            new_signature="evaluate(self, ctx)",
            legacy_signature="evaluate(self, payload, ctx)",
        )
    except ValueError as exc:
        raise ValueError(f"rule '{rule_id}' {exc}") from exc

    _setdefault(cls, "owner", None)
    _setdefault(cls, "description", None)
    _setdefault(cls, "runbook", None)
    _setdefault(cls, "depends_on", [])
    _setdefault(cls, "suppressed_by", [])
    _setdefault(cls, "matched_outputs", [])
    cls.inputs = inputs
    _setdefault(cls, "resolved_sources", [])

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
    if "default_rule_id" not in cls.__dict__:
        cls.default_rule_id = classmethod(lambda inner_cls: getattr(inner_cls, "rule_id", None))
    return cls


def _setdefault(cls: type[Any], attr_name: str, value: Any) -> None:
    if hasattr(cls, attr_name):
        return
    setattr(cls, attr_name, value)
