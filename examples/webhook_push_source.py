"""Evaluate webhook values as soon as they arrive.

HTTPPushSource listens for HTTP POSTs and the rules run right after each one.
It uses the standard library's ``http.server``: fine on a trusted network; to
accept from the internet, put a reverse proxy with TLS in front of it.

Run Kanary:
    kanary examples/webhook_push_source.py --api-host 127.0.0.1 --api-port 8910

Send values (a JSON object of input name to value):
    curl -X POST http://127.0.0.1:8911/inputs \\
      -d '{"temperature": 92.1, "fan_rpm": 1200}'

A value may carry its own time; without one it gets the time it arrived:
    curl -X POST http://127.0.0.1:8911/inputs \\
      -d '{"temperature": {"value": 92.1, "timestamp": "2026-07-20T10:00:00+09:00"}}'
"""

import kanary


@kanary.source(source_id="demo.webhook")
class WebhookInputs(kanary.HTTPPushSource):
    """Values posted to http://127.0.0.1:8911/inputs."""

    port = 8911
    path = "/inputs"


@kanary.rule(
    rule_id="demo.webhook.temperature",
    inputs="demo.webhook:temperature",
    severity=kanary.WARN,
    tags=["demo", "webhook"],
)
class TemperatureHigh(kanary.ThresholdRule):
    direction = "high"
    thresholds = [(80, kanary.WARN), (90, kanary.ERROR)]


@kanary.rule(
    rule_id="demo.webhook.stale",
    inputs="demo.webhook:temperature",
    severity=kanary.WARN,
    tags=["demo", "webhook"],
    evaluation_interval=30 * kanary.second,
)
class TemperatureStale(kanary.StaleRule):
    timeout = 5 * kanary.minute
