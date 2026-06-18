from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ast
from importlib import metadata as importlib_metadata
import json
import os
from pathlib import Path
import subprocess
import tomllib
from typing import Callable
from urllib.parse import unquote

from .engine import Engine
from .constants import AlertState

WEB_ROOT = Path(__file__).with_name("web")
PROJECT_ROOT = Path(__file__).resolve().parents[2]


class ControlAPI:
    def __init__(
        self,
        *,
        engine_getter: Callable[[], Engine | None],
        reload_callback: Callable[..., object],
        meta_getter: Callable[[], dict[str, object]] | None = None,
        host: str = "0.0.0.0",
        port: int = 8000,
        enable_default_viewer: bool = True,
    ) -> None:
        self._engine_getter = engine_getter
        self._reload_callback = reload_callback
        self._meta_getter = meta_getter or (lambda: {})
        self._enable_default_viewer = enable_default_viewer
        self.host = host
        self.port = port
        self._server = ThreadingHTTPServer((host, port), self._build_handler())
        self._server.control_api = self

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
                    if not self.server.control_api._enable_default_viewer:
                        self._write_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                        return
                    self._write_file(WEB_ROOT / "index.html", "text/html; charset=utf-8")
                    return

                if request_path == "/viewer/app.js":
                    if not self.server.control_api._enable_default_viewer:
                        self._write_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                        return
                    self._write_file(WEB_ROOT / "app.js", "application/javascript; charset=utf-8")
                    return

                if request_path == "/viewer/styles.css":
                    if not self.server.control_api._enable_default_viewer:
                        self._write_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                        return
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

                if request_path.startswith("/test-evaluate-template/"):
                    engine = engine_getter()
                    if engine is None:
                        self._write_json(
                            HTTPStatus.SERVICE_UNAVAILABLE,
                            {"status": "starting"},
                        )
                        return
                    rule_id = unquote(request_path[len("/test-evaluate-template/") :]).strip("/")
                    try:
                        payload = engine.test_evaluate_template(rule_id)
                    except KeyError:
                        self._write_json(HTTPStatus.NOT_FOUND, {"error": "rule not found"})
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

                if request_path == "/meta":
                    self._write_json(HTTPStatus.OK, _installation_metadata(engine_getter(), self.server.control_api._meta_getter()))
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
                        alerts.append(_viewer_alert_payload(engine, alert, rule))
                    self._write_json(HTTPStatus.OK, {"alerts": alerts})
                    return

                if request_path == "/export-alerts":
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
                        alerts.append(_export_alert_payload(engine, alert, rule))
                    self._write_json(
                        HTTPStatus.OK,
                        {"node_id": engine.node_id, "alerts": alerts},
                    )
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
                        plugin_class = plugin if isinstance(plugin, type) else plugin.__class__ if plugin is not None else None
                        definition_file = (
                            status.definition_file
                            or (getattr(plugin_class, "__kanary_definition_file__", None) if plugin_class is not None else None)
                        )
                        plugins.append(
                            {
                                "type": status.plugin_type,
                                "plugin_id": status.plugin_id,
                                "state": status.state,
                                "loaded": status.loaded,
                                "init_ok": status.init_ok,
                                "dirty_reason": status.dirty_reason,
                                "last_error": status.last_error,
                                "last_error_detail": status.last_error_detail,
                                "run_count": status.run_count,
                                "last_run_at": status.last_run_at,
                                "last_success_at": status.last_success_at,
                                "last_failure_at": status.last_failure_at,
                                "last_updated_at": status.last_updated_at,
                                "definition_file": definition_file,
                                "definition_file_name": Path(definition_file).name if definition_file else None,
                                "description": getattr(plugin, "description", None) if plugin is not None else None,
                                "tags": list(getattr(plugin, "tags", [])) if plugin is not None else [],
                                "owner": getattr(plugin, "owner", None) if status.plugin_type == "rule" and plugin is not None else None,
                                "runbook": getattr(plugin, "runbook", None) if status.plugin_type == "rule" and plugin is not None else None,
                                "inputs": list(getattr(plugin, "inputs", [])) if status.plugin_type == "rule" and plugin is not None else [],
                                "resolved_sources": list(getattr(plugin, "resolved_sources", [])) if status.plugin_type == "rule" and plugin is not None else [],
                                "matched_outputs": list(getattr(plugin, "matched_outputs", [])) if status.plugin_type == "rule" and plugin is not None else [],
                                "include_tags": list(getattr(plugin, "include_tags", [])) if status.plugin_type == "output" and plugin is not None else [],
                                "exclude_tags": list(getattr(plugin, "exclude_tags", [])) if status.plugin_type == "output" and plugin is not None else [],
                                "exclude_states": list(getattr(plugin, "exclude_states", [])) if status.plugin_type == "output" and plugin is not None else [],
                                "exclude_transitions": list(getattr(plugin, "exclude_transitions", [])) if status.plugin_type == "output" and plugin is not None else [],
                                "minimum_severity": _plugin_minimum_severity(plugin, status.plugin_type),
                            }
                        )
                    plugins.sort(key=lambda row: (row["type"], row["plugin_id"]))
                    self._write_json(HTTPStatus.OK, {"plugins": plugins})
                    return

                self._write_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

            def do_POST(self) -> None:
                engine = engine_getter()

                if self.path == "/reload":
                    payload = self._read_json_body(allow_empty=True)
                    try:
                        try:
                            result = reload_callback(payload)
                        except TypeError:
                            result = reload_callback()
                    except Exception as exc:
                        self._write_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                        return
                    if isinstance(result, bool):
                        status = HTTPStatus.OK if result else HTTPStatus.INTERNAL_SERVER_ERROR
                        payload = {"status": "reloaded" if result else "reload_failed"}
                    else:
                        payload = result
                        status = HTTPStatus.OK if payload.get("status") == "reloaded" else HTTPStatus.INTERNAL_SERVER_ERROR
                    self._write_json(status, payload)
                    return

                if engine is None:
                    self._write_json(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        {"status": "starting"},
                    )
                    return

                if self.path.startswith("/test-poll/"):
                    source_id = unquote(self.path[len("/test-poll/") :]).strip("/")
                    try:
                        payload = engine.test_poll(source_id)
                    except KeyError:
                        self._write_json(HTTPStatus.NOT_FOUND, {"error": "source not found"})
                        return
                    except Exception as exc:
                        self._write_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                        return
                    self._write_json(HTTPStatus.OK, payload)
                    return

                if self.path.startswith("/test-evaluate/"):
                    rule_id = unquote(self.path[len("/test-evaluate/") :]).strip("/")
                    body = self._read_json_body()
                    try:
                        payload = engine.test_evaluate(
                            rule_id,
                            body["payload"],
                            now=_parse_datetime(body["now"]) if body.get("now") else None,
                        )
                    except KeyError as exc:
                        self._write_json(HTTPStatus.BAD_REQUEST, {"error": f"missing field: {exc.args[0]}"})
                        return
                    except Exception as exc:
                        self._write_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                        return
                    self._write_json(HTTPStatus.OK, payload)
                    return

                if self.path.startswith("/test-fire/"):
                    rule_id = unquote(self.path[len("/test-fire/") :]).strip("/")
                    body = self._read_json_body()
                    try:
                        payload = engine.test_fire(
                            rule_id,
                            state=_parse_alert_state(body["state"]),
                            message=body.get("message"),
                            reason=body.get("reason"),
                            now=_parse_datetime(body["now"]) if body.get("now") else None,
                        )
                    except KeyError as exc:
                        self._write_json(HTTPStatus.BAD_REQUEST, {"error": f"missing field: {exc.args[0]}"})
                        return
                    except Exception as exc:
                        self._write_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                        return
                    self._write_json(HTTPStatus.OK, payload)
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

            def _read_json_body(self, allow_empty: bool = False) -> dict:
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


