"""What a notification about an alert event shows, independent of where it goes.

The chat and mail outputs build their rich messages from describe(event); an
output of your own (Teams, LINE, a pager) can use it the same way::

    notice = kanary.describe(event)
    notice.headline   # "🔴 FIRING ERROR oil.temperature"
    notice.message    # the rule's message
    notice.color_hex  # "#E74C3C": green when OK, grey when silenced, else by severity
    notice.fields     # [("State", "OK → FIRING"), ("Severity", "ERROR"), ..., ("Runbook", ...)]
    notice.text       # one line, for previews and plain-text receivers
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..constants import AlertState, Severity, severity_label
from ..models import AlertEvent
from ..values import is_number

# Colors as 0xRRGGBB.
COLORS = {
    "OK": 0x2ECC71,
    "MUTED": 0x95A5A6,  # silenced, suppressed
    "ACKED": 0x5D7B93,
    Severity.INFO: 0x3498DB,
    Severity.WARN: 0xF1C40F,
    Severity.ERROR: 0xE74C3C,
    Severity.CRITICAL: 0x8E44AD,
}
SYMBOLS = {
    "OK": "✅",
    "SILENCED": "🔕",
    "SUPPRESSED": "⏸️",
    "ACKED": "👀",
    Severity.INFO: "🔵",
    Severity.WARN: "🟡",
    Severity.ERROR: "🔴",
    Severity.CRITICAL: "🚨",
}
MAX_VALUES = 10  # input values listed in the "Values" field


@dataclass(slots=True)
class Notice:
    """The parts of a rich notification; see describe()."""

    title: str  # "FIRING ERROR oil.temperature"
    symbol: str  # "🔴"
    color: int  # 0xRRGGBB
    message: str  # the rule's message, "" when there is none
    fields: list[tuple[str, str]] = field(default_factory=list)
    time: str = ""  # when it happened, in the host's local time
    text: str = ""  # one-line summary
    link: str | None = None  # the viewer, when the output sets viewer_url

    @property
    def headline(self) -> str:
        return f"{self.symbol} {self.title}"

    @property
    def color_hex(self) -> str:
        return f"#{self.color:06X}"


def describe(event: AlertEvent, *, viewer_url: str | None = None) -> Notice:
    """The title, color, fields, and summary of a notification about ``event``."""
    state = event.current_state
    severity = event.effective_severity
    marker = event.transition.value if event.transition is not None else state.value
    if state == AlertState.OK:
        title = f"OK {event.rule_id}"
        key: Any = "OK"
    else:
        title = f"{marker} {severity_label(severity)} {event.rule_id}"
        key = state.value if state in (AlertState.SILENCED, AlertState.SUPPRESSED, AlertState.ACKED) else severity
    color = COLORS["MUTED"] if key in ("SILENCED", "SUPPRESSED") else COLORS.get(key, COLORS["MUTED"])
    return Notice(
        title=title,
        symbol=SYMBOLS.get(key, "•"),
        color=color,
        message=event.message or "",
        fields=event_fields(event),
        time=local_time(event.occurred_at),
        text=summary(event),
        link=viewer_url or None,
    )


@dataclass(slots=True)
class Digest:
    """Several events as one message; see digest()."""

    title: str  # "7 alert events, 3 rules"
    color: int  # of the most severe rule still alerting; green when all are OK
    entries: list[tuple[Notice, int]] = field(default_factory=list)  # newest notice per rule, events of it
    text: str = ""  # one line

    @property
    def color_hex(self) -> str:
        return f"#{self.color:06X}"

    def lines(self) -> list[str]:
        """One line per rule: "🔴 FIRING ERROR oil.temperature (3 events): value=95 ..."."""
        lines = []
        for notice, count in self.entries:
            line = notice.headline + (f" ({count} events)" if count > 1 else "")
            lines.append(f"{line}: {notice.message}" if notice.message else line)
        return lines


def digest(events: list[AlertEvent], *, title: str | None = None, viewer_url: str | None = None) -> Digest:
    """Group events by rule, newest state each, for one message instead of many."""
    newest: dict[str, AlertEvent] = {}
    counts: dict[str, int] = {}
    for event in events:
        newest[event.rule_id] = event
        counts[event.rule_id] = counts.get(event.rule_id, 0) + 1
    entries = [(describe(event, viewer_url=viewer_url), counts[rule_id]) for rule_id, event in newest.items()]
    alerting = [event for event in newest.values() if event.current_state != AlertState.OK]
    if alerting:
        worst = max(alerting, key=lambda event: event.effective_severity)
        color = describe(worst).color
    else:
        color = COLORS["OK"]
    heading = title or f"{len(events)} alert events, {len(newest)} rule{'s' if len(newest) != 1 else ''}"
    still = f"{len(alerting)} still alerting" if alerting else "all OK now"
    return Digest(title=heading, color=color, entries=entries, text=f"{heading}: {still}")


def summary(event: AlertEvent) -> str:
    """One line: "[ERROR] FIRING oil.temperature: value=95 out of range [10, 80]".

    Back to OK it has no severity, as the title: "OK oil.temperature: value=42 within range".
    """
    if event.current_state == AlertState.OK:
        line = f"OK {event.rule_id}"
    else:
        marker = event.transition.value if event.transition is not None else event.current_state.value
        line = f"[{severity_label(event.effective_severity)}] {marker} {event.rule_id}"
    return f"{line}: {event.message}" if event.message else line


def event_fields(event: AlertEvent) -> list[tuple[str, str]]:
    """(label, value) pairs worth showing next to the message, in display order."""
    fields: list[tuple[str, str]] = []
    state = event.current_state.value
    previous_state = event.previous_state.value if event.previous_state is not None else None
    fields.append(("State", f"{previous_state} → {state}" if previous_state and previous_state != state else state))
    if event.current_state == AlertState.OK:
        if event.previous_severity is not None:
            fields.append(("Severity", f"was {severity_label(event.previous_severity)}"))
    else:
        current = severity_label(event.current_severity)
        previous = event.previous_severity
        changed = previous is not None and previous != event.current_severity and event.previous_state not in (None, AlertState.OK)
        fields.append(("Severity", f"{severity_label(previous)} → {current}" if changed else current))
        if event.active_since is not None and event.active_since != event.occurred_at:
            fields.append(("Since", local_time(event.active_since)))  # an alert that began earlier
    if event.acked_by:
        fields.append(("Acked by", f"{event.acked_by} ({event.ack_reason})" if event.ack_reason else event.acked_by))
    if event.owner:
        fields.append(("Owner", event.owner))
    if event.tags:
        fields.append(("Tags", ", ".join(event.tags)))
    values = event.payload.get("values") if isinstance(event.payload, dict) else None
    if isinstance(values, dict) and values:
        shown = [f"{name} = {_format_value(value)}" for name, value in list(values.items())[:MAX_VALUES]]
        if len(values) > MAX_VALUES:
            shown.append(f"… {len(values) - MAX_VALUES} more")
        fields.append(("Values", "\n".join(shown)))
    if event.alert.restored:
        # Sent before the rule was evaluated again (a silence that ended while KANARY was
        # stopped): the message and values are from before the restart.
        evaluated = event.alert.last_evaluated_at
        fields.append(("Evaluated", f"{local_time(evaluated)}, before the restart" if evaluated else "before the restart"))
    if event.runbook:
        fields.append(("Runbook", event.runbook))  # the rule's: what to do, a link to a dashboard
    return fields


def local_time(moment: datetime) -> str:
    return moment.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z").strip()


def cut(text: str, limit: int) -> str:
    """text shortened to at most ``limit`` characters (receivers reject longer ones)."""
    return text if len(text) <= limit else text[: max(0, limit - 1)] + "…"


def _format_value(value: Any) -> str:
    return f"{value:g}" if is_number(value) else str(value)
