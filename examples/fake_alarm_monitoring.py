from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

import kanary


def parse_iso_timestamp(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)


@kanary.source(source_id="fake_alarm", interval=5 * kanary.second)
class FakeAlarmSource:
    status_url = os.environ.get("KANARY_FAKE_ALARM_URL", "http://127.0.0.1:18081/status")
    timeout_seconds = 3.0

    def poll(self, ctx):
        try:
            with urlopen(self.status_url, timeout=self.timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (URLError, HTTPError, json.JSONDecodeError) as exc:
            return kanary.SourceResult(status="error", error=str(exc))

        active = bool(payload.get("active", False))
        severity = str(payload.get("severity") or "WARN").upper()
        message = str(payload.get("message") or "Manual fake alarm target is idle")
        updated_at = parse_iso_timestamp(payload.get("updated_at"))

        return kanary.SourceResult(
            measurements=[
                kanary.Measurement(
                    name="manual_alarm",
                    value=1 if active else 0,
                    timestamp=updated_at,
                    metadata={
                        "message": message,
                        "severity": severity,
                        "status_url": self.status_url,
                    },
                ),
            ],
            status="ok",
            metadata={"status_url": self.status_url},
        )


@kanary.rule(
    rule_id="fake_alarm.manual",
    source="fake_alarm",
    severity=kanary.WARN,
    tags=["fake-alarm", "demo"],
    owner="demo_owner",
)
class FakeAlarmRule:
    description = "Manual fake alarm that can be triggered and cleared through a small HTTP target."
    runbook = "Use curl against the fake alarm target to trigger or clear the alarm."

    def evaluate(self, payload, ctx):
        value = ctx.value("manual_alarm")
        metadata = ctx.metadata("manual_alarm")
        if value is None:
            return kanary.Evaluation(
                state=kanary.AlertState.OK,
                payload=payload,
                message="manual_alarm is missing",
            )

        active = bool(value)
        severity_name = str(metadata.get("severity") or "WARN").upper()
        severity = {
            "INFO": kanary.INFO,
            "WARN": kanary.WARN,
            "ERROR": kanary.ERROR,
            "CRITICAL": kanary.CRITICAL,
        }.get(severity_name, kanary.WARN)
        message = str(metadata.get("message") or "Fake alarm target updated")

        if active:
            return kanary.Evaluation(
                state=kanary.AlertState.FIRING,
                payload=payload,
                message=message,
                severity=severity,
            )
        return kanary.Evaluation(
            state=kanary.AlertState.OK,
            payload=payload,
            message=message,
        )


@kanary.output(output_id="fake_alarm_console", include_tags=["fake-alarm"])
class FakeAlarmConsoleOutput:
    def emit(self, event, ctx):
        print(
            json.dumps(
                {
                    "rule_id": event.rule_id,
                    "previous_state": event.previous_state.value if event.previous_state else None,
                    "current_state": event.current_state.value,
                    "severity": kanary.severity_label(int(event.alert.severity)),
                    "message": event.alert.message,
                    "occurred_at": event.occurred_at.isoformat(),
                },
                ensure_ascii=False,
            )
        )