def _parse_alert_state(value: str) -> AlertState:
    try:
        return AlertState(str(value))
    except ValueError as exc:
        raise ValueError(f"invalid alert state: {value}") from exc


def _json_datetime_fromisoformat(value: str):
    from datetime import datetime

    return datetime.fromisoformat(value)


def _duration_to_timedelta_minutes(duration_minutes: float):
    from datetime import timedelta

    return timedelta(minutes=duration_minutes)


def _resolve_plugin(engine: Engine, plugin_type: str, plugin_id: str) -> object | None:
    if plugin_type == "source":
        plugin = engine.sources.get(plugin_id)
    elif plugin_type == "rule":
        plugin = engine.rules.get(plugin_id)
    elif plugin_type == "output":
        plugin = engine.outputs.get(plugin_id)
    else:
        plugin = None
    if plugin is not None:
        return plugin
    return getattr(engine, "runtime_discovered_plugin_classes", {}).get((plugin_type, plugin_id))


def _installation_metadata(engine: Engine | None = None, extra: dict[str, object] | None = None) -> dict[str, object]:
    result = {
        "package_name": "kanary",
        "version": None,
        "git_commit": _git_commit_hash(),
        "homepage_url": None,
        "repository_url": None,
        "documentation_url": None,
        "issues_url": None,
        "state_db_enabled": bool(getattr(getattr(engine, "store", None), "enabled", False)),
        "state_db_schema_version": getattr(getattr(engine, "store", None), "schema_version", 0) if engine is not None else 0,
        "state_db_target_schema_version": getattr(getattr(engine, "store", None), "target_schema_version", 1) if engine is not None else 1,
    }
    if extra:
        result.update(extra)
    try:
        dist_metadata = importlib_metadata.metadata("kanary")
        result["version"] = importlib_metadata.version("kanary")
        for line in dist_metadata.get_all("Project-URL", []):
            try:
                label, url = [part.strip() for part in line.split(",", 1)]
            except ValueError:
                continue
            if label == "Homepage":
                result["homepage_url"] = url
            elif label == "Repository":
                result["repository_url"] = url
            elif label == "Documentation":
                result["documentation_url"] = url
            elif label == "Issues":
                result["issues_url"] = url
        if result["homepage_url"] is None and dist_metadata.get("Home-page"):
            result["homepage_url"] = dist_metadata.get("Home-page")
        if result["repository_url"] is None:
            result["repository_url"] = result["homepage_url"]
        return result
    except importlib_metadata.PackageNotFoundError:
        return _installation_metadata_from_pyproject(result)


