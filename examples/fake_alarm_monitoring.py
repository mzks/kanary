from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import tomllib
from urllib.request import urlopen

import kanary

# Small demo source that reads a fake alarm target over HTTP. HTTP and JSON
# failures are treated as source plugin failures so the runtime recovery logic
# can retry/reinit them instead of hiding them as ordinary alert payloads.

CONFIG_PATH = Path(__file__).with_name("fake_alarm_monitoring_config.toml")


def load_config() -> dict:
    with CONFIG_PATH.open("rb") as handle:
        return tomllib.load(handle)


def parse_iso_timestamp(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)


@kanary.source(source_id="fake_alarm", interval=5 * kanary.second)
class FakeAlarmSource:
    def init(self):
        config = load_config()
        self.status_url = str(config.get("status_url", "http://127.0.0.1:18081/status"))
        self.timeout_seconds = float(config.get("timeout_seconds", 3.0))

    def poll(self):
        with urlopen(self.status_url, timeout=self.timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))

        active = bool(payload.get("active", False))
        severity = str(payload.get("severity") or "WARN").upper()
        message = str(payload.get("message") or "Manual fake alarm target is idle")
        updated_at = parse_iso_timestamp(payload.get("updated_at"))

        return kanary.inputs(
            [
                (
                    "manual_alarm",
                    1 if active else 0,
                    updated_at,
                    {
                        "message": message,
                        "severity": severity,
                        "status_url": self.status_url,
                    },
                ),
            ],
            metadata={"status_url": self.status_url},
        )


@kanary.rule(
    rule_id="fake_alarm.manual",
    inputs="fake_alarm:manual_alarm",
    severity=kanary.WARN,
    tags=["fake-alarm", "demo"],
    owner="demo_owner",
)
class FakeAlarmRule:
    description = "Manual fake alarm that can be triggered and cleared through a small HTTP target."
    runbook = "Use curl against the fake alarm target to trigger or clear the alarm."

    def evaluate(self, ctx):
        value = ctx.value()
        metadata = ctx.metadata(default={}) or {}
        if value is None:
            return kanary.ok("manual_alarm is missing")

        active = bool(value)
        severity_name = str(metadata.get("severity") or "WARN").upper()
        message = str(metadata.get("message") or "Fake alarm target updated")
        return kanary.fire_if(active, message, severity=severity_name) or kanary.ok(message)


@kanary.output(output_id="fake_alarm_console", include_tags=["fake-alarm"])
class FakeAlarmConsoleOutput:
    def emit(self, event):
        print(
            json.dumps(
                {
                    "rule_id": event.rule_id,
                    "previous_state": event.previous_state.value if event.previous_state else None,
                    "current_state": event.current_state.value,
                    "previous_severity": (
                        kanary.severity_label(event.previous_severity)
                        if event.previous_severity is not None else None
                    ),
                    "current_severity": kanary.severity_label(event.current_severity),
                    "transition": event.transition.value if event.transition else None,
                    "owner": event.owner,
                    "tags": list(event.tags),
                    "message": event.message,
                    "occurred_at": event.occurred_at.isoformat(),
                },
                ensure_ascii=False,
            )
        )
