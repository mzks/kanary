from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ast
import json
from pathlib import Path
from typing import Callable
from urllib.parse import unquote

from .engine import Engine

WEB_ROOT = Path(__file__).with_name("web")


class ControlAPI:
    def __init__(
        self,
        *,
        engine_getter: Callable[[], Engine | None],
        reload_callback: Callable[[], bool],
        host: str = "127.0.0.1",
        port: int = 8000,
    ) -> None:
        self._engine_getter = engine_getter
        self._reload_callback = reload_callback
        self.host = host
        self.port = port
        self._server = ThreadingHTTPServer((host, port), self._build_handler())

    def start(self) -> None:
        self._server.serve_forever()

    def shutdown(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def _build_handler(self) -> type[BaseHTTPRequestHandler]:
        engine_getter = self._engine_getter
        reload_callback = self._reload_callback

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                request_path = self.path.split("?", 1)[0]

                if request_path == "/viewer":
                    self._write_file(WEB_ROOT / "index.html", "text/html; charset=utf-8")
                    return

                if request_path == "/viewer/app.js":
                    self._write_file(WEB_ROOT / "app.js", "application/javascript; charset=utf-8")
                    return

                if request_path == "/viewer/styles.css":
                    self._write_file(WEB_ROOT / "styles.css", "text/css; charset=utf-8")
                    return

                if request_path.startswith("/plugins/") and request_path.endswith("/source"):
                    engine = engine_getter()
                    if engine is None:
                        self._write_json(
                            HTTPStatus.SERVICE_UNAVAILABLE,
                            {"status": "starting"},
                        )
                        return
                    parts = [unquote(part) for part in request_path.strip("/").split("/")]
                    if len(parts) != 4:
                        self._write_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                        return
                    _, plugin_type, plugin_id, _ = parts
                    try:
                        payload = _plugin_source_payload(engine, plugin_type, plugin_id)
                    except FileNotFoundError:
                        self._write_json(HTTPStatus.NOT_FOUND, {"error": "definition file not found"})
                        return
                    except KeyError:
                        self._write_json(HTTPStatus.NOT_FOUND, {"error": "plugin not found"})
                        return
                    self._write_json(HTTPStatus.OK, payload)
                    return

                if request_path == "/health":
                    engine = engine_getter()
                    if engine is None:
                        self._write_json(
                            HTTPStatus.SERVICE_UNAVAILABLE,
                            {"status": "starting"},
                        )
                        return

                    self._write_json(
                        HTTPStatus.OK,
                        {
                            "status": "ok",
                            "sources": sorted(engine.sources),
                            "rules": sorted(engine.rules),
                            "alert_count": len(engine.alerts),
                        },
                    )
                    return

                if request_path == "/peer-status":
                    engine = engine_getter()
                    if engine is None:
                        self._write_json(
                            HTTPStatus.SERVICE_UNAVAILABLE,
                            {"status": "starting"},
                        )
                        return

                    self._write_json(HTTPStatus.OK, engine.peer_status())
                    return

                if request_path == "/alerts":
                    engine = engine_getter()
                    if engine is None:
                        self._write_json(
                            HTTPStatus.SERVICE_UNAVAILABLE,
                            {"status": "starting"},
                        )
                        return

                    alerts = []
                    for alert in engine.alerts.values():
                        rule = engine.rules.get(alert.rule_id)
                        alerts.append(
                            {
                                "rule_id": alert.rule_id,
                                "state": alert.state.value,
                                "severity": int(alert.severity),
                                "owner": alert.owner,
                                "tags": list(alert.tags),
                                "message": alert.message,
                                "payload": alert.payload,
                                "last_evaluated_at": alert.last_evaluated_at,
                                "matched_outputs": list(getattr(rule, "matched_outputs", [])) if rule is not None else [],
                                "definition_file": getattr(rule.__class__, "__kanary_definition_file__", None) if rule is not None else None,
                                "acked_at": alert.acked_at,
                                "acked_by": alert.acked_by,
                                "ack_reason": alert.ack_reason,
                                "active_silence_ids": list(alert.active_silence_ids),
                            }
                        )
                    self._write_json(HTTPStatus.OK, {"alerts": alerts})
                    return

                if request_path.startswith("/history/"):
                    engine = engine_getter()
                    if engine is None:
                        self._write_json(
                            HTTPStatus.SERVICE_UNAVAILABLE,
                            {"status": "starting"},
                        )
                        return
                    rule_id = unquote(request_path[len("/history/") :]).strip("/")
                    self._write_json(HTTPStatus.OK, engine.get_rule_history(rule_id))
                    return

                if request_path == "/silences":
                    engine = engine_getter()
                    if engine is None:
                        self._write_json(
                            HTTPStatus.SERVICE_UNAVAILABLE,
                            {"status": "starting"},
                        )
                        return

                    silences = []
                    now = engine._now_fn()
                    for silence in engine.list_silences():
                        silences.append(
                            {
                                "silence_id": silence.silence_id,
                                "created_by": silence.created_by,
                                "reason": silence.reason,
                                "created_at": silence.created_at,
                                "start_at": silence.start_at,
                                "end_at": silence.end_at,
                                "rule_patterns": list(silence.rule_patterns),
                                "tags": list(silence.tags),
                                "remote_silence_refs": list(silence.remote_silence_refs),
                                "cancelled_at": silence.cancelled_at,
                                "cancelled_by": silence.cancelled_by,
                                "cancel_reason": silence.cancel_reason,
                                "active": silence.cancelled_at is None and silence.start_at <= now < silence.end_at,
                            }
                        )
                    silences.sort(key=lambda row: (row["start_at"], row["silence_id"]))
                    self._write_json(HTTPStatus.OK, {"silences": silences})
                    return

                if request_path == "/plugins":
                    engine = engine_getter()
                    if engine is None:
                        self._write_json(
                            HTTPStatus.SERVICE_UNAVAILABLE,
                            {"status": "starting"},
                        )
                        return

                    plugins = []
                    for status in engine.plugin_states.values():
                        plugin = _resolve_plugin(engine, status.plugin_type, status.plugin_id)
                        plugins.append(
                            {
                                "type": status.plugin_type,
                                "plugin_id": status.plugin_id,
                                "state": status.state,
                                "init_ok": status.init_ok,
                                "last_error": status.last_error,
                                "run_count": status.run_count,
                                "last_run_at": status.last_run_at,
                                "last_success_at": status.last_success_at,
                                "last_failure_at": status.last_failure_at,
                                "last_updated_at": status.last_updated_at,
                                "definition_file": getattr(plugin.__class__, "__kanary_definition_file__", None) if plugin is not None else None,
                            }
                        )
                    plugins.sort(key=lambda row: (row["type"], row["plugin_id"]))
                    self._write_json(HTTPStatus.OK, {"plugins": plugins})
                    return

                self._write_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

            def do_POST(self) -> None:
                engine = engine_getter()

                if self.path == "/reload":
                    reloaded = reload_callback()
                    status = HTTPStatus.OK if reloaded else HTTPStatus.INTERNAL_SERVER_ERROR
                    payload = {"status": "reloaded" if reloaded else "reload_failed"}
                    self._write_json(status, payload)
                    return

                if engine is None:
                    self._write_json(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        {"status": "starting"},
                    )
                    return

                if self.path.startswith("/alerts/") and self.path.endswith("/ack"):
                    rule_id = unquote(self.path[len("/alerts/") : -len("/ack")]).strip("/")
                    body = self._read_json_body()
                    try:
                        alert = engine.acknowledge(
                            rule_id,
                            operator=body["operator"],
                            reason=body.get("reason"),
                        )
                    except KeyError as exc:
                        self._write_json(HTTPStatus.BAD_REQUEST, {"error": f"missing field: {exc.args[0]}"})
                        return
                    except Exception as exc:
                        self._write_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                        return
                    self._write_json(HTTPStatus.OK, {"status": "acked", "rule_id": alert.rule_id})
                    return

                if self.path.startswith("/alerts/") and self.path.endswith("/unack"):
                    rule_id = unquote(self.path[len("/alerts/") : -len("/unack")]).strip("/")
                    body = self._read_json_body()
                    try:
                        alert = engine.unacknowledge(
                            rule_id,
                            operator=body["operator"],
                            reason=body.get("reason"),
                        )
                    except KeyError as exc:
                        self._write_json(HTTPStatus.BAD_REQUEST, {"error": f"missing field: {exc.args[0]}"})
                        return
                    except Exception as exc:
                        self._write_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                        return
                    self._write_json(HTTPStatus.OK, {"status": "unacked", "rule_id": alert.rule_id})
                    return

                if self.path == "/silences/duration":
                    body = self._read_json_body()
                    try:
                        duration_minutes = float(body["duration_minutes"])
                        start_at = _parse_datetime(body.get("start_at")) if body.get("start_at") else engine._now_fn()
                        warnings = engine.silence_target_warnings(
                            rule_patterns=body.get("rule_patterns"),
                            tags=body.get("tags"),
                        )
                        silence = engine.create_silence(
                            operator=body["operator"],
                            reason=body.get("reason"),
                            start_at=start_at,
                            end_at=start_at + _duration_to_timedelta_minutes(duration_minutes),
                            rule_patterns=body.get("rule_patterns"),
                            tags=body.get("tags"),
                        )
                    except KeyError as exc:
                        self._write_json(HTTPStatus.BAD_REQUEST, {"error": f"missing field: {exc.args[0]}"})
                        return
                    except Exception as exc:
                        self._write_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                        return
                    self._write_json(
                        HTTPStatus.OK,
                        {"status": "silenced", "silence_id": silence.silence_id, "warnings": warnings},
                    )
                    return

                if self.path == "/silences/window":
                    body = self._read_json_body()
                    try:
                        warnings = engine.silence_target_warnings(
                            rule_patterns=body.get("rule_patterns"),
                            tags=body.get("tags"),
                        )
                        silence = engine.create_silence(
                            operator=body["operator"],
                            reason=body.get("reason"),
                            start_at=_parse_datetime(body["start_at"]),
                            end_at=_parse_datetime(body["end_at"]),
                            rule_patterns=body.get("rule_patterns"),
                            tags=body.get("tags"),
                        )
                    except KeyError as exc:
                        self._write_json(HTTPStatus.BAD_REQUEST, {"error": f"missing field: {exc.args[0]}"})
                        return
                    except Exception as exc:
                        self._write_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                        return
                    self._write_json(
                        HTTPStatus.OK,
                        {"status": "silenced", "silence_id": silence.silence_id, "warnings": warnings},
                    )
                    return

                if self.path.startswith("/silences/") and self.path.endswith("/cancel"):
                    silence_id = unquote(self.path[len("/silences/") : -len("/cancel")]).strip("/")
                    body = self._read_json_body()
                    try:
                        silence = engine.cancel_silence(
                            silence_id,
                            operator=body["operator"],
                            reason=body.get("reason"),
                        )
                    except KeyError as exc:
                        self._write_json(HTTPStatus.BAD_REQUEST, {"error": f"missing field: {exc.args[0]}"})
                        return
                    except Exception as exc:
                        self._write_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                        return
                    self._write_json(HTTPStatus.OK, {"status": "unsilenced", "silence_id": silence.silence_id})
                    return

                self._write_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

            def log_message(self, format: str, *args) -> None:
                return None

            def _write_json(self, status: HTTPStatus, payload: dict) -> None:
                body = json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _write_file(self, path: Path, content_type: str) -> None:
                try:
                    body = path.read_bytes()
                except FileNotFoundError:
                    self._write_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                    return
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _read_json_body(self) -> dict:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0:
                    return {}
                return json.loads(self.rfile.read(length).decode())

        return Handler


def _json_default(value: object) -> str:
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        return isoformat()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _parse_datetime(value: str):
    return _json_datetime_fromisoformat(value)


def _json_datetime_fromisoformat(value: str):
    from datetime import datetime

    return datetime.fromisoformat(value)


def _duration_to_timedelta_minutes(duration_minutes: float):
    from datetime import timedelta

    return timedelta(minutes=duration_minutes)


def _resolve_plugin(engine: Engine, plugin_type: str, plugin_id: str) -> object | None:
    if plugin_type == "source":
        return engine.sources.get(plugin_id)
    if plugin_type == "rule":
        return engine.rules.get(plugin_id)
    if plugin_type == "output":
        return engine.outputs.get(plugin_id)
    return None


def _plugin_source_payload(engine: Engine, plugin_type: str, plugin_id: str) -> dict[str, object]:
    plugin = _resolve_plugin(engine, plugin_type, plugin_id)
    if plugin is None:
        raise KeyError(plugin_id)

    plugin_class = plugin.__class__
    definition_file = getattr(plugin_class, "__kanary_definition_file__", None)
    if not definition_file:
        raise FileNotFoundError(plugin_id)

    path = Path(definition_file)
    source_text = path.read_text(encoding="utf-8")
    snippet = _extract_class_source(source_text, plugin_class.__name__)
    if snippet is None:
        return {
            "plugin_id": plugin_id,
            "type": plugin_type,
            "definition_file": str(path),
            "symbol_name": plugin_class.__name__,
            "mode": "file",
            "start_line": 1,
            "end_line": len(source_text.splitlines()),
            "source_text": source_text,
        }

    return {
        "plugin_id": plugin_id,
        "type": plugin_type,
        "definition_file": str(path),
        "symbol_name": plugin_class.__name__,
        "mode": "class",
        "start_line": snippet["start_line"],
        "end_line": snippet["end_line"],
        "source_text": snippet["source_text"],
    }


def _extract_class_source(source_text: str, class_name: str) -> dict[str, object] | None:
    tree = ast.parse(source_text)
    lines = source_text.splitlines()
    for node in tree.body:
        if not isinstance(node, ast.ClassDef) or node.name != class_name:
            continue
        start_line = min([node.lineno, *[decorator.lineno for decorator in node.decorator_list]] or [node.lineno])
        end_line = node.end_lineno or node.lineno
        return {
            "start_line": start_line,
            "end_line": end_line,
            "source_text": "\n".join(lines[start_line - 1:end_line]),
        }
    return None