def _installation_metadata_from_pyproject(base: dict[str, object]) -> dict[str, object]:
    pyproject_path = PROJECT_ROOT / "pyproject.toml"
    if not pyproject_path.exists():
        return base
    try:
        project = tomllib.loads(pyproject_path.read_text(encoding="utf-8")).get("project", {})
    except Exception:
        return base

    urls = project.get("urls", {})
    return {
        "package_name": project.get("name", base["package_name"]),
        "version": project.get("version"),
        "git_commit": base["git_commit"],
        "homepage_url": urls.get("Homepage"),
        "repository_url": urls.get("Repository") or urls.get("Homepage"),
        "documentation_url": urls.get("Documentation"),
        "issues_url": urls.get("Issues"),
    }


def _git_commit_hash() -> str | None:
    env_value = os.environ.get("KANARY_GIT_COMMIT")
    if env_value:
        return env_value

    git_dir = PROJECT_ROOT / ".git"
    if not git_dir.exists():
        return None

    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=PROJECT_ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
    except Exception:
        return None

    commit_hash = completed.stdout.strip()
    return commit_hash or None


def _viewer_alert_payload(engine: Engine, alert, rule) -> dict[str, object]:
    payload = _export_alert_payload(engine, alert, rule)
    payload["acked_by"] = alert.acked_by
    payload["acked_at"] = alert.acked_at
    payload["ack_reason"] = alert.ack_reason
    payload["active_silence_ids"] = list(alert.active_silence_ids)
    payload["active_silences"] = [
        {
            "silence_id": silence.silence_id,
            "created_by": silence.created_by,
            "reason": silence.reason,
            "created_at": silence.created_at,
            "start_at": silence.start_at,
            "end_at": silence.end_at,
        }
        for silence_id in alert.active_silence_ids
        if (silence := engine.silences.get(silence_id)) is not None
    ]
    payload["matched_outputs"] = list(getattr(rule, "matched_outputs", [])) if rule else []
    payload["description"] = getattr(rule, "description", None) if rule else None
    payload["runbook"] = getattr(rule, "runbook", None) if rule else None
    return payload


def _export_alert_payload(engine: Engine, alert, rule) -> dict[str, object]:
    payload = alert.payload if isinstance(alert.payload, dict) else {}
    remote_alarm = payload.get("remote_alarm") if isinstance(payload, dict) else None
    if isinstance(remote_alarm, dict):
        origin_node_id = str(remote_alarm.get("origin_node_id") or remote_alarm.get("node_id") or engine.node_id)
        origin_rule_id = str(remote_alarm.get("origin_rule_id") or remote_alarm.get("rule_id") or alert.rule_id)
        mirror_path = [str(node_id) for node_id in list(remote_alarm.get("mirror_path") or [])]
        if engine.node_id not in mirror_path:
            mirror_path.append(engine.node_id)
        is_mirrored = True
    else:
        origin_node_id = engine.node_id
        origin_rule_id = alert.rule_id
        mirror_path = [engine.node_id]
        is_mirrored = False

    return {
        "node_id": engine.node_id,
        "rule_id": alert.rule_id,
        "state": alert.state.value,
        "severity": alert.severity.value,
        "owner": alert.owner,
        "tags": list(alert.tags),
        "message": alert.message,
        "description": getattr(rule, "description", None) if rule else None,
        "runbook": getattr(rule, "runbook", None) if rule else None,
        "payload": alert.payload,
        "last_evaluated_at": alert.last_evaluated_at,
        "definition_file": getattr(rule.__class__, "__kanary_definition_file__", None) if rule is not None else None,
        "origin_node_id": origin_node_id,
        "origin_rule_id": origin_rule_id,
        "mirror_path": mirror_path,
        "is_mirrored": is_mirrored,
    }


def _plugin_source_payload(engine: Engine, plugin_type: str, plugin_id: str) -> dict[str, object]:
    plugin = _resolve_plugin(engine, plugin_type, plugin_id)
    if plugin is None:
        raise KeyError(plugin_id)

    plugin_class = plugin if isinstance(plugin, type) else plugin.__class__
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


def _plugin_minimum_severity(plugin: object | None, plugin_type: str) -> object | None:
    if plugin_type != "output" or plugin is None:
        return None
    value = getattr(plugin, "minimum_severity", None)
    return value.name if hasattr(value, "name") else value


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
