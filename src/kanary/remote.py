from __future__ import annotations

from fnmatch import fnmatch
import json
import re
from typing import Any
from urllib.request import Request, urlopen

from .constants import ACKED, OK, RESOLVED, AlertState, Severity
from .models import Evaluation, Measurement, SourceResult
from .patterns import matches_any_tag, matches_excluded_tag
from .registry import get_source_registry, register_rule
from .rule import Rule, RuleContext
from .source import Source


class RemoteKanarySource(Source):
    base_url: str | None = None
    url: str | None = None
    timeout_seconds: float = 5.0
    alerts_path: str = "/export-alerts"
    ack_path_template: str = "/alerts/{rule_id}/ack"
    unack_path_template: str = "/alerts/{rule_id}/unack"
    silence_window_path: str = "/silences/window"
    unsilence_path_template: str = "/silences/{silence_id}/cancel"

    def poll(self, ctx: dict[str, Any]) -> SourceResult:
        alerts = self.fetch_remote_alerts()
        local_node_id = getattr(ctx.get("engine"), "node_id", None)
        measurements: list[Measurement] = []
        for alert in alerts:
            mirror_path = [str(node_id) for node_id in list(alert.get("mirror_path") or [])]
            if local_node_id is not None and local_node_id in mirror_path:
                continue
            timestamp = _parse_remote_datetime(alert.get("last_evaluated_at")) or ctx["engine"]._now_fn()
            state = str(alert.get("state", OK.value))
            measurements.append(
                Measurement(
                    name=str(alert["rule_id"]),
                    value=0 if state in {OK.value, RESOLVED.value} else 1,
                    timestamp=timestamp,
                    metadata=dict(alert),
                )
            )
        return SourceResult(measurements=measurements, status="ok")

    @classmethod
    def discover_remote_alerts(cls) -> list[dict[str, Any]]:
        instance = cls()
        return instance.fetch_remote_alerts()

    def fetch_remote_alerts(self) -> list[dict[str, Any]]:
        payload = self._read_json("GET", self.alerts_path)
        return list(payload.get("alerts", []))

    def acknowledge_remote(self, remote_alarm_id: str, *, operator: str, reason: str | None = None) -> None:
        self._read_json(
            "POST",
            self.ack_path_template.format(rule_id=remote_alarm_id),
            {"operator": operator, "reason": reason},
        )

    def unacknowledge_remote(self, remote_alarm_id: str, *, operator: str, reason: str | None = None) -> None:
        self._read_json(
            "POST",
            self.unack_path_template.format(rule_id=remote_alarm_id),
            {"operator": operator, "reason": reason},
        )

    def create_remote_silence(
        self,
        remote_alarm_id: str,
        *,
        operator: str,
        reason: str | None,
        start_at: str,
        end_at: str,
    ) -> str:
        payload = self._read_json(
            "POST",
            self.silence_window_path,
            {
                "operator": operator,
                "reason": reason,
                "start_at": start_at,
                "end_at": end_at,
                "rule_patterns": [remote_alarm_id],
            },
        )
        silence_id = payload.get("silence_id")
        if not isinstance(silence_id, str) or not silence_id:
            raise RuntimeError("remote silence response did not include silence_id")
        return silence_id

    def cancel_remote_silence(self, remote_silence_id: str, *, operator: str, reason: str | None = None) -> None:
        self._read_json(
            "POST",
            self.unsilence_path_template.format(silence_id=remote_silence_id),
            {"operator": operator, "reason": reason},
        )

    def _read_json(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        data = None if body is None else json.dumps(body).encode()
        request = Request(_join_url(self._base_url(), path), method=method, data=data)
        request.add_header("Accept", "application/json")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        with urlopen(request, timeout=self.timeout_seconds) as response:
            return json.loads(response.read().decode())

    def _base_url(self) -> str:
        base_url = self.base_url or self.url
        if not isinstance(base_url, str) or not base_url:
            raise RuntimeError(f"{type(self).__name__} must define non-empty base_url")
        return base_url


class RemoteAlarm(Rule):
    remote_alarm_id: str
    propagate_ack: bool = False
    propagate_silence: bool = False

    def evaluate(self, payload: dict[str, Any], ctx: RuleContext) -> Evaluation:
        measurement = ctx.measurement(self.remote_alarm_id)
        metadata = measurement.get("metadata", {})
        if not measurement or not isinstance(metadata, dict):
            return Evaluation(
                state=AlertState.OK,
                payload=payload,
                message=f"remote alarm {self.remote_alarm_id} is missing",
            )

        remote_state = _coerce_alert_state(metadata.get("state"))
        remote_severity = _coerce_severity(metadata.get("severity")) or self.severity
        result_payload = dict(payload)
        result_payload["remote_alarm"] = dict(metadata)
        return Evaluation(
            state=remote_state,
            payload=result_payload,
            message=metadata.get("message"),
            severity=remote_severity,
        )

    @classmethod
    def default_rule_id(cls) -> str | None:
        source_id = getattr(cls, "source", None)
        remote_alarm_id = getattr(cls, "remote_alarm_id", None)
        if not source_id or not remote_alarm_id:
            return None
        return f"{source_id}.{remote_alarm_id}"

    def acknowledge_remote(self, engine: Any, *, operator: str, reason: str | None = None) -> None:
        if not self.propagate_ack:
            return
        self._remote_source(engine).acknowledge_remote(self.remote_alarm_id, operator=operator, reason=reason)

    def unacknowledge_remote(self, engine: Any, *, operator: str, reason: str | None = None) -> None:
        if not self.propagate_ack:
            return
        self._remote_source(engine).unacknowledge_remote(self.remote_alarm_id, operator=operator, reason=reason)

    def create_remote_silence(
        self,
        engine: Any,
        *,
        operator: str,
        reason: str | None,
        start_at: str,
        end_at: str,
    ) -> str | None:
        if not self.propagate_silence:
            return None
        return self._remote_source(engine).create_remote_silence(
            self.remote_alarm_id,
            operator=operator,
            reason=reason,
            start_at=start_at,
            end_at=end_at,
        )

    def cancel_remote_silence(self, engine: Any, remote_silence_id: str, *, operator: str, reason: str | None = None) -> None:
        if not self.propagate_silence:
            return
        self._remote_source(engine).cancel_remote_silence(remote_silence_id, operator=operator, reason=reason)

    def _remote_source(self, engine: Any) -> RemoteKanarySource:
        source = engine.sources[self.source]
        if not isinstance(source, RemoteKanarySource):
            raise TypeError(f"source '{self.source}' is not a RemoteKanarySource")
        return source


def import_remote_alarms(
    *,
    source: str,
    remote_alarm_ids: list[str] | None = None,
    prefix: str | None = None,
    suffix: str | None = None,
    add_tags: list[str] | None = None,
    owner: str | None = None,
    severity: Severity = Severity.ERROR,
    include_rule_ids: list[str] | None = None,
    exclude_rule_ids: list[str] | None = None,
    include_tags: list[str] | None = None,
    exclude_tags: list[str] | None = None,
    propagate_ack: bool = False,
    propagate_silence: bool = False,
) -> list[type[RemoteAlarm]]:
    source_cls = get_source_registry().get(source)
    if source_cls is None:
        raise ValueError(f"unknown source '{source}'")

    discovered_alerts: list[dict[str, Any]]
    if remote_alarm_ids is None:
        if not issubclass(source_cls, RemoteKanarySource):
            raise ValueError("remote_alarm_ids is required unless source is a RemoteKanarySource")
        discovered_alerts = source_cls.discover_remote_alerts()
    else:
        discovered_alerts = [{"rule_id": remote_alarm_id} for remote_alarm_id in remote_alarm_ids]

    generated: list[type[RemoteAlarm]] = []
    include_rule_patterns = include_rule_ids or []
    exclude_rule_patterns = exclude_rule_ids or []
    include_tag_patterns = list(include_tags or [])
    exclude_tag_patterns = list(exclude_tags or [])

    for alert in discovered_alerts:
        remote_alarm_id = str(alert["rule_id"])
        remote_tags = list(alert.get("tags") or [])
        if include_rule_patterns and not any(fnmatch(remote_alarm_id, pattern) for pattern in include_rule_patterns):
            continue
        if exclude_rule_patterns and any(fnmatch(remote_alarm_id, pattern) for pattern in exclude_rule_patterns):
            continue
        if include_tag_patterns and not matches_any_tag(remote_tags, include_tag_patterns):
            continue
        if exclude_tag_patterns and matches_excluded_tag(remote_tags, exclude_tag_patterns):
            continue

        local_rule_id = _compose_local_rule_id(remote_alarm_id, prefix=prefix, suffix=suffix)
        class_name = _generated_class_name(local_rule_id)
        rule_tags = sorted(set(remote_tags).union(add_tags or []))
        attrs = {
            "__module__": source_cls.__module__,
            "rule_id": local_rule_id,
            "source": source,
            "severity": _coerce_severity(alert.get("severity")) or severity,
            "tags": rule_tags,
            "owner": owner if owner is not None else alert.get("owner"),
            "remote_alarm_id": remote_alarm_id,
            "propagate_ack": propagate_ack,
            "propagate_silence": propagate_silence,
        }
        generated_cls = type(class_name, (RemoteAlarm,), attrs)
        register_rule(generated_cls)
        generated.append(generated_cls)

    return generated


def _join_url(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


def _parse_remote_datetime(value: Any):
    from datetime import datetime

    if not isinstance(value, str) or not value:
        return None
    return datetime.fromisoformat(value)


def _coerce_alert_state(value: Any) -> AlertState:
    if isinstance(value, AlertState):
        return value
    if isinstance(value, str):
        try:
            return AlertState(value)
        except ValueError:
            return AlertState.FIRING
    return AlertState.FIRING


def _coerce_severity(value: Any) -> Severity | None:
    if isinstance(value, Severity):
        return value
    if isinstance(value, int):
        try:
            return Severity(value)
        except ValueError:
            return None
    if isinstance(value, str):
        try:
            return Severity[value]
        except KeyError:
            return None
    return None


def _compose_local_rule_id(remote_alarm_id: str, *, prefix: str | None, suffix: str | None) -> str:
    parts = [part for part in [prefix, remote_alarm_id, suffix] if part]
    return ".".join(parts)


def _generated_class_name(local_rule_id: str) -> str:
    normalized = re.sub(r"[^0-9a-zA-Z]+", "_", local_rule_id).strip("_")
    if not normalized:
        normalized = "remote_alarm"
    return f"Imported_{normalized}"
