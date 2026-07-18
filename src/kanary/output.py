from collections.abc import Callable
from dataclasses import dataclass, field
from email.message import EmailMessage
import heapq
import logging
import math
import os
import smtplib
import threading
import time
from typing import Any

from .models import AlertEvent
from .constants import Severity, severity_label
from .patterns import matches_any_tag, matches_excluded_tag
from .signature_compat import detect_instance_method_style


_DEFAULT_EXCLUDED_STATES = ("SUPPRESSED", "SILENCED")

logger = logging.getLogger("kanary.output")


@dataclass(slots=True)
class _FollowupEntry:
    callback: Callable[[AlertEvent], None]
    callback_key: tuple[int, int]


@dataclass(slots=True)
class _FollowupEpisode:
    started_at: float
    event: AlertEvent
    entries: dict[int, _FollowupEntry] = field(default_factory=dict)


class _EventFollowups:
    def __init__(self, owner: "OutputFollowups", event: AlertEvent) -> None:
        self._owner = owner
        self._event = event

    def now(self, callback: Callable[[AlertEvent], None]) -> None:
        """Run callback synchronously with the current event."""
        if not callable(callback):
            raise TypeError("followup callback must be callable")
        callback(self._event)

    def after(
        self,
        delay: float,
        callback: Callable[[AlertEvent], None],
    ) -> None:
        """Run callback after delay from the start of this alert episode."""
        self._owner._schedule(self._event, delay, callback)

    def cancel(
        self,
        callback: Callable[[AlertEvent], None] | None = None,
    ) -> None:
        """Cancel all followups for this event, or only one callback."""
        self._owner._cancel(self._event.rule_id, callback)


