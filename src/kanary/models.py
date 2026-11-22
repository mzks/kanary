from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .constants import AlertState, Severity, TransitionKind
from .values import plain_value, to_datetime


@dataclass(slots=True)
class Evaluation:
    state: AlertState
    payload: dict[str, Any] | None = None
    extra: dict[str, Any] | None = None
    message: str | None = None
    severity: Severity | None = None


@dataclass(slots=True)
class Alert:
    rule_id: str
    state: AlertState
    severity: Severity
    owner: str | None = None
    tags: tuple[str, ...] = ()
    payload: dict[str, Any] = field(default_factory=dict)
    message: str | None = None
    active_since: datetime | None = None
    last_evaluated_at: datetime | None = None
    acked_at: datetime | None = None
    acked_by: str | None = None
    ack_reason: str | None = None
    active_silence_ids: tuple[str, ...] = ()
    runbook: str | None = None  # the rule's runbook, shown with its notifications
    # Restored at start from the state database and not evaluated since: the state
    # KANARY stopped with. The rule's first evaluation replaces it.
    restored: bool = False


@dataclass(slots=True)
class Acknowledgement:
    rule_id: str
    operator: str
    reason: str | None
    created_at: datetime


@dataclass(slots=True)
class Silence:
    silence_id: str
    created_by: str
    reason: str | None
    created_at: datetime
    start_at: datetime
    end_at: datetime
    rule_patterns: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    remote_silence_refs: tuple[str, ...] = ()
    cancelled_at: datetime | None = None
    cancelled_by: str | None = None
    cancel_reason: str | None = None


@dataclass(slots=True)
class SourceSnapshot:
    payload: dict[str, Any] = field(default_factory=dict)
    observed_at: datetime | None = None


@dataclass(slots=True)
class SourceState:
    source_id: str
    current: SourceSnapshot = field(default_factory=SourceSnapshot)
    previous: SourceSnapshot = field(default_factory=SourceSnapshot)
    updated_at: datetime | None = None
    poll_count: int = 0


@dataclass(slots=True)
class InputValue:
    """One input of a poll result: a name, its value, when it was observed, and metadata."""

    name: str
    value: Any
    timestamp: datetime
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Every input value and time enters here: Decimal and numpy numbers become
        # Python numbers, a time without a time zone is this host's local time.
        self.value = plain_value(self.value)
        if self.timestamp is not None:
            self.timestamp = to_datetime(self.timestamp)


@dataclass(slots=True)
class PollResult:
    """What a source's poll() returns: kanary.inputs(), no_data(), no_update(), or skip()."""

    inputs: list[InputValue] = field(default_factory=list)
    status: str = "ok"
    reason: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class AlertEvent:
    rule_id: str
    previous_state: AlertState | None
    current_state: AlertState
    previous_severity: Severity | None
    current_severity: Severity
    transition: TransitionKind | None
    alert: Alert
    occurred_at: datetime

    @property
    def effective_severity(self) -> Severity:
        if self.current_state == AlertState.OK and self.previous_severity is not None:
            return self.previous_severity
        return self.current_severity

    @property
    def state(self) -> AlertState:
        return self.current_state

    @property
    def severity(self) -> Severity:
        return self.current_severity

    @property
    def message(self) -> str | None:
        return self.alert.message

    @property
    def payload(self) -> dict[str, Any]:
        return self.alert.payload

    @property
    def owner(self) -> str | None:
        return self.alert.owner

    @property
    def tags(self) -> tuple[str, ...]:
        return self.alert.tags

    @property
    def runbook(self) -> str | None:
        return self.alert.runbook

    @property
    def active_since(self) -> datetime | None:
        return self.alert.active_since

    @property
    def acked_at(self) -> datetime | None:
        return self.alert.acked_at

    @property
    def acked_by(self) -> str | None:
        return self.alert.acked_by

    @property
    def ack_reason(self) -> str | None:
        return self.alert.ack_reason

    @property
    def active_silence_ids(self) -> tuple[str, ...]:
        return self.alert.active_silence_ids


@dataclass(slots=True)
class PluginStatus:
    plugin_type: str
    plugin_id: str
    state: str = "created"
    loaded: bool = True
    dirty_reason: str | None = None
    definition_file: str | None = None
    init_ok: bool = False
    last_error: str | None = None
    last_error_detail: str | None = None
    run_count: int = 0
    last_run_at: datetime | None = None
    last_success_at: datetime | None = None
    last_failure_at: datetime | None = None
    last_updated_at: datetime | None = None
    warning: str | None = None  # e.g. a rule whose evaluate() is slow


