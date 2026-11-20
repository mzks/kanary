"""HTTP helpers: HTTPJSONSource, WebhookOutput, and chat outputs built on it."""

import json
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from ..models import AlertEvent, SourceResult
from ..output import Output, event_record
from ..source import Source, inputs, no_data
from ..values import flatten, get_path, to_number
from .notice import cut, describe, digest, summary

_USER_AGENT = "kanary"


class HTTPJSONSource(Source):
    """GET a JSON document and turn selected values into numeric inputs.

    fields maps input names to dotted paths ("data.sensors.0.value"); when
    empty, every numeric leaf becomes an input under its dotted name.
    Missing paths and non-numeric values are listed in the result metadata.
    Keep tokens out of the plugin file, for example in an environment variable:

        headers = {"Authorization": f"Bearer {os.environ.get('API_TOKEN', '')}"}
    """

    url: str | None = None
    headers: dict[str, str] = {}
    timeout: float = 5.0
    fields: dict[str, str] = {}

    def fetch_json(self) -> Any:
        if not self.url:
            raise ValueError("HTTPJSONSource.url is not set")
        request = Request(
            str(self.url),
            headers={"Accept": "application/json", "User-Agent": _USER_AGENT, **self.headers},
        )
        with urlopen(request, timeout=self.timeout) as response:
            return json.load(response)

    def select(self, document: Any) -> dict[str, Any]:
        """Return {input name: raw value}; override for custom documents."""
        if not self.fields:
            return flatten(document)
        return {name: get_path(document, path) for name, path in self.fields.items()}

    def poll(self) -> SourceResult:
        selected = self.select(self.fetch_json())
        items, missing, texts = [], [], {}
        for name, value in selected.items():
            if value is None:
                missing.append(name)
                continue
            number = to_number(value)
            if number is None:
                texts[name] = value if isinstance(value, str) else json.dumps(value)
                continue
            items.append((name, number))
        metadata: dict[str, Any] = {"url": urlsplit(str(self.url))._replace(query="", fragment="").geturl()}
        if missing:
            metadata["missing"] = missing
        if texts:
            metadata["text"] = texts
        if not items:
            return no_data(reason="no numeric values in the response", metadata=metadata)
        return inputs(items, metadata=metadata)


class WebhookOutput(Output):
    """POST ``payload(event)`` as JSON to ``url``.

    A webhook URL is usually a secret: ``url = os.environ.get("WEBHOOK_URL")``,
    or kanary.load_json(..., filename="secrets.json") next to the plugin.
    Override payload() for the receiver's format; text() is a one-line summary
    that the chat outputs use.
    """

    url: str | None = None
    headers: dict[str, str] = {}
    timeout: float = 5.0

    def init(self) -> None:
        if not self.url:
            raise ValueError(f"{type(self).__name__}.url is not set")

    def payload(self, event: AlertEvent) -> dict[str, Any]:
        return event_record(event)

    def text(self, event: AlertEvent) -> str:
        """One line: "[ERROR] FIRING oil.temperature: value=95 out of range [10, 80]"."""
        return summary(event)

    def emit(self, event: AlertEvent) -> None:
        self.post_json(self.payload(event))

    def emit_summary(self, events: list[AlertEvent]) -> None:
        """Send several events as one message (QuietHours with release="summary")."""
        self.post_json(self.summary_payload(events))

    def summary_payload(self, events: list[AlertEvent]) -> dict[str, Any]:
        return {"events": [self.payload(event) for event in events]}

    def post_json(self, body: dict[str, Any]) -> None:
        """POST body as JSON to url; raises on an HTTP error."""
        request = Request(
            str(self.url),
            data=json.dumps(body, ensure_ascii=False, default=str).encode(),
            headers={"Content-Type": "application/json", "User-Agent": _USER_AGENT, **self.headers},
            method="POST",
        )
        # A finite timeout is essential: this output's queue waits while it hangs.
        try:
            with urlopen(request, timeout=self.timeout) as response:
                response.read()
        except HTTPError as exc:
            body = exc.read().decode(errors="replace")[:300]
            raise RuntimeError(f"webhook returned HTTP {exc.code}: {body}") from exc


