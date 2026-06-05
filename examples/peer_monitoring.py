import json
import os
import time
from datetime import datetime
from urllib.request import urlopen

import kanary

# Peer monitoring example. It models the peer node's reported health as
# ordinary inputs, while transport failures to the peer API remain source
# plugin failures handled by Kanary's runtime recovery policy.


@kanary.source(source_id="kanary.peer", interval=30.0)
class KanaryPeerSource:

    def init(self, ctx):
        self.peer_url = os.environ.get("KANARY_PEER_URL", "http://127.0.0.1:8000/peer-status")
        self.timeout_seconds = float(os.environ.get("KANARY_PEER_TIMEOUT_SECONDS", "5.0"))

    def poll(self, ctx):
        started = time.monotonic()
        with urlopen(self.peer_url, timeout=self.timeout_seconds) as response:
            payload = json.loads(response.read().decode())

        generated_at = datetime.fromisoformat(payload["generated_at"])
        counts = payload.get("counts", {})
        alert_states = payload.get("alert_states", {})
        latency_ms = (time.monotonic() - started) * 1000.0

        return kanary.inputs(
            [
                ("heartbeat", 1, generated_at),
                ("latency_ms", latency_ms, generated_at),
                ("failed_plugins", counts.get("failed_plugins", 0), generated_at),
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
