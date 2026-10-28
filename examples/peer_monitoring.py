import json
import time
from datetime import datetime
from urllib.request import urlopen

import kanary

# Peer monitoring example. It models the peer node's reported health as
# ordinary inputs, while transport failures to the peer API remain source
# plugin failures handled by Kanary's runtime recovery policy.

@kanary.source(source_id="kanary.peer", interval=30.0)
class KanaryPeerSource:
    description = "Poll a peer Kanary node and expose its reported health as ordinary inputs."

    def init(self):
        config = kanary.load_toml(filename="peer_monitoring_config.toml")
        self.peer_url = str(config.get("peer_url", "http://127.0.0.1:8000/peer-status"))
        self.timeout_seconds = float(config.get("timeout_seconds", 5.0))

    def poll(self):
        started = time.monotonic()
        with urlopen(self.peer_url, timeout=self.timeout_seconds) as response:
            payload = json.loads(response.read().decode())

        if not isinstance(payload, dict):
            raise ValueError("peer-status response must be an object")
        for field in ("generated_at", "counts", "alert_states"):
            if field not in payload:
                raise ValueError(f"peer-status response is missing required field '{field}'")
        if not isinstance(payload["counts"], dict) or not isinstance(payload["alert_states"], dict):
            raise ValueError("peer-status counts and alert_states must be objects")
        if "failed_plugins" not in payload["counts"]:
            raise ValueError("peer-status counts is missing required field 'failed_plugins'")

        generated_at = datetime.fromisoformat(str(payload["generated_at"]).replace("Z", "+00:00"))
        counts = payload["counts"]
        alert_states = payload["alert_states"]
        if generated_at.tzinfo is None:
            raise ValueError("peer-status generated_at must include a timezone")
        if isinstance(counts["failed_plugins"], bool) or not isinstance(counts["failed_plugins"], (int, float)):
            raise ValueError("peer-status failed_plugins must be numeric")
        latency_ms = (time.monotonic() - started) * 1000.0

        return kanary.inputs(
            [
                ("heartbeat", 1, generated_at),
                ("latency_ms", latency_ms, generated_at),
                ("failed_plugins", counts["failed_plugins"], generated_at),
                ("firing_alerts", alert_states.get("FIRING", 0), generated_at),
            ],
            metadata={"peer_url": self.peer_url},
        )

@kanary.rule(
    rule_id="kanary.peer.heartbeat.stale",
    inputs="kanary.peer:heartbeat",
    severity=kanary.ERROR,
    tags=["kanary", "peer", "heartbeat"],
    owner="expert_kanary",
    evaluation_interval=30.0,
)
class KanaryPeerHeartbeatStale(kanary.StaleRule):
    timeout = 2 * kanary.minute


@kanary.rule(
    rule_id="kanary.peer.failed_plugins",
    inputs="kanary.peer:failed_plugins",
    severity=kanary.ERROR,
    tags=["kanary", "peer", "plugins"],
    owner="expert_kanary",
)
class KanaryPeerFailedPlugins(kanary.ThresholdRule):
    direction = "high"
    thresholds = [(1.0, kanary.ERROR)]