class SlackOutput(WebhookOutput):
    """Slack incoming webhook (https://api.slack.com/messaging/webhooks).

    Each event is a message with a colored bar (green OK, grey silenced, else by
    severity), the rule's message, and fields: state, severity, since, owner,
    tags, ack, and the judged values. The one-line text() is what notifications
    and previews show. Set viewer_url to link the title to the KANARY viewer.
    """

    viewer_url: str | None = None

    def payload(self, event: AlertEvent) -> dict[str, Any]:
        notice = describe(event, viewer_url=self.viewer_url)
        title = _slack_escape(notice.headline)
        title = f"*<{notice.link}|{title}>*" if notice.link else f"*{title}*"
        if notice.message:
            title += "\n" + _slack_escape(notice.message)
        blocks: list[dict[str, Any]] = [{"type": "section", "text": {"type": "mrkdwn", "text": cut(title, 3000)}}]
        if notice.fields:
            blocks.append({
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": cut(f"*{label}*\n{_slack_escape(value)}", 2000)}
                    for label, value in notice.fields[:10]
                ],
            })
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": f"KANARY · {notice.time}"}]})
        return {"text": notice.text, "attachments": [{"color": notice.color_hex, "blocks": blocks}]}

    def summary_payload(self, events: list[AlertEvent]) -> dict[str, Any]:
        summary = digest(events, viewer_url=self.viewer_url)
        text = f"*{_slack_escape(summary.title)}*\n" + "\n".join(_slack_escape(line) for line in summary.lines())
        return {"text": summary.text, "attachments": [{"color": summary.color_hex, "blocks": [
            {"type": "section", "text": {"type": "mrkdwn", "text": cut(text, 3000)}},
        ]}]}


class MattermostOutput(WebhookOutput):
    """Mattermost incoming webhook, as a message attachment with a colored bar.

    Shows the same as SlackOutput. ``username`` and ``icon_url`` change the sender
    when the server allows overriding them; set viewer_url to link the title.
    """

    username: str | None = None
    icon_url: str | None = None
    viewer_url: str | None = None

    def payload(self, event: AlertEvent) -> dict[str, Any]:
        notice = describe(event, viewer_url=self.viewer_url)
        attachment: dict[str, Any] = {
            "fallback": notice.text,
            "color": notice.color_hex,
            "title": notice.headline,
            "text": notice.message,
            "fields": [{"short": "\n" not in value and len(value) <= 40, "title": label, "value": value} for label, value in notice.fields],
            "footer": f"KANARY · {notice.time}",
        }
        if notice.link:
            attachment["title_link"] = notice.link
        return self._sender({"attachments": [attachment]})

    def summary_payload(self, events: list[AlertEvent]) -> dict[str, Any]:
        summary = digest(events, viewer_url=self.viewer_url)
        attachment = {"fallback": summary.text, "color": summary.color_hex, "title": summary.title,
                      "text": "\n".join(summary.lines())}
        return self._sender({"attachments": [attachment]})

    def _sender(self, body: dict[str, Any]) -> dict[str, Any]:
        if self.username:
            body["username"] = self.username
        if self.icon_url:
            body["icon_url"] = self.icon_url
        return body


class DiscordOutput(WebhookOutput):
    """Discord channel webhook, as an embed with a colored bar.

    Shows the same as SlackOutput; Discord shows the time in each reader's own
    time zone. ``username`` changes the sender; set viewer_url to link the title.
    """

    username: str | None = None
    viewer_url: str | None = None

    def payload(self, event: AlertEvent) -> dict[str, Any]:
        notice = describe(event, viewer_url=self.viewer_url)
        embed: dict[str, Any] = {
            "title": cut(notice.headline, 256),
            "description": cut(notice.message, 4096),
            "color": notice.color,
            "fields": [
                {"name": cut(label, 256), "value": cut(value, 1024), "inline": "\n" not in value and len(value) <= 40}
                for label, value in notice.fields[:25]
            ],
            "footer": {"text": "KANARY"},
            "timestamp": event.occurred_at.isoformat(),
        }
        if notice.link:
            embed["url"] = notice.link
        return self._sender({"embeds": [embed]})

    def summary_payload(self, events: list[AlertEvent]) -> dict[str, Any]:
        summary = digest(events, viewer_url=self.viewer_url)
        embed = {"title": cut(summary.title, 256), "description": cut("\n".join(summary.lines()), 4096),
                 "color": summary.color, "footer": {"text": "KANARY"}}
        return self._sender({"embeds": [embed]})

    def _sender(self, body: dict[str, Any]) -> dict[str, Any]:
        if self.username:
            body["username"] = self.username
        return body


def _slack_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

