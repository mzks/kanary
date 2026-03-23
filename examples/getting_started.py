from datetime import datetime, timezone
import json
import os
from pathlib import Path

import kanary


@kanary.source(source_id="local_load", interval=10 * kanary.second)
class LocalLoadSource:
    def poll(self, ctx):
        load1, _, _ = os.getloadavg()
        cpu_count = os.cpu_count() or 1
        return kanary.SourceResult(
            measurements=[
                kanary.Measurement(
                    name="load1_per_cpu",
                    value=load1 / cpu_count,
                    timestamp=datetime.now(timezone.utc),
                    metadata={"raw_load1": load1, "cpu_count": cpu_count},
                ),
            ],
            status="ok",
        )


@kanary.rule(
    rule_id="local_load.busy",
    source="local_load",
    severity=kanary.WARN,
    tags=["getting-started", "demo"],
    owner="demo_owner",
)
class LocalLoadBusy:
    description = "Alert when the 1-minute load average per CPU is high."
    runbook = "Run `uptime` or `top` on the monitored host."

    def evaluate(self, payload, ctx):
        load = ctx.value("load1_per_cpu")
        threshold = 0.70
        if load is None:
            return kanary.Evaluation(
                state=kanary.AlertState.OK,
                payload=payload,
                message="load1_per_cpu is missing",
            )
        if load > threshold:
            return kanary.Evaluation(
                state=kanary.AlertState.FIRING,
                payload=payload,
                message=f"load1_per_cpu={load:.2f} is over {threshold:.2f}",
            )
        return kanary.Evaluation(
            state=kanary.AlertState.OK,
            payload=payload,
            message=f"load1_per_cpu={load:.2f} is within the normal range",
        )


@kanary.rule(
    rule_id="local_load.busy_threshold",
    source="local_load",
    severity=kanary.WARN,
    tags=["getting-started", "demo"],
    owner="demo_owner",
)
class LocalLoadBusyThreshold(kanary.ThresholdRule):
    measurement = "load1_per_cpu"
    direction = "high"
    thresholds = [
        (0.70, kanary.WARN),
        (1.00, kanary.ERROR),
    ]


@kanary.output(output_id="file", include_tags=["getting-started"])
class FileOutput:
    output_path = Path("getting_started_alerts.jsonl")

    def init(self, ctx):
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self.output_path.touch(exist_ok=True)

    def emit(self, event, ctx):
        record = {
            "rule_id": event.rule_id,
            "previous_state": event.previous_state.value if event.previous_state else None,
            "current_state": event.current_state.value,
            "severity": kanary.severity_label(int(event.alert.severity)),
            "message": event.alert.message,
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
