from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .constants import AlertState, Severity


@dataclass(slots=True)
class Evaluation:
    state: AlertState
    payload: dict[str, Any] = field(default_factory=dict)
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
    resolved_at: datetime | None = None
    acked_at: datetime | None = None
    acked_by: str | None = None
    ack_reason: str | None = None
    active_silence_ids: tuple[str, ...] = ()


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
class Measurement:
    name: str
    value: Any
    timestamp: datetime
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class SourceResult:
    measurements: list[Measurement] = field(default_factory=list)
    status: str = "ok"
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class AlertEvent:
    rule_id: str
    previous_state: AlertState | None
    current_state: AlertState
    alert: Alert
    occurred_at: datetime


@dataclass(slots=True)
class PluginStatus:
    plugin_type: str
    plugin_id: str
    state: str = "created"
    init_ok: bool = False
    last_error: str | None = None
    run_count: int = 0
    last_run_at: datetime | None = None
    last_success_at: datetime | None = None
    last_failure_at: datetime | None = None
    last_updated_at: datetime | None = None
