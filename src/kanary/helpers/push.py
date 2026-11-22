"""Sources for values that arrive by themselves: HTTPPushSource and MQTTSource.

Both put what arrives into a kanary.Inbox and wait for it in poll(), with
interval = 0, so the rules run as soon as a value arrives.
"""

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import logging
import threading
from typing import Any

from ..models import PollResult
from ..source import Source
from ..values import flatten, to_datetime, to_number
from .inbox import Inbox

logger = logging.getLogger("kanary.source")


def _require_waiting(source: Source, helper: str) -> None:
    """These helpers wait for values in poll(), so they only run with interval 0."""
    interval = getattr(source, "interval", None)
    schedule = getattr(source, "schedule", None)
    if interval == 0 and not schedule:
        return
    given = f"schedule={schedule!r}" if schedule else f"interval={interval!r}"
    raise ValueError(
        f"{helper} waits for values in poll() and runs with interval 0; "
        f"remove {given} from source '{source.source_id}'. To evaluate in batches instead, "
        "write a Source with a kanary.Inbox and take() what arrived."
    )


class HTTPPushSource(Source):
    """Receive values over HTTP; the rules run as soon as they arrive.

    Senders POST a JSON object of input name to value::

        curl -X POST http://127.0.0.1:8911/ -d '{"temperature": 21.5, "humidity": 40}'

    A value may carry its own time, as unix time or ISO 8601:
    ``{"temperature": {"value": 21.5, "timestamp": "2026-11-20T10:00:00+09:00"}}``.
    With ``token`` set, requests need ``Authorization: Bearer <token>``.

    The server is the standard library's http.server: fine on a trusted network;
    to accept from the internet, put a reverse proxy with TLS in front of it.
    Override parse(body) for another body format.
    """

    interval = 0  # poll() waits for the next value; any other interval or a schedule fails init()
    host: str = "127.0.0.1"  # "0.0.0.0" accepts from other hosts
    port: int = 8911
    path: str = "/"
    token: str | None = None
    max_body_bytes: int = 1_000_000
    inbox_size: int = 10_000

    def init(self) -> None:
        _require_waiting(self, "HTTPPushSource")
        self.inbox = Inbox(size=self.inbox_size)
        source = self

        class Handler(BaseHTTPRequestHandler):
            timeout = 10  # seconds a sender may take; a stalled one does not hold a thread

            def do_POST(self) -> None:
                status, reply = source._receive(self)
                body = json.dumps(reply).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, message_format: str, *args: Any) -> None:
                return None

        self._server = ThreadingHTTPServer((self.host, int(self.port)), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, name=f"kanary-push-{self.source_id}", daemon=True)
        self._thread.start()

    def poll(self) -> PollResult:
        return self.inbox.wait(timeout=1.0)

    def terminate(self) -> None:
        server = getattr(self, "_server", None)
        if server is not None:
            server.shutdown()
            server.server_close()
            self._thread.join(timeout=2.0)

    def parse(self, body: Any) -> list[tuple[Any, ...]]:
        """The (name, value, timestamp) items of one request body."""
        if not isinstance(body, dict) or not body:
            raise ValueError("the body must be a JSON object of input name to value")
        items: list[tuple[Any, ...]] = []
        for name, raw in body.items():
            if isinstance(raw, dict):
                if "value" not in raw:
                    raise ValueError(f"{name}: an object needs a 'value'")
                items.append((str(name), raw["value"], to_datetime(raw.get("timestamp"))))
            else:
                items.append((str(name), raw, None))
        return items

    def _receive(self, request: BaseHTTPRequestHandler) -> tuple[HTTPStatus, dict[str, Any]]:
        if request.path.split("?", 1)[0] != self.path:
            return HTTPStatus.NOT_FOUND, {"error": f"POST to {self.path}"}
        if self.token:
            given = request.headers.get("Authorization", "")
            if not hmac.compare_digest(given.encode(), f"Bearer {self.token}".encode()):
                return HTTPStatus.UNAUTHORIZED, {"error": "missing or wrong token"}
        try:
            length = int(request.headers.get("Content-Length", ""))
        except ValueError:
            return HTTPStatus.LENGTH_REQUIRED, {"error": "a Content-Length header with the body size is needed"}
        if length < 0:
            return HTTPStatus.BAD_REQUEST, {"error": "Content-Length must not be negative"}
        if length > self.max_body_bytes:
            return HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": f"the body is over {self.max_body_bytes} bytes"}
        try:
            items = self.parse(json.loads(request.rfile.read(length) or b"null"))
            self.inbox.put(items)
        except (TypeError, ValueError) as exc:
            return HTTPStatus.BAD_REQUEST, {"error": str(exc)}
        return HTTPStatus.ACCEPTED, {"accepted": len(items)}