def normalize_source_output(
    result: Any,
    *,
    now: datetime | None = None,
) -> PollResult:
    if isinstance(result, PollResult):
        return result
    default_timestamp = now
    if isinstance(result, Mapping):
        return PollResult(inputs=_inputs_from_mapping(result, default_timestamp=default_timestamp))
    if isinstance(result, list):
        return PollResult(inputs=_inputs_from_iterable(result, default_timestamp=default_timestamp))
    if isinstance(result, tuple):
        return PollResult(inputs=normalize_input_items(result, default_timestamp=default_timestamp))
    raise TypeError(
        "source poll() must return kanary.inputs(...), kanary.no_data(), kanary.no_update(), "
        "kanary.skip(), a dict[name, value], or a list/tuple of input tuples"
    )


def normalize_input_items(
    items: tuple[Any, ...],
    *,
    default_timestamp: datetime | None,
) -> list[InputValue]:
    if not items:
        return []
    if len(items) >= 2 and isinstance(items[0], str):
        return [_input_from_named_item(tuple(items), default_timestamp=default_timestamp)]
    if len(items) == 1:
        first = items[0]
        if isinstance(first, Mapping):
            return _inputs_from_mapping(first, default_timestamp=default_timestamp)
        if isinstance(first, list):
            return _inputs_from_iterable(first, default_timestamp=default_timestamp)
        if isinstance(first, tuple):
            return [_input_from_named_item(first, default_timestamp=default_timestamp)]
    return _inputs_from_iterable(items, default_timestamp=default_timestamp)


def _inputs_from_mapping(
    mapping: Mapping[str, Any],
    *,
    default_timestamp: datetime | None,
) -> list[InputValue]:
    values: list[InputValue] = []
    for name, raw in mapping.items():
        if not isinstance(name, str) or not name:
            raise ValueError("inputs mapping keys must be non-empty strings")
        values.append(
            _input_from_mapping_value(name, raw, default_timestamp=default_timestamp)
        )
    return values


def _inputs_from_iterable(
    items: Iterable[Any],
    *,
    default_timestamp: datetime | None,
) -> list[InputValue]:
    values: list[InputValue] = []
    for item in items:
        if isinstance(item, InputValue):
            values.append(item)
            continue
        if not isinstance(item, tuple):
            raise ValueError(
                "an input must be a (name, value, ...) tuple or a sample of a kanary.Buffer or "
                f"kanary.Inbox, not {type(item).__name__}"
            )
        values.append(_input_from_named_item(item, default_timestamp=default_timestamp))
    return values


def _input_from_mapping_value(
    name: str,
    raw: Any,
    *,
    default_timestamp: datetime | None,
) -> InputValue:
    if isinstance(raw, Mapping):
        value = raw.get("value")
        timestamp = _resolve_timestamp(raw.get("timestamp"), default_timestamp)
        metadata = _coerce_item_metadata(raw.get("metadata"))
        return InputValue(name=name, value=value, timestamp=timestamp, metadata=metadata)
    if isinstance(raw, tuple):
        length = len(raw)
        if length == 0 or length > 3:
            raise ValueError("mapping tuple values must be (value), (value, timestamp), or (value, timestamp, metadata)")
        value = raw[0]
        timestamp = _resolve_timestamp(raw[1] if length >= 2 else None, default_timestamp)
        metadata = _coerce_item_metadata(raw[2] if length >= 3 else None)
        return InputValue(name=name, value=value, timestamp=timestamp, metadata=metadata)
    return InputValue(
        name=name,
        value=raw,
        timestamp=_resolve_timestamp(None, default_timestamp),
        metadata={},
    )


def _input_from_named_item(
    item: tuple[Any, ...],
    *,
    default_timestamp: datetime | None,
) -> InputValue:
    if len(item) < 2 or len(item) > 4:
        raise ValueError("input tuples must be (name, value), (name, value, timestamp), or (name, value, timestamp, metadata)")
    name = item[0]
    if not isinstance(name, str) or not name:
        raise ValueError("input tuple name must be a non-empty string")
    value = item[1]
    timestamp = _resolve_timestamp(item[2] if len(item) >= 3 else None, default_timestamp)
    metadata = _coerce_item_metadata(item[3] if len(item) >= 4 else None)
    return InputValue(name=name, value=value, timestamp=timestamp, metadata=metadata)


def _coerce_item_metadata(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    return {"value": value}


def _resolve_timestamp(
    value: datetime | float | str | None,
    default_timestamp: datetime | None,
) -> datetime:
    if value is not None:
        try:
            return to_datetime(value)
        except (TypeError, ValueError) as exc:
            raise TypeError(
                f"an input timestamp must be a datetime, ISO 8601 text, unix time, or None, not {value!r}"
            ) from exc
    if isinstance(default_timestamp, datetime):
        return default_timestamp
    return datetime.now(timezone.utc)
