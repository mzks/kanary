from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .constants import AlertState, Severity, CRITICAL, ERROR, FIRING, INFO, OK, WARN, coerce_severity
from .models import Alert, Evaluation, SourceState
from .selectors import (
    normalize_rule_inputs,
    normalize_runtime_selectors,
    qualify_input_name,
    select_inputs,
)
from .values import coerce_datetime, is_number


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
        selectors = normalize_runtime_selectors(selector, self.declared_inputs)
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
            for input_name in select_inputs(source_id, channels, selectors):
                raw = channels[input_name]
                full_name = qualify_input_name(source_id, str(input_name))
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

    def age(self, name: str | None = None, default: Any = None) -> float | None:
        """Seconds between the input's timestamp and ctx.now."""
        timestamp = self.timestamp(name)
        if timestamp is None:
            return default
        return (self.now - coerce_datetime(timestamp)).total_seconds()

    def delta(self, name: str | None = None, default: Any = None) -> float | None:
        """Current value minus the previous value; default when either is not a number."""
        current = self.value(name)
        previous = self.prev_value(name)
        if not is_number(current) or not is_number(previous):
            return default
        return current - previous

    def changed(self, name: str | None = None) -> bool:
        """True when both the current and the previous value exist and differ."""
        current = self.value(name)
        previous = self.prev_value(name)
        return current is not None and previous is not None and current != previous

    def missing(self, *selectors: str) -> list[str]:
        """Selectors (default: the rule's inputs) that match nothing or only have no value."""
        wanted = list(selectors) or list(self.declared_inputs)
        missing: list[str] = []
        for selector in wanted:
            matches = self.inputs(selector)
            if not matches:
                missing.append(selector)
                continue
            missing.extend(item.name for item in matches if item.value is None)
        return missing


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
    evaluation_interval: float | None = None

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
                severity=coerce_severity(severity_like),
            )
        raise TypeError(
            f"{type(self).__name__}.evaluate() must return kanary.Evaluation, None, bool, or (severity, message)"
        )

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
        severity=coerce_severity(severity) if severity is not None else None,
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