class MQTTSource(Source):
    """Subscribe to MQTT topics; the rules run as soon as a message arrives.

    ``topics`` maps a topic filter to an input name. A filter mapped to None, and
    every filter with a wildcard ("+" one level, "#" the rest), names each input
    after its topic with "/" -> ".": "plant/pumps/p1/flow" -> "plant.pumps.p1.flow".
    A payload is a number ("21.5"), a state ("ON" / "true" -> 1.0, as
    kanary.to_number() reads it), other text (kept as it is), or JSON; a JSON object
    gives one input per key ("room_a.t", "room_a.rh" for a topic named "room_a").
    The input "mqtt.connected" is 1 while connected to the broker, else 0.

    Needs the paho-mqtt package in the Python environment that runs KANARY (see
    docs/deployment.md). The client reconnects by
    itself and subscribes again after each connect; a retained message gives a
    value right after start. Override parse(payload) for another payload format.
    """

    interval = 0  # poll() waits for the next message; any other interval or a schedule fails init()
    host: str = "localhost"
    port: int = 1883
    username: str | None = None
    password: str | None = None
    tls: bool = False  # True: TLS with the system's CA certificates (usually port 8883)
    client_id: str = ""  # empty: a random id
    qos: int = 1
    keepalive: int = 60
    topics: dict[str, str | None] | list[str] = {}
    inbox_size: int = 10_000

    def init(self) -> None:
        _require_waiting(self, "MQTTSource")
        try:
            import paho.mqtt.client as mqtt
        except ImportError as exc:
            raise RuntimeError(
                "MQTTSource needs the paho-mqtt package: install it into the Python environment that runs "
                "KANARY (see docs/deployment.md: When A Plugin Needs Additional Python Modules)"
            ) from exc
        self._mqtt = mqtt
        self._filters = self._topic_filters()
        self.inbox = Inbox(size=self.inbox_size)
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=self.client_id)
        if self.username:
            client.username_pw_set(self.username, self.password)
        if self.tls:
            client.tls_set()
        client.reconnect_delay_set(min_delay=1, max_delay=60)
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        self.client = client
        client.connect_async(self.host, int(self.port), keepalive=int(self.keepalive))
        client.loop_start()

    def poll(self) -> PollResult:
        return self.inbox.wait(timeout=1.0)

    def terminate(self) -> None:
        client = getattr(self, "client", None)
        if client is not None:
            client.disconnect()
            client.loop_stop()

    def parse(self, payload: bytes) -> Any:
        """A number, a dict (from a JSON object), or the text itself."""
        text = payload.decode("utf-8", errors="replace").strip()
        try:
            document = json.loads(text)
        except ValueError:
            document = text
        if isinstance(document, dict):
            return document
        number = to_number(document)
        return number if number is not None else text

    def input_name(self, topic: str) -> str | None:
        """The input a topic goes to, or None for a topic no filter matches."""
        for topic_filter, name in self._filters.items():
            if self._mqtt.topic_matches_sub(topic_filter, topic):
                return name or topic.replace("/", ".")
        return None

    def _topic_filters(self) -> dict[str, str | None]:
        filters = dict.fromkeys(self.topics) if isinstance(self.topics, (list, tuple)) else dict(self.topics)
        if not filters:
            raise ValueError(f"{type(self).__name__}.topics is empty")
        for topic_filter, name in filters.items():
            if name and ("+" in topic_filter or "#" in topic_filter):
                raise ValueError(
                    f"topic filter {topic_filter!r} has a wildcard, so each topic is its own input: map it to None"
                )
        return filters

    def _on_connect(self, client: Any, userdata: Any, flags: Any, reason_code: Any, properties: Any) -> None:
        if getattr(reason_code, "is_failure", False):
            self.inbox.put(("mqtt.connected", 0, None, {"error": str(reason_code)}))
            return
        for topic_filter in self._filters:
            client.subscribe(topic_filter, qos=self.qos)
        self.inbox.put(("mqtt.connected", 1, None, {"broker": f"{self.host}:{self.port}"}))

    def _on_disconnect(self, client: Any, userdata: Any, flags: Any, reason_code: Any, properties: Any) -> None:
        self.inbox.put(("mqtt.connected", 0, None, {"error": str(reason_code)}))

    def _on_message(self, client: Any, userdata: Any, message: Any) -> None:
        name = self.input_name(message.topic)
        if name is None:
            return
        try:
            value = self.parse(message.payload)
        except Exception:
            logger.exception("source '%s' could not read a message on %s", self.source_id, message.topic)
            return
        meta = {"topic": message.topic}
        if isinstance(value, dict):
            self.inbox.put([(f"{name}.{key}", _number_or_raw(item), None, meta) for key, item in flatten(value).items()])
        else:
            self.inbox.put((name, value, None, meta))


def _number_or_raw(value: Any) -> Any:
    number = to_number(value)
    return number if number is not None else value
