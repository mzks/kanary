"""Accept webhook values with a PushSource.

This example uses only the standard library. ``http.server`` is for local
testing, not a production server. Replace the listener for production use; a
reverse proxy can separately provide TLS, authentication, and path controls.

Run Kanary:
    kanary examples/webhook_push_source.py --api-host 127.0.0.1 --api-port 8910

Send an input snapshot to the plugin-owned endpoint:
    curl -X POST http://127.0.0.1:8911/inputs \
      -H 'Content-Type: application/json' \
      -d '{"temperature": 92.1, "fan_rpm": 1200, "timestamp": "2026-07-20T10:00:00+09:00"}'

``timestamp`` is optional and defaults to the time the webhook is received.
"""

from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading

import kanary


@kanary.source(source_id="demo.webhook", interval=1 * kanary.minute)
class WebhookInputs(kanary.PushSource):
    """Accept the latest webhook snapshot and evaluate it immediately."""

    webhook_host = "127.0.0.1"
    webhook_port = 8911

    def init(self):
        super().init()
        source = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                if self.path != "/inputs":
                    self._write_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                    return
                try:
                    payload = self._read_json()
                    timestamp = self._timestamp(payload.get("timestamp"))
                    source.push(
                        kanary.inputs(
                            ("temperature", payload["temperature"], timestamp),
                            ("fan_rpm", payload["fan_rpm"], timestamp),
                            metadata={"transport": "webhook"},
                        )
                    )
                except (KeyError, TypeError, ValueError) as exc:
                    self._write_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                    return
                self._write_json(HTTPStatus.ACCEPTED, {"status": "accepted"})

            def log_message(self, message_format, *args):
                return None

            def _read_json(self):
                length = int(self.headers.get("Content-Length", "0"))
                return json.loads(self.rfile.read(length).decode())

            def _timestamp(self, value):
                if value is None:
                    return datetime.now(timezone.utc)
                if not isinstance(value, str):
                    raise ValueError("timestamp must be an ISO 8601 string")
                timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if timestamp.tzinfo is None:
                    raise ValueError("timestamp must include a timezone")
                return timestamp

            def _write_json(self, status, payload):
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._webhook_server = ThreadingHTTPServer((self.webhook_host, self.webhook_port), Handler)
        self._webhook_thread = threading.Thread(target=self._webhook_server.serve_forever, daemon=True)
        self._webhook_thread.start()

    def terminate(self):
        self._webhook_server.shutdown()
        self._webhook_server.server_close()
        self._webhook_thread.join(timeout=2.0)


@kanary.rule(
    rule_id="demo.webhook.temperature",
    inputs="demo.webhook:temperature",
    severity=kanary.WARN,
    tags=["demo", "webhook"],
)
class TemperatureHigh(kanary.ThresholdRule):
    direction = "high"
    thresholds = [(80, kanary.WARN), (90, kanary.ERROR)]
