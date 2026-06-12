import json
from pathlib import Path
import tomllib
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import kanary

CONFIG_PATH = Path(__file__).with_name("discord_webhook_output_config.toml")


def load_config() -> dict:
    with CONFIG_PATH.open("rb") as handle:
        return tomllib.load(handle)


@kanary.output(
    output_id="discord",
    include_tags=["sqlite"],
    exclude_states=["SUPPRESSED"],
    minimum_severity="ERROR",
)
class DiscordOutput:

    def init(self, ctx):
        config = load_config()
        self.webhook_url = str(config.get("webhook_url") or "").strip()
        if not self.webhook_url:
            raise RuntimeError(f"{CONFIG_PATH.name} must define webhook_url")

    def emit(self, event, ctx):
        color = alert_color(event.current_state.value, int(event.effective_severity))
        transition = event.transition.value if event.transition else None
        title = event.rule_id if transition is None else f"{event.rule_id}: {transition}"
        severity = (
            kanary.severity_label(event.current_severity)
            if event.previous_severity is None or event.previous_severity == event.current_severity
            else f"{kanary.severity_label(event.previous_severity)} -> "
            f"{kanary.severity_label(event.current_severity)}"
        )
        payload = {
            "content": f"{title}: {event.current_state.value}",
            "embeds": [
                {
                    "title": f"{title}: {event.current_state.value}",
                    "description": event.alert.message or "",
                    "color": color,
                    "fields": [
                        {
                            "name": "Transition",
                            "value": transition or "-",
                            "inline": True,
                        },
                        {
                            "name": "Severity",
                            "value": severity,
                            "inline": True,
                        },
                        {
                            "name": "Tags",
                            "value": ", ".join(event.alert.tags) or "-",
                            "inline": True,
                        },
                    ],
                }
            ]
        }

        request = Request(
            self.webhook_url,
            data=json.dumps(payload).encode(),
            headers={
                "Content-Type": "application/json",
                "User-Agent": "kanary-discord-output/0.1",
            },
            method="POST",
        )
        try:
            with urlopen(request):
                return None
        except HTTPError as exc:
            body = exc.read().decode(errors="replace")
            raise RuntimeError(f"discord webhook returned {exc.code}: {body}") from exc


def alert_color(state: str, severity: int) -> int:
    if state == kanary.OK:
        return 0x2ECC71
    return {
        10: 0x3498DB,
        20: 0xF1C40F,
        30: 0xE74C3C,
        40: 0x8E44AD,
    }.get(severity, 0x95A5A6)
