import json
import os
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import kanary


@kanary.output(
    output_id="discord",
    include_tags=["sqlite"],
    exclude_states=["SUPPRESSED"],
)
class DiscordOutput:

    def init(self, ctx):
        self.webhook_url = os.environ.get("KANARY_DISCORD_WEBHOOK_URL")
        if not self.webhook_url:
            raise RuntimeError("KANARY_DISCORD_WEBHOOK_URL is not set")

    def emit(self, event, ctx):
        color = alert_color(event.current_state.value, int(event.alert.severity))
        payload = {
            "content": f"{event.rule_id}: {event.current_state.value}",
            "embeds": [
                {
                    "title": f"{event.rule_id}: {event.current_state.value}",
                    "description": event.alert.message or "",
                    "color": color,
                    "fields": [
                        {
                            "name": "Severity",
                            "value": kanary.severity_label(int(event.alert.severity)),
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
