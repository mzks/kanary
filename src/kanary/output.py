from email.message import EmailMessage
import os
import smtplib
from typing import Any

from .models import AlertEvent
from .constants import severity_label


class Output:
    output_id: str
    include_tags: list[str] = []
    exclude_tags: list[str] = []
    include_states: list[str] = []
    exclude_states: list[str] = []

    def init(self, ctx: dict[str, Any]) -> None:
        return None

    def emit(self, event: AlertEvent, ctx: dict[str, Any]) -> None:
        raise NotImplementedError

    def terminate(self, ctx: dict[str, Any]) -> None:
        return None

    def matches(self, event: AlertEvent) -> bool:
        alert_tags = set(event.alert.tags)
        if self.include_tags and not alert_tags.intersection(self.include_tags):
            return False
        if self.exclude_tags and alert_tags.intersection(self.exclude_tags):
            return False
        state = event.current_state.value
        if self.include_states and state not in self.include_states:
            return False
        if self.exclude_states and state in self.exclude_states:
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

    def init(self, ctx: dict[str, Any]) -> None:
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

    def emit(self, event: AlertEvent, ctx: dict[str, Any]) -> None:
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
        return (
            f"{self.subject_prefix} "
            f"{event.current_state.value} {severity_label(event.alert.severity)} {event.rule_id}"
        )

    def _body(self, event: AlertEvent) -> str:
        lines = [
            f"Rule: {event.rule_id}",
            f"State: {event.current_state.value}",
            f"Severity: {severity_label(event.alert.severity)}",
            f"Message: {event.alert.message or '-'}",
        ]
        if event.alert.tags:
            lines.append(f"Tags: {', '.join(event.alert.tags)}")
        if event.alert.owner:
            lines.append(f"Owner: {event.alert.owner}")
        return "\n".join(lines)


def prepare_output_class(cls: type[Any]) -> type[Any]:
    _setdefault(cls, "include_tags", [])
    _setdefault(cls, "exclude_tags", [])
    _setdefault(cls, "include_states", [])
    _setdefault(cls, "exclude_states", [])
    if "init" not in cls.__dict__ and getattr(cls, "init", None) in {None, Output.init}:
        cls.init = Output.init
    if "terminate" not in cls.__dict__ and getattr(cls, "terminate", None) in {None, Output.terminate}:
        cls.terminate = Output.terminate
    if "matches" not in cls.__dict__ and getattr(cls, "matches", None) in {None, Output.matches}:
        cls.matches = Output.matches

    output_id = getattr(cls, "output_id", None)
    if not isinstance(output_id, str) or not output_id:
        raise ValueError(f"output '{cls.__name__}' must define non-empty string output_id")
    if not callable(getattr(cls, "emit", None)):
        raise ValueError(f"output '{output_id}' must implement emit(event, ctx)")
    return cls


def _setdefault(cls: type[Any], attr_name: str, value: Any) -> None:
    if hasattr(cls, attr_name):
        return
    setattr(cls, attr_name, value)
