from __future__ import annotations

from datetime import datetime, timezone
import argparse
import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


STATE = {
    "active": False,
    "severity": "WARN",
    "message": "Manual fake alarm target is idle",
    "updated_at": datetime.now(timezone.utc).isoformat(),
}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class FakeAlarmHandler(BaseHTTPRequestHandler):
    server_version = "KanaryFakeAlarm/0.1"

    def log_message(self, format: str, *args) -> None:
        return

    def do_GET(self) -> None:
        if self.path != "/status":
            self._write_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        self._write_json(HTTPStatus.OK, dict(STATE))

    def do_POST(self) -> None:
        if self.path not in {"/trigger", "/clear", "/set"}:
            self._write_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return

        body = self._read_json_body()
        if self.path == "/clear":
            STATE["active"] = False
            STATE["severity"] = str(body.get("severity") or "WARN").upper()
            STATE["message"] = str(body.get("message") or "Fake alarm cleared")
        else:
            STATE["active"] = bool(body.get("active", True))
            STATE["severity"] = str(body.get("severity") or "WARN").upper()
            STATE["message"] = str(body.get("message") or "Manual fake alarm triggered")
        STATE["updated_at"] = utc_now_iso()
        self._write_json(HTTPStatus.OK, dict(STATE))

    def _read_json_body(self) -> dict[str, object]:
        content_length = int(self.headers.get("Content-Length", "0"))
        if content_length <= 0:
            return {}
        payload = self.rfile.read(content_length).decode("utf-8")
        if not payload.strip():
            return {}
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            return {}
        if isinstance(data, dict):
            return data
        return {}

    def _write_json(self, status: HTTPStatus, payload: dict[str, object]) -> None:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def main() -> None:
    parser = argparse.ArgumentParser(description="Manual fake alarm target for Kanary examples.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18081)
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), FakeAlarmHandler)
    print(f"Fake alarm target listening on http://{args.host}:{args.port}")
    print("GET  /status")
    print("POST /trigger  {\"severity\": \"WARN|ERROR|CRITICAL\", \"message\": \"...\"}")
    print("POST /clear    {\"message\": \"...\"}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
