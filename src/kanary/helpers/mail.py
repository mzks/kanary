"""MailOutput: alert events by e-mail over SMTP."""

from email.message import EmailMessage
import html
import smtplib

from ..models import AlertEvent
from ..output import Output
from .notice import describe, digest


class MailOutput(Output):
    """Send each event as an e-mail over SMTP.

    The mail has a plain-text part and an HTML part with a colored header (green
    OK, grey silenced, else by severity), the rule's message, and a table of
    state, severity, since, owner, tags, ack, and the judged values. Override
    subject(), body() (plain text), or html() (return None for text only). Set
    viewer_url to link the mail to the KANARY viewer.
    """

    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_timeout_seconds: float = 10.0
    use_starttls: bool = True
    sender: str | None = None
    recipients: list[str] = []
    subject_prefix: str = "[KANARY]"
    viewer_url: str | None = None

    def recipients_for(self, event: AlertEvent) -> list[str]:
        """Who receives this event; override to use an OnCallSchedule."""
        return list(self.recipients)

    def init(self) -> None:
        # smtp_password is a secret: smtp_password = os.environ.get("SMTP_PASSWORD").
        name = type(self).__name__
        if not self.smtp_host:
            raise ValueError(f"{name}.smtp_host is not set")
        if not self.sender:
            raise ValueError(f"{name}.sender is not set")
        if not self.recipients and type(self).recipients_for is MailOutput.recipients_for:
            raise ValueError(f"{name}.recipients is empty; set it or override recipients_for()")

    def emit(self, event: AlertEvent) -> None:
        recipients = self.recipients_for(event)
        if not recipients:
            raise RuntimeError(f"no recipients for {event.rule_id}")
        message = self.message(event)
        message["To"] = ", ".join(recipients)
        self.send_mail(message)

    def send_mail(self, message: EmailMessage) -> None:
        """Send a finished mail over SMTP."""
        with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=self.smtp_timeout_seconds) as smtp:
            if self.use_starttls:
                smtp.starttls()
            if self.smtp_username:
                smtp.login(self.smtp_username, self.smtp_password or "")
            smtp.send_message(message)

    def emit_summary(self, events: list[AlertEvent]) -> None:
        """Send several events as one mail (QuietHours with release="summary")."""
        recipients = sorted({address for event in events for address in self.recipients_for(event)})
        if not recipients:
            raise RuntimeError("no recipients for the summary")
        summary = digest(events, viewer_url=self.viewer_url)
        message = EmailMessage()
        message["Subject"] = f"{self.subject_prefix} {summary.title}".strip()
        message["From"] = self.sender or ""
        message["To"] = ", ".join(recipients)
        lines = summary.lines()
        message.set_content("\n".join([summary.text, "", *lines]))
        escape = html.escape
        items = "".join(f'<li style="margin:4px 0">{escape(line)}</li>' for line in lines)
        message.add_alternative(
            '<!doctype html><html><head><meta charset="utf-8"></head><body style="margin:0;padding:16px">'
            '<div style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;max-width:640px;color:#222">'
            f'<div style="border-left:6px solid {summary.color_hex};background:#f6f7f8;padding:12px 16px;border-radius:4px">'
            f'<div style="font-size:18px;font-weight:700">{escape(summary.title)}</div>'
            f'<div style="margin-top:6px">{escape(summary.text)}</div></div>'
            f'<ul style="padding-left:20px;font-size:14px">{items}</ul></div></body></html>',
            subtype="html",
        )
        self.send_mail(message)

    def message(self, event: AlertEvent) -> EmailMessage:
        """The mail without recipients: subject(), body(), and html() when it returns one."""
        message = EmailMessage()
        message["Subject"] = self.subject(event)
        message["From"] = self.sender or ""
        message.set_content(self.body(event))
        rich = self.html(event)
        if rich is not None:
            message.add_alternative(rich, subtype="html")
        return message

    def subject(self, event: AlertEvent) -> str:
        """"[KANARY] FIRING ERROR oil.temperature"."""
        return f"{self.subject_prefix} {describe(event).title}".strip()

    def body(self, event: AlertEvent) -> str:
        """The plain-text part (and what mail clients without HTML show)."""
        notice = describe(event, viewer_url=self.viewer_url)
        lines = [notice.title, ""]
        if notice.message:
            lines += [notice.message, ""]
        width = max((len(label) for label, _ in notice.fields), default=0)
        for label, value in notice.fields:
            first, *rest = value.split("\n")
            lines.append(f"{label + ':':<{width + 1}} {first}")
            lines += [" " * (width + 2) + line for line in rest]
        lines += ["", f"KANARY · {notice.time}"]
        if notice.link:
            lines.append(notice.link)
        return "\n".join(lines)

    def html(self, event: AlertEvent) -> str | None:
        """The HTML part; return None to send plain text only."""
        notice = describe(event, viewer_url=self.viewer_url)
        escape = html.escape
        title = escape(notice.headline)
        if notice.link:
            title = f'<a href="{escape(notice.link)}" style="color:inherit;text-decoration:none">{title}</a>'
        rows = "".join(
            f'<tr><th style="text-align:left;vertical-align:top;padding:4px 16px 4px 0;color:#666;font-weight:600">{escape(label)}</th>'
            f'<td style="padding:4px 0;white-space:pre-wrap">{escape(value)}</td></tr>'
            for label, value in notice.fields
        )
        message = f'<div style="margin-top:6px;font-size:15px">{escape(notice.message)}</div>' if notice.message else ""
        link = f' · <a href="{escape(notice.link)}">Open KANARY</a>' if notice.link else ""
        return (
            '<!doctype html><html><head><meta charset="utf-8"></head>'
            '<body style="margin:0;padding:16px;background:#ffffff">'
            '<div style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;max-width:640px;color:#222">'
            f'<div style="border-left:6px solid {notice.color_hex};background:#f6f7f8;padding:12px 16px;border-radius:4px">'
            f'<div style="font-size:18px;font-weight:700">{title}</div>{message}</div>'
            f'<table style="border-collapse:collapse;margin-top:12px;font-size:14px">{rows}</table>'
            f'<div style="margin-top:16px;color:#888;font-size:12px">KANARY · {escape(notice.time)}{link}</div>'
            "</div></body></html>"
        )
