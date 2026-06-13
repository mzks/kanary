from datetime import datetime, timezone
import json
import os
from pathlib import Path

import kanary


@kanary.source(source_id="local_load", interval=10 * kanary.second)
class LocalLoadSource:
    def poll(self):
        load1, _, _ = os.getloadavg()
        cpu_count = os.cpu_count() or 1
        return kanary.inputs([
            (
                "load1_per_cpu",
                load1 / cpu_count,
                datetime.now(timezone.utc),
                {"raw_load1": load1, "cpu_count": cpu_count},
            ),
        ])


@kanary.rule(
    rule_id="local_load.busy",
    inputs="local_load:load1_per_cpu",
    severity=kanary.WARN,
    tags=["getting-started", "demo"],
)
class LocalLoadBusy:
    description = "Alert when the 1-minute load average per CPU is high."
    runbook = "Run `uptime` or `top` on the monitored host."

    def evaluate(self, ctx):
        load = ctx.value()
        threshold = 0.70
        if load is None:
            return kanary.ok("load1_per_cpu is missing")
        return kanary.error_if(
            load > threshold,
            f"load1_per_cpu={load:.2f} is over {threshold:.2f}",
        ) or kanary.ok(f"load1_per_cpu={load:.2f} is within the normal range")


@kanary.rule(
    rule_id="local_load.busy_threshold",
    inputs="local_load:load1_per_cpu",
    severity=kanary.WARN,
    tags=["getting-started", "demo"],
)
class LocalLoadBusyThreshold(kanary.ThresholdRule):
    direction = "high"
    thresholds = [
        (0.70, kanary.WARN),
        (1.00, kanary.ERROR),
    ]


@kanary.output(output_id="file", include_tags=["getting-started"])
class FileOutput:
    output_path = Path("getting_started_alerts.jsonl")

    def init(self):
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self.output_path.touch(exist_ok=True)

    def emit(self, event):
        record = {
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
        }
        with self.output_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


@kanary.output(output_id="mail", include_tags=["getting-started", "sqlite"])
class MailAlert(kanary.MailOutput):
    smtp_host = "127.0.0.1"
    smtp_port = 1025
    use_starttls = False
    sender = "kanary@example.test"
    recipients = ["operator@example.test"]
    subject_prefix = "[KANARY getting-started]"

    def _subject(self, event):
        marker = event.transition.value if event.transition is not None else event.current_state.value
        return (
            f"{self.subject_prefix} "
            f"{marker} {kanary.severity_label(event.effective_severity)} {event.rule_id}"
        )

    def _body(self, event):
        lines = [
            f"Rule: {event.rule_id}",
            f"Occurred At: {event.occurred_at.isoformat()}",
            f"Previous State: {event.previous_state.value if event.previous_state is not None else '-'}",
            f"State: {event.current_state.value}",
            (
                "Previous Severity: "
                f"{kanary.severity_label(event.previous_severity) if event.previous_severity is not None else '-'}"
            ),
            f"Severity: {kanary.severity_label(event.current_severity)}",
            f"Transition: {event.transition.value if event.transition else '-'}",
            f"Owner: {event.owner or '-'}",
            f"Tags: {', '.join(event.tags) if event.tags else '-'}",
            f"Message: {event.message or '-'}",
        ]
        if event.payload:
            lines.extend(
                [
                    "",
                    "Payload:",
                    json.dumps(event.payload, ensure_ascii=False, indent=2, sort_keys=True),
                ]
            )
        return "\n".join(lines)