class OutputFollowups:
    """Schedule in-memory followups for the latest event of each rule."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._episodes: dict[str, _FollowupEpisode] = {}
        self._heap: list[tuple[float, int, str]] = []
        self._sequence = 0
        self._worker: threading.Thread | None = None
        self._closed = False

    def for_event(self, event: AlertEvent) -> _EventFollowups:
        """Select an event and refresh callbacks with its latest state."""
        with self._condition:
            self._ensure_open()
            episode = self._episodes.get(event.rule_id)
            if episode is not None:
                episode.event = event
        return _EventFollowups(self, event)

    def close(self) -> None:
        """Cancel pending followups and stop the worker thread."""
        with self._condition:
            if self._closed:
                return
            self._closed = True
            self._episodes.clear()
            self._heap.clear()
            worker = self._worker
            self._condition.notify_all()

        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=2.0)
            if worker.is_alive():
                logger.warning("output followup worker did not stop before timeout")

    def _schedule(
        self,
        event: AlertEvent,
        delay: float,
        callback: Callable[[AlertEvent], None],
    ) -> None:
        try:
            delay_seconds = float(delay)
        except (TypeError, ValueError) as exc:
            raise ValueError("followup delay must be a finite non-negative number") from exc
        if not math.isfinite(delay_seconds) or delay_seconds < 0:
            raise ValueError("followup delay must be a finite non-negative number")
        if not callable(callback):
            raise TypeError("followup callback must be callable")

        with self._condition:
            self._ensure_open()
            episode = self._episodes.get(event.rule_id)
            if episode is None:
                episode = _FollowupEpisode(started_at=time.monotonic(), event=event)
                self._episodes[event.rule_id] = episode
            else:
                episode.event = event

            self._sequence += 1
            sequence = self._sequence
            episode.entries[sequence] = _FollowupEntry(
                callback=callback,
                callback_key=_callback_key(callback),
            )
            heapq.heappush(
                self._heap,
                (episode.started_at + delay_seconds, sequence, event.rule_id),
            )
            self._start_worker()
            self._condition.notify_all()

    def _cancel(
        self,
        rule_id: str,
        callback: Callable[[AlertEvent], None] | None,
    ) -> None:
        with self._condition:
            self._ensure_open()
            episode = self._episodes.get(rule_id)
            if episode is None:
                return
            if callback is None:
                del self._episodes[rule_id]
            else:
                callback_key = _callback_key(callback)
                episode.entries = {
                    sequence: entry
                    for sequence, entry in episode.entries.items()
                    if entry.callback_key != callback_key
                }
                if not episode.entries:
                    del self._episodes[rule_id]
            self._condition.notify_all()

    def _start_worker(self) -> None:
        if self._worker is not None:
            return
        self._worker = threading.Thread(
            target=self._run,
            name="kanary-output-followups",
            daemon=True,
        )
        self._worker.start()

    def _run(self) -> None:
        while True:
            with self._condition:
                due = self._next_due()
                while due is None:
                    if self._closed:
                        return
                    self._condition.wait()
                    due = self._next_due()

                deadline, sequence, rule_id = due
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    self._condition.wait(timeout=remaining)
                    continue

                heapq.heappop(self._heap)
                episode = self._episodes.get(rule_id)
                if episode is None:
                    continue
                entry = episode.entries.pop(sequence, None)
                if entry is None:
                    continue
                event = episode.event
                if not episode.entries:
                    del self._episodes[rule_id]

            try:
                entry.callback(event)
            except Exception:
                logger.exception(
                    "output followup callback failed: rule=%s callback=%r",
                    rule_id,
                    entry.callback,
                )

    def _next_due(self) -> tuple[float, int, str] | None:
        while self._heap:
            due = self._heap[0]
            _deadline, sequence, rule_id = due
            episode = self._episodes.get(rule_id)
            if episode is not None and sequence in episode.entries:
                return due
            heapq.heappop(self._heap)
        return None

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("OutputFollowups is closed")


def _callback_key(callback: Callable[[AlertEvent], None]) -> tuple[int, int]:
    instance = getattr(callback, "__self__", None)
    function = getattr(callback, "__func__", callback)
    return id(instance), id(function)


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
        minimum_severity = _coerce_minimum_severity(self.minimum_severity)
        if minimum_severity is not None and event.effective_severity < minimum_severity:
            return False
        return True


class MailOutput(Output):
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_timeout_seconds: float = 10.0
    use_starttls: bool = True
    sender: str | None = None
    recipients: list[str] = []
    subject_prefix: str = "[KANARY]"

    def init(self) -> None:
        self.smtp_host = self.smtp_host or os.environ.get("KANARY_SMTP_HOST")
        self.smtp_port = int(os.environ.get("KANARY_SMTP_PORT", str(self.smtp_port)))
        self.smtp_username = self.smtp_username or os.environ.get("KANARY_SMTP_USER")
        self.smtp_password = self.smtp_password or os.environ.get("KANARY_SMTP_PASSWORD")
        self.sender = self.sender or os.environ.get("KANARY_SMTP_SENDER")
        if not self.recipients:
            raw_recipients = os.environ.get("KANARY_SMTP_RECIPIENTS", "")
            self.recipients = [item.strip() for item in raw_recipients.split(",") if item.strip()]
        if not self.smtp_host:
            raise RuntimeError("KANARY_SMTP_HOST is not set")
        if not self.sender:
            raise RuntimeError("KANARY_SMTP_SENDER is not set")
        if not self.recipients:
            raise RuntimeError("KANARY_SMTP_RECIPIENTS is not set")

    def emit(self, event: AlertEvent) -> None:
        message = EmailMessage()
        message["Subject"] = self._subject(event)
        message["From"] = self.sender or ""
        message["To"] = ", ".join(self.recipients)
        message.set_content(self._body(event))

        with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=self.smtp_timeout_seconds) as smtp:
            if self.use_starttls:
                smtp.starttls()
            if self.smtp_username:
                smtp.login(self.smtp_username, self.smtp_password or "")
            smtp.send_message(message)

    def _subject(self, event: AlertEvent) -> str:
        marker = event.transition.value if event.transition is not None else event.current_state.value
        return (
            f"{self.subject_prefix} "
            f"{marker} {severity_label(event.effective_severity)} {event.rule_id}"
        )

    def _body(self, event: AlertEvent) -> str:
        lines = [
            f"Rule: {event.rule_id}",
            f"Previous State: {event.previous_state.value if event.previous_state is not None else '-'}",
            f"State: {event.current_state.value}",
            f"Previous Severity: {severity_label(event.previous_severity) if event.previous_severity is not None else '-'}",
            f"Severity: {severity_label(event.current_severity)}",
            f"Message: {event.message or '-'}",
        ]
        if event.transition is not None:
            lines.append(f"Transition: {event.transition.value}")
        if event.tags:
            lines.append(f"Tags: {', '.join(event.tags)}")
        if event.owner:
            lines.append(f"Owner: {event.owner}")
        return "\n".join(lines)


def prepare_output_class(cls: type[Any]) -> type[Any]:
    _setdefault(cls, "description", None)
    _setdefault(cls, "include_tags", [])
    _setdefault(cls, "exclude_tags", [])
    _setdefault(cls, "exclude_states", list(_DEFAULT_EXCLUDED_STATES))
    _setdefault(cls, "exclude_transitions", [])
    _setdefault(cls, "minimum_severity", None)
    _setdefault(cls, "max_retry", 1)
    _setdefault(cls, "max_reinit", 1)
    if "init" not in cls.__dict__ and getattr(cls, "init", None) in {None, Output.init}:
        cls.init = Output.init
    if "terminate" not in cls.__dict__ and getattr(cls, "terminate", None) in {None, Output.terminate}:
        cls.terminate = Output.terminate
    if "matches" not in cls.__dict__ and getattr(cls, "matches", None) in {None, Output.matches}:
        cls.matches = Output.matches

    output_id = getattr(cls, "output_id", None)
    if not isinstance(output_id, str) or not output_id:
        raise ValueError(f"output '{cls.__name__}' must define non-empty string output_id")
    try:
        _coerce_minimum_severity(getattr(cls, "minimum_severity", None))
    except Exception as exc:
        raise ValueError(f"output '{output_id}' has invalid minimum_severity: {exc}") from exc
    for attr_name in ("max_retry", "max_reinit"):
        value = getattr(cls, attr_name, None)
        if not isinstance(value, int) or value < 0:
            raise ValueError(f"output '{output_id}' {attr_name} must be a non-negative integer")
    if not callable(getattr(cls, "emit", None)):
        raise ValueError(f"output '{output_id}' must implement emit(event)")
    try:
        cls.__kanary_init_style__ = detect_instance_method_style(
            getattr(cls, "init"),
            new_arity=0,
            legacy_arity=1,
            new_signature="init(self)",
            legacy_signature="init(self, ctx)",
        )
        cls.__kanary_emit_style__ = detect_instance_method_style(
            getattr(cls, "emit"),
            new_arity=1,
            legacy_arity=2,
            new_signature="emit(self, event)",
            legacy_signature="emit(self, event, ctx)",
        )
        cls.__kanary_terminate_style__ = detect_instance_method_style(
            getattr(cls, "terminate"),
            new_arity=0,
            legacy_arity=1,
            new_signature="terminate(self)",
            legacy_signature="terminate(self, ctx)",
        )
    except ValueError as exc:
        raise ValueError(f"output '{output_id}' {exc}") from exc
    return cls


def _setdefault(cls: type[Any], attr_name: str, value: Any) -> None:
    if hasattr(cls, attr_name):
        return
    setattr(cls, attr_name, value)


def _coerce_minimum_severity(value: str | Severity | None) -> Severity | None:
    if value is None:
        return None
    if isinstance(value, Severity):
        return value
    normalized = str(value).strip().upper()
    return Severity[normalized]
