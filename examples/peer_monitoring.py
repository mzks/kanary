import json
import os
import time
from datetime import datetime
from urllib.request import urlopen

import kanary


@kanary.source(source_id="kanary.peer", interval=30.0)
class KanaryPeerSource:

    def init(self, ctx):
        self.peer_url = os.environ.get("KANARY_PEER_URL", "http://127.0.0.1:8000/peer-status")
        self.timeout_seconds = float(os.environ.get("KANARY_PEER_TIMEOUT_SECONDS", "5.0"))

    def poll(self, ctx):
        started = time.monotonic()
        try:
            with urlopen(self.peer_url, timeout=self.timeout_seconds) as response:
                payload = json.loads(response.read().decode())
        except Exception as exc:
            return kanary.SourceResult(status="error", error=str(exc))

        generated_at = datetime.fromisoformat(payload["generated_at"])
        counts = payload.get("counts", {})
        alert_states = payload.get("alert_states", {})
        latency_ms = (time.monotonic() - started) * 1000.0

        measurements = [
            kanary.Measurement(name="heartbeat", value=1, timestamp=generated_at),
            kanary.Measurement(
                name="latency_ms",
                value=latency_ms,
                timestamp=generated_at,
            ),
            kanary.Measurement(
                name="failed_plugins",
                value=counts.get("failed_plugins", 0),
                timestamp=generated_at,
            ),
            kanary.Measurement(
                name="firing_alerts",
                value=alert_states.get("FIRING", 0),
                timestamp=generated_at,
            ),
        ]
        return kanary.SourceResult(
            measurements=measurements,
            status="ok",
            metadata={"peer_url": self.peer_url},
        )


@kanary.rule(
    rule_id="kanary.peer.connection.failed",
    source="kanary.peer",
    severity=kanary.ERROR,
    tags=["kanary", "peer", "infra"],
    owner="expert_kanary",
)
class KanaryPeerConnectionFailed:

    def evaluate(self, payload, ctx):
        if payload.get("status") == "ok":
            return kanary.Evaluation(
                state=kanary.AlertState.OK,
                payload=payload,
                message=f"peer API ok: {payload.get('metadata', {}).get('peer_url', 'unknown')}",
            )
        return kanary.Evaluation(
            state=kanary.AlertState.FIRING,
            payload=payload,
            message=payload.get("error") or f"peer source status={payload.get('status')}",
        )


@kanary.rule(
    rule_id="kanary.peer.heartbeat.stale",
    source="kanary.peer",
    severity=kanary.ERROR,
    tags=["kanary", "peer", "heartbeat"],
    owner="expert_kanary",
)
class KanaryPeerHeartbeatStale(kanary.StaleRule):
    measurement = "heartbeat"
    timeout = 2 * kanary.minute
    suppressed_by = ["kanary.peer.connection.failed"]


@kanary.rule(
    rule_id="kanary.peer.failed_plugins",
    source="kanary.peer",
    severity=kanary.ERROR,
    tags=["kanary", "peer", "plugins"],
    owner="expert_kanary",
)
class KanaryPeerFailedPlugins(kanary.ThresholdRule):
    measurement = "failed_plugins"
    direction = "high"
    thresholds = [(1.0, kanary.ERROR)]
    suppressed_by = ["kanary.peer.connection.failed"]
