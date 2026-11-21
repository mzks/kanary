from typing import Any

from .models import AlertEvent
from .constants import Severity, coerce_severity, severity_label
from .patterns import matches_any_tag, matches_excluded_tag


_DEFAULT_EXCLUDED_STATES = ("SUPPRESSED", "SILENCED")



class Output:
    output_id: str
    description: str | None = None
    include_tags: list[str] = []
    exclude_tags: list[str] = []
    exclude_states: list[str] = list(_DEFAULT_EXCLUDED_STATES)
    exclude_transitions: list[str] = []
    minimum_severity: str | Severity | None = None
    max_retry: int = 1
    max_reinit: int = 1

    def init(self) -> None:
        return None

    def emit(self, event: AlertEvent) -> None:
        raise NotImplementedError

    def terminate(self) -> None:
        return None

    def matches(self, event: AlertEvent) -> bool:
        alert_tags = set(event.alert.tags)
        if self.include_tags and not matches_any_tag(alert_tags, self.include_tags):
            return False
        if self.exclude_tags and matches_excluded_tag(alert_tags, self.exclude_tags):
            return False
        state = event.current_state.value
        if self.exclude_states and state in self.exclude_states:
            return False
        transition = event.transition.value if event.transition is not None else None
        if transition is not None and self.exclude_transitions and transition in self.exclude_transitions:
            return False
        minimum_severity = None if self.minimum_severity is None else coerce_severity(self.minimum_severity)
        if minimum_severity is not None and event.effective_severity < minimum_severity:
            return False
        return True


def event_record(event: AlertEvent) -> dict[str, Any]:
    """A JSON-ready dict of the event fields that outputs usually send."""
    return {
        "rule_id": event.rule_id,
        "state": event.current_state.value,
        "previous_state": event.previous_state.value if event.previous_state else None,
        "transition": event.transition.value if event.transition else None,
        "severity": severity_label(event.effective_severity),
        "message": event.message,
        "owner": event.owner,
        "tags": list(event.tags),
        "occurred_at": event.occurred_at.isoformat(),
        "active_since": event.active_since.isoformat() if event.active_since else None,
        "acked_by": event.acked_by,
        "runbook": event.runbook,
    }
