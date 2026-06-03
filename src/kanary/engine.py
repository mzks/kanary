from collections.abc import Mapping
from datetime import datetime, timezone
from fnmatch import fnmatch
import logging
import socket
import traceback
from uuid import uuid4
import threading
from typing import Callable

from .constants import AlertState, DEESCALATED, ESCALATED, TransitionKind, UNACK
from .models import Acknowledgement, Alert, AlertEvent, Evaluation, PluginStatus, Silence, SourceResult, SourceSnapshot, SourceState
from .output import Output
from .registry import get_output_registry, get_rule_registry, get_source_registry
from .rule import Rule, RuleContext
from .store import NullStore
from .source import Source

logger = logging.getLogger("kanary.engine")


class Engine:
    def __init__(
        self,
        *,
        source_registry: dict[str, type[Source]] | None = None,
        rule_registry: dict[str, type[Rule]] | None = None,
        output_registry: dict[str, type[Output]] | None = None,
        exclude_rule_patterns: list[str] | None = None,
        now_fn: Callable[[], datetime] | None = None,
        store: object | None = None,
        node_id: str | None = None,
    ) -> None:
        self._source_registry = source_registry or get_source_registry()
        self._rule_registry = rule_registry or get_rule_registry()
        self._output_registry = output_registry or get_output_registry()
        self._exclude_rule_patterns = exclude_rule_patterns or []
        self._now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self.node_id = node_id or socket.gethostname()
        self.started_at = self._now_fn()
        self.last_reload_at: datetime | None = None
        self.store = store or NullStore()
        self._lock = threading.RLock()
        self.sources = self._instantiate_sources()
        self.rules = self._instantiate_rules()
        self.outputs = self._instantiate_outputs()
        self.plugin_states: dict[str, PluginStatus] = {}
        self.alerts: dict[str, Alert] = {}
        self.acknowledgements: dict[str, Acknowledgement] = {}
        self.silences: dict[str, Silence] = {}
        self.source_states: dict[str, SourceState] = {
            source_id: SourceState(source_id=source_id)
            for source_id in self.sources
        }
        for source_id in self.sources:
            self.plugin_states[self._plugin_key("source", source_id)] = PluginStatus("source", source_id)
        for rule_id in self.rules:
            self.plugin_states[self._plugin_key("rule", rule_id)] = PluginStatus("rule", rule_id)
        for output_id in self.outputs:
            self.plugin_states[self._plugin_key("output", output_id)] = PluginStatus("output", output_id)
        self._sync_plugin_definition_files()

    def _instantiate_sources(self) -> dict[str, Source]:
        return {source_id: cls() for source_id, cls in self._source_registry.items()}

    def _instantiate_rules(self) -> dict[str, Rule]:
        return {rule_id: cls() for rule_id, cls in self._rule_registry.items()}

    def _instantiate_outputs(self) -> dict[str, Output]:
        return {output_id: cls() for output_id, cls in self._output_registry.items()}

    def start(self) -> None:
        with self._lock:
            self.store.initialize()
            restored = self.store.load_runtime_state()
            self.acknowledgements = {
                rule_id: acknowledgement
                for rule_id, acknowledgement in restored.acknowledgements.items()
                if rule_id in self.rules
            }
            self.silences = restored.silences
            for source in self.sources.values():
                self._initialize_source(source)
            for output_id, output in self.outputs.items():
                self._initialize_output(output_id, output)

    def shutdown(self) -> None:
        with self._lock:
            for source in self.sources.values():
                self._terminate_source(source)
            for output_id, output in self.outputs.items():
                self._terminate_output(output_id, output)
            self.store.close()

    def evaluate_source(
        self,
        source_id: str,
        payload: Mapping[str, object] | SourceResult,
        *,
        now: datetime | None = None,
    ) -> dict[str, Alert]:
        with self._lock:
            current_time = now or self._now_fn()
            source_payload = self._normalize_source_input(source_id, payload, current_time)
            source_state = self._update_source_state(
                source_id,
                source_payload,
                observed_at=current_time,
            )
            for rule in self.rules.values():
                if rule.source != source_id:
                    continue
                if self._is_rule_excluded(rule.rule_id):
                    continue
                self._evaluate_rule(rule, source_payload, source_state, current_time)
            return dict(self.alerts)

    def reload(
        self,
        *,
        source_registry: dict[str, type[Source]] | None = None,
        rule_registry: dict[str, type[Rule]] | None = None,
        output_registry: dict[str, type[Output]] | None = None,
    ) -> None:
        with self._lock:
            old_rule_ids = set(self.rules)
            old_rules = self.rules
            old_sources = self.sources
            old_outputs = self.outputs
            old_source_ids = set(self.sources)
            old_output_ids = set(self.outputs)

            if source_registry is not None:
                self._source_registry = source_registry
                for source in old_sources.values():
                    self._terminate_source(source)
                self.sources = self._instantiate_sources()
                self.source_states = {
                    source_id: self.source_states.get(source_id, SourceState(source_id=source_id))
                    for source_id in self.sources
                }
                self._rebuild_plugin_states()
                for source in self.sources.values():
                    self._initialize_source(source)

            if rule_registry is not None:
                self._rule_registry = rule_registry
                self.rules = self._instantiate_rules()
                self._rebuild_plugin_states()

            if output_registry is not None:
                self._output_registry = output_registry
                for output_id, output in old_outputs.items():
                    self._terminate_output(output_id, output)
                self.outputs = self._instantiate_outputs()
                self._rebuild_plugin_states()
                for output_id, output in self.outputs.items():
                    self._initialize_output(output_id, output)

            removed_rule_ids = old_rule_ids - set(self.rules)
            now = self._now_fn()
            self.last_reload_at = now
            logger.info(
                "reload applied: sources=%d (%+d), rules=%d (%+d), outputs=%d (%+d), active_alerts=%d",
                len(self.sources),
                len(self.sources) - len(old_source_ids),
                len(self.rules),
                len(self.rules) - len(old_rule_ids),
                len(self.outputs),
                len(self.outputs) - len(old_output_ids),
                len(self.alerts),
            )
            for rule_id in removed_rule_ids:
                alert = self.alerts.get(rule_id)
                if alert is None:
                    continue
                self.store.record_rule_removed(
                    rule_id=rule_id,
                    definition_file=getattr(old_rules.get(rule_id, None).__class__, "__kanary_definition_file__", None)
                    if old_rules.get(rule_id, None) is not None else None,
                    previous_state=alert.state.value,
                    previous_severity=int(alert.severity),
                    operator="system",
                    reason="rule removed during reload",
                    created_at=now,
                    had_ack=rule_id in self.acknowledgements,
                    active_silence_ids=list(alert.active_silence_ids),
                )
                self.alerts.pop(rule_id, None)
                self.acknowledgements.pop(rule_id, None)
            self._sync_plugin_definition_files()

    def reload_rule_plugins(
        self,
        *,
        replacements: dict[str, type[Rule]],
        removed_rule_ids: set[str] | None = None,
    ) -> None:
        with self._lock:
            old_rules = dict(self.rules)
            removed_rule_ids = set(removed_rule_ids or ())
            now = self._now_fn()
            for rule_id in removed_rule_ids:
                if rule_id not in self.rules:
                    continue
                old_rule = old_rules.get(rule_id)
                alert = self.alerts.get(rule_id)
                if alert is not None:
                    self.store.record_rule_removed(
                        rule_id=rule_id,
                        definition_file=getattr(old_rule.__class__, "__kanary_definition_file__", None)
                        if old_rule is not None else None,
                        previous_state=alert.state.value,
                        previous_severity=int(alert.severity),
                        operator="system",
                        reason="rule removed during reload",
                        created_at=now,
                        had_ack=rule_id in self.acknowledgements,
                        active_silence_ids=list(alert.active_silence_ids),
                    )
                self.alerts.pop(rule_id, None)
                self.acknowledgements.pop(rule_id, None)
                self.rules.pop(rule_id, None)
                self._rule_registry.pop(rule_id, None)

            for rule_id, rule_cls in replacements.items():
                self._rule_registry[rule_id] = rule_cls
                self.rules[rule_id] = rule_cls()

            self._rebuild_plugin_states()
            self._sync_plugin_definition_files()

    def reload_output_plugins(
        self,
        *,
        replacements: dict[str, type[Output]],
        removed_output_ids: set[str] | None = None,
    ) -> None:
        with self._lock:
            removed_output_ids = set(removed_output_ids or ())
            for output_id in removed_output_ids:
                output = self.outputs.pop(output_id, None)
                self._output_registry.pop(output_id, None)
                if output is not None:
                    self._terminate_output(output_id, output)

            for output_id, output_cls in replacements.items():
                existing = self.outputs.get(output_id)
                if existing is not None:
                    self._terminate_output(output_id, existing)
                self._output_registry[output_id] = output_cls
                output = output_cls()
                self.outputs[output_id] = output
                self._initialize_output(output_id, output)

            self._rebuild_plugin_states()
            self._sync_plugin_definition_files()

    def reload_source_plugins(
        self,
        *,
        replacements: dict[str, type[Source]],
        removed_source_ids: set[str] | None = None,
    ) -> None:
        with self._lock:
            removed_source_ids = set(removed_source_ids or ())
            for source_id in removed_source_ids:
                source = self.sources.pop(source_id, None)
                self._source_registry.pop(source_id, None)
                self.source_states.pop(source_id, None)
                if source is not None:
                    self._terminate_source(source)

            for source_id, source_cls in replacements.items():
                existing = self.sources.get(source_id)
                if existing is not None:
                    self._terminate_source(existing)
                self._source_registry[source_id] = source_cls
                source = source_cls()
                self.sources[source_id] = source
                self.source_states.setdefault(source_id, SourceState(source_id=source_id))
                self._initialize_source(source)

            self._rebuild_plugin_states()
            self._sync_plugin_definition_files()

    def acknowledge(self, rule_id: str, *, operator: str, reason: str | None = None) -> Alert:
        with self._lock:
            rule = self.rules[rule_id]
            self._propagate_remote_ack(rule, operator=operator, reason=reason)
            now = self._now_fn()
            alert = self.alerts[rule_id]
            previous_state = alert.state
            acknowledgement = Acknowledgement(
                rule_id=rule_id,
                operator=operator,
                reason=reason,
                created_at=now,
            )
            self.acknowledgements[rule_id] = acknowledgement
            self.store.record_acknowledgement(acknowledgement)
            alert.state = AlertState.ACKED
            alert.acked_at = now
            alert.acked_by = operator
            alert.ack_reason = reason
            alert.last_evaluated_at = now
            self.store.append_alert_event(
                self._make_alert_event(
                    alert=alert,
                    occurred_at=now,
                    previous_state=previous_state,
                    previous_severity=alert.severity,
                    current_state=AlertState.ACKED,
                    current_severity=alert.severity,
                    transition=None,
                ),
                definition_file=getattr(rule.__class__, "__kanary_definition_file__", None),
                matched_outputs=list(getattr(rule, "matched_outputs", [])),
            )
            self._emit_alert_event(
                self._make_alert_event(
                    alert=alert,
                    occurred_at=now,
                    previous_state=previous_state,
                    previous_severity=alert.severity,
                    current_state=AlertState.ACKED,
                    current_severity=alert.severity,
                    transition=None,
                )
            )
            return alert

    def unacknowledge(self, rule_id: str, *, operator: str, reason: str | None = None) -> Alert:
        with self._lock:
            alert = self.alerts.get(rule_id)
            rule = self.rules[rule_id]
            if rule_id not in self.acknowledgements and (alert is None or alert.state != AlertState.ACKED):
                raise ValueError(f"rule '{rule_id}' is not acknowledged")
            self._propagate_remote_unack(rule, operator=operator, reason=reason)
            now = self._now_fn()
            alert = self.alerts[rule_id]
            previous_state = alert.state
            self.acknowledgements.pop(rule_id, None)
            self.store.record_unacknowledgement(
                rule_id=rule_id,
                operator=operator,
                reason=reason,
                created_at=now,
            )
            if alert.state == AlertState.ACKED:
                alert.state = AlertState.FIRING
                alert.acked_at = None
                alert.acked_by = None
                alert.ack_reason = None
                alert.last_evaluated_at = now
                self.store.append_alert_event(
                    self._make_alert_event(
                        alert=alert,
                        occurred_at=now,
                        previous_state=previous_state,
                        previous_severity=alert.severity,
                        current_state=AlertState.FIRING,
                        current_severity=alert.severity,
                        transition=UNACK,
                    ),
                    definition_file=getattr(rule.__class__, "__kanary_definition_file__", None),
                    matched_outputs=list(getattr(rule, "matched_outputs", [])),
                )
                self._emit_alert_event(
                    self._make_alert_event(
                        alert=alert,
                        occurred_at=now,
                        previous_state=previous_state,
                        previous_severity=alert.severity,
                        current_state=AlertState.FIRING,
                        current_severity=alert.severity,
                        transition=UNACK,
                    )
                )
            return alert

    def create_silence(
        self,
        *,
        operator: str,
        start_at: datetime,
        end_at: datetime,
        rule_patterns: list[str] | None = None,
        tags: list[str] | None = None,
        reason: str | None = None,
    ) -> Silence:
        with self._lock:
            if end_at <= start_at:
                raise ValueError("silence end_at must be later than start_at")
            if not (rule_patterns or tags):
                raise ValueError("silence requires at least one rule pattern or tag")
            silence = Silence(
                silence_id=uuid4().hex,
                created_by=operator,
                reason=reason,
                created_at=self._now_fn(),
                start_at=start_at,
                end_at=end_at,
                rule_patterns=tuple(rule_patterns or []),
                tags=tuple(tags or []),
                remote_silence_refs=self._propagate_remote_silence(
                    operator=operator,
                    reason=reason,
                    start_at=start_at,
                    end_at=end_at,
                    rule_patterns=tuple(rule_patterns or []),
                    tags=tuple(tags or []),
                ),
            )
            self.silences[silence.silence_id] = silence
            self.store.create_silence(silence)
            return silence

    def cancel_silence(self, silence_id: str, *, operator: str, reason: str | None = None) -> Silence:
        with self._lock:
            silence = self.silences[silence_id]
            self._cancel_remote_silence_refs(
                silence.remote_silence_refs,
                operator=operator,
                reason=reason,
            )
            silence.cancelled_at = self._now_fn()
            silence.cancelled_by = operator
            silence.cancel_reason = reason
            self.store.cancel_silence(silence)
            return silence

    def list_silences(self) -> list[Silence]:
        with self._lock:
            return list(self.silences.values())

    def silence_target_warnings(
        self,
        *,
        rule_patterns: list[str] | None = None,
        tags: list[str] | None = None,
    ) -> list[str]:
        patterns = list(rule_patterns or [])
        silence_tags = set(tags or [])
        if not patterns and not silence_tags:
            return []

        matched_rule_ids = [
            rule.rule_id
            for rule in self.rules.values()
            if any(fnmatch(rule.rule_id, pattern) for pattern in patterns)
            or bool(set(rule.tags).intersection(silence_tags))
        ]
        total_rules = len(self.rules)
        warnings: list[str] = []

        if any(pattern in {"*", "*.*"} for pattern in patterns):
            warnings.append("silence target uses a very broad wildcard pattern")
        if not matched_rule_ids:
            warnings.append("silence target matches no currently loaded rules")
        elif total_rules and len(matched_rule_ids) == total_rules:
            warnings.append("silence target matches all loaded rules")
        elif total_rules >= 6 and len(matched_rule_ids) * 2 >= total_rules:
            warnings.append("silence target matches many loaded rules")
        return warnings

    def get_rule_history(self, rule_id: str) -> dict[str, list[dict]]:
        with self._lock:
            rule = self.rules.get(rule_id)
            return self.store.get_rule_history(rule_id, list(getattr(rule, "tags", [])) if rule is not None else [])

    def test_poll(self, source_id: str) -> dict[str, object]:
        with self._lock:
            source = self.sources[source_id]
            result = source.poll({"engine": self, "now": self._now_fn()})
            normalized = self._normalize_source_result(result) if isinstance(result, SourceResult) else dict(result)
            return normalized

    def test_evaluate(
        self,
        rule_id: str,
        payload: Mapping[str, object] | SourceResult,
        *,
        now: datetime | None = None,
    ) -> dict[str, object]:
        with self._lock:
            rule = self.rules[rule_id]
            current_time = now or self._now_fn()
            normalized_payload = self._normalize_source_result(payload) if isinstance(payload, SourceResult) else dict(payload)
            existing_state = self.source_states.get(rule.source, SourceState(source_id=rule.source))
            source_state = SourceState(
                source_id=rule.source,
                current=SourceSnapshot(payload=dict(normalized_payload), observed_at=current_time),
                previous=existing_state.current,
                updated_at=current_time,
                poll_count=existing_state.poll_count,
            )
            alert = self._evaluate_rule_preview(rule, normalized_payload, source_state, current_time)
            event = AlertEvent(
                rule_id=rule.rule_id,
                previous_state=None,
                current_state=alert.state,
                previous_severity=None,
                current_severity=alert.severity,
                transition=None,
                alert=alert,
                occurred_at=current_time,
            )
            matched_outputs = list(getattr(rule, "matched_outputs", []))
            would_emit_outputs = [output_id for output_id, output in self.outputs.items() if output.matches(event)]
            return {
                "rule_id": rule.rule_id,
                "state": alert.state.value,
                "severity": int(alert.severity),
                "owner": alert.owner,
                "tags": list(alert.tags),
                "message": alert.message,
                "payload": alert.payload,
                "occurred_at": current_time,
                "matched_outputs": matched_outputs,
                "would_emit_outputs": would_emit_outputs,
            }

    def test_fire(
        self,
        rule_id: str,
        *,
        state: AlertState,
        message: str | None = None,
        reason: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, object]:
        with self._lock:
            rule = self.rules[rule_id]
            current_time = now or self._now_fn()
            previous_alert = self.alerts.get(rule_id)
            base_message = message or f"synthetic {state.value} notification test"
            synthetic_message = f"[TEST-FIRE] {base_message}"
            synthetic_payload: dict[str, object] = {
                "synthetic": True,
                "test_fire": {
                    "reason": reason,
                    "created_at": current_time.isoformat(),
                },
            }
            alert = Alert(
                rule_id=rule.rule_id,
                state=state,
                severity=rule.severity,
                owner=rule.owner,
                tags=tuple(rule.tags),
                payload=synthetic_payload,
                message=synthetic_message,
                last_evaluated_at=current_time,
            )
            event = AlertEvent(
                rule_id=rule.rule_id,
                previous_state=previous_alert.state if previous_alert is not None else None,
                current_state=state,
                previous_severity=previous_alert.severity if previous_alert is not None else None,
                current_severity=alert.severity,
                transition=None,
                alert=alert,
                occurred_at=current_time,
            )
            logger.info("test fire requested: rule=%s state=%s reason=%s", rule.rule_id, state.value, reason or "-")
            summary = self._emit_alert_event(event)
            summary["synthetic"] = True
            summary["message"] = synthetic_message
            summary["reason"] = reason
            return summary

    def peer_status(self) -> dict[str, object]:
        with self._lock:
            now = self._now_fn()
            alert_state_counts = {
                state.value: sum(1 for alert in self.alerts.values() if alert.state == state)
                for state in AlertState
            }
            failed_plugin_count = sum(
                1 for status in self.plugin_states.values() if status.state == "FAILED"
            )
            latest_activity_candidates = [
                alert.last_evaluated_at for alert in self.alerts.values() if alert.last_evaluated_at is not None
            ]
            latest_activity_candidates.extend(
                status.last_updated_at
                for status in self.plugin_states.values()
                if status.last_updated_at is not None
            )
            latest_activity_at = max(latest_activity_candidates) if latest_activity_candidates else None
            return {
                "status": "ok",
                "node_id": self.node_id,
                "generated_at": now,
                "started_at": self.started_at,
                "uptime_seconds": (now - self.started_at).total_seconds(),
                "last_reload_at": self.last_reload_at,
                "latest_activity_at": latest_activity_at,
                "counts": {
                    "sources": len(self.sources),
                    "rules": len(self.rules),
                    "outputs": len(self.outputs),
                    "alerts": len(self.alerts),
                    "failed_plugins": failed_plugin_count,
                    "dirty_plugins": sum(
                        1
                        for status in self.plugin_states.values()
                        if status.state in {"DISCOVERED", "DIRTY", "PENDING_REMOVE"}
                    ),
                    "untracked_files": len(getattr(self, "runtime_untracked_files", [])),
                },
                "alert_states": alert_state_counts,
                "reload": {
                    "untracked_files": list(getattr(self, "runtime_untracked_files", [])),
                },
            }

    def _poll_sources(self, now: datetime) -> dict[str, dict]:
        payloads: dict[str, dict] = {}
        for source_id, source in self.sources.items():
            payloads[source_id] = self._poll_source(source_id, source, now)
        return payloads

    def _poll_source(self, source_id: str, source: Source, now: datetime) -> dict[str, object]:
        status = self._plugin_status("source", source_id)
        try:
            result = source.poll({"engine": self, "now": now})
            payload = self._normalize_source_result(result)
            status.state = "READY"
            status.init_ok = True
            status.last_error = None
            status.last_error_detail = None
            status.run_count += 1
            status.last_run_at = now
            status.last_success_at = now
            status.last_updated_at = now
            return payload
        except Exception as exc:
            status.state = "FAILED"
            status.last_error = str(exc)
            status.last_error_detail = traceback.format_exc()
            status.run_count += 1
            status.last_run_at = now
            status.last_failure_at = now
            status.last_updated_at = now
            raise

    def _normalize_source_result(self, result: SourceResult) -> dict[str, object]:
        channels: dict[str, dict[str, object]] = {}
        for measurement in result.measurements:
            channels[measurement.name] = {
                "value": measurement.value,
                "timestamp": measurement.timestamp,
                "metadata": measurement.metadata,
            }

        payload: dict[str, object] = {
            "channels": channels,
            "status": result.status,
        }
        if result.error is not None:
            payload["error"] = result.error
        if result.metadata:
            payload["metadata"] = result.metadata
        return payload

    def _normalize_source_input(
        self,
        source_id: str,
        payload: Mapping[str, object] | SourceResult,
        now: datetime,
    ) -> dict[str, object]:
        status = self._plugin_status("source", source_id)
        normalized = self._normalize_source_result(payload) if isinstance(payload, SourceResult) else dict(payload)
        status.state = "READY"
        status.init_ok = True
        status.last_error = None
        status.last_error_detail = None
        status.run_count += 1
        status.last_run_at = now
        status.last_success_at = now
        status.last_updated_at = now
        return normalized

    def _update_source_state(
        self,
        source_id: str,
        payload: dict,
        *,
        observed_at: datetime,
    ) -> SourceState:
        state = self.source_states.setdefault(source_id, SourceState(source_id=source_id))
        state.previous = state.current
        state.current = SourceSnapshot(payload=dict(payload), observed_at=observed_at)
        state.updated_at = observed_at
        state.poll_count += 1
        return state

    def _apply_evaluation(
        self,
        rule: Rule,
        state: AlertState,
        payload: dict,
        message: str | None,
        severity,
        now: datetime,
    ) -> None:
        previous = self.alerts.get(rule.rule_id)
        active_since = previous.active_since if previous and previous.state != AlertState.OK else None
        acknowledgement = self.acknowledgements.get(rule.rule_id)
        acked_at = acknowledgement.created_at if acknowledgement and state == AlertState.ACKED else None
        acked_by = acknowledgement.operator if acknowledgement and state == AlertState.ACKED else None
        ack_reason = acknowledgement.reason if acknowledgement and state == AlertState.ACKED else None
        previous_state = previous.state if previous else None
        active_silence_ids = tuple(silence.silence_id for silence in self._matching_active_silences(rule, now))

        if state == AlertState.FIRING and active_since is None:
            active_since = now
        if state == AlertState.OK:
            active_since = None
            acked_at = None
            acked_by = None
            ack_reason = None
            self.acknowledgements.pop(rule.rule_id, None)

        alert = Alert(
            rule_id=rule.rule_id,
            state=state,
            severity=severity,
            owner=rule.owner,
            tags=tuple(rule.tags),
            payload=payload,
            message=message,
            active_since=active_since,
            last_evaluated_at=now,
            acked_at=acked_at,
            acked_by=acked_by,
            ack_reason=ack_reason,
            active_silence_ids=active_silence_ids,
        )
        self.alerts[rule.rule_id] = alert

        event = self._derive_alert_event(
            previous=previous,
            alert=alert,
            occurred_at=now,
        )

        if event is not None:
            self.store.append_alert_event(
                event,
                definition_file=getattr(rule.__class__, "__kanary_definition_file__", None),
                matched_outputs=list(getattr(rule, "matched_outputs", [])),
            )

        if event is not None and previous is not None:
            self._emit_alert_event(event)

    def _resolve_dependency_state(
        self,
        rule: Rule,
        payload: dict[str, object],
    ) -> Alert | None:
        suppressing_rules = [
            dependency_rule_id
            for dependency_rule_id in rule.suppressed_by
            if self._dependency_is_active(dependency_rule_id)
        ]
        if suppressing_rules:
            return Alert(
                rule_id=rule.rule_id,
                state=AlertState.SUPPRESSED,
                severity=rule.severity,
                owner=rule.owner,
                tags=tuple(rule.tags),
                payload=payload,
                message=f"suppressed by {', '.join(suppressing_rules)}",
            )

        blocking_rules = [
            dependency_rule_id
            for dependency_rule_id in rule.depends_on
            if self._dependency_is_active(dependency_rule_id)
        ]
        if blocking_rules:
            return Alert(
                rule_id=rule.rule_id,
                state=AlertState.OK,
                severity=rule.severity,
                owner=rule.owner,
                tags=tuple(rule.tags),
                payload=payload,
                message=f"blocked by {', '.join(blocking_rules)}",
            )
        return None

    def _resolve_operator_state(
        self,
        rule: Rule,
        state: AlertState,
        payload: dict[str, object],
        message: str | None,
        severity,
        now: datetime,
    ) -> Alert | None:
        active_silences = self._matching_active_silences(rule, now)
        if active_silences:
            details = ", ".join(silence.silence_id for silence in active_silences)
            return Alert(
                rule_id=rule.rule_id,
                state=AlertState.SILENCED,
                severity=severity,
                owner=rule.owner,
                tags=tuple(rule.tags),
                payload=payload,
                message=f"silenced by {details}",
                active_silence_ids=tuple(silence.silence_id for silence in active_silences),
            )

        if state == AlertState.FIRING and rule.rule_id in self.acknowledgements:
            acknowledgement = self.acknowledgements[rule.rule_id]
            return Alert(
                rule_id=rule.rule_id,
                state=AlertState.ACKED,
                severity=severity,
                owner=rule.owner,
                tags=tuple(rule.tags),
                payload=payload,
                message=message,
                acked_at=acknowledgement.created_at,
                acked_by=acknowledgement.operator,
                ack_reason=acknowledgement.reason,
            )
        return None

    def _matching_active_silences(self, rule: Rule, now: datetime) -> list[Silence]:
        return [
            silence
            for silence in self.silences.values()
            if self._silence_matches_rule(silence, rule, now)
        ]

    def _matching_rules_for_targets(
        self,
        *,
        rule_patterns: tuple[str, ...],
        tags: tuple[str, ...],
    ) -> list[Rule]:
        matched: list[Rule] = []
        tag_set = set(tags)
        for rule in self.rules.values():
            matches_rule = any(fnmatch(rule.rule_id, pattern) for pattern in rule_patterns)
            matches_tag = bool(set(rule.tags).intersection(tag_set))
            if matches_rule or matches_tag:
                matched.append(rule)
        return matched

    def _silence_matches_rule(self, silence: Silence, rule: Rule, now: datetime) -> bool:
        if silence.cancelled_at is not None:
            return False
        if not (silence.start_at <= now < silence.end_at):
            return False
        matches_rule = any(fnmatch(rule.rule_id, pattern) for pattern in silence.rule_patterns)
        matches_tag = bool(set(rule.tags).intersection(silence.tags))
        return matches_rule or matches_tag

    def _dependency_is_active(self, dependency_rule_id: str) -> bool:
        alert = self.alerts.get(dependency_rule_id)
        if alert is None:
            return False
        return alert.state != AlertState.OK

    def _emit_alert_event(self, event: AlertEvent) -> dict[str, object]:
        matched_output_ids: list[str] = []
        initialized_output_ids: list[str] = []
        delivered_output_ids: list[str] = []
        filtered_output_ids: list[str] = []
        uninitialized_output_ids: list[str] = []
        failed_output_ids: list[str] = []
        for output_id, output in self.outputs.items():
            if not output.matches(event):
                filtered_output_ids.append(output_id)
                logger.debug(
                    "output '%s' skipped for rule '%s': event does not match output filters",
                    output_id,
                    event.rule_id,
                )
                continue
            matched_output_ids.append(output_id)
            status = self._plugin_status("output", output_id)
            if not status.init_ok:
                uninitialized_output_ids.append(output_id)
                logger.warning(
                    "output '%s' skipped for rule '%s': output is not initialized",
                    output_id,
                    event.rule_id,
                )
                continue
            initialized_output_ids.append(output_id)
            try:
                output.emit(event, {"engine": self})
                delivered_output_ids.append(output_id)
                status.state = "READY"
                status.last_error = None
                status.last_error_detail = None
                status.run_count += 1
                status.last_run_at = event.occurred_at
                status.last_success_at = event.occurred_at
                status.last_updated_at = event.occurred_at
            except Exception as exc:
                failed_output_ids.append(output_id)
                status.state = "FAILED"
                status.last_error = str(exc)
                status.last_error_detail = traceback.format_exc()
                status.last_failure_at = event.occurred_at
                status.last_updated_at = event.occurred_at
                logger.exception("output '%s' failed", output.output_id)
        logger.info(
            "alert dispatch summary: rule=%s transition=%s->%s matched=%s delivered=%s filtered=%s uninitialized=%s failed=%s",
            event.rule_id,
            event.previous_state.value if event.previous_state is not None else "-",
            event.current_state.value,
            ",".join(matched_output_ids) or "-",
            ",".join(delivered_output_ids) or "-",
            ",".join(filtered_output_ids) or "-",
            ",".join(uninitialized_output_ids) or "-",
            ",".join(failed_output_ids) or "-",
        )
        self.store.append_output_dispatch(
            event=event,
            matched_outputs=matched_output_ids,
            delivered_outputs=delivered_output_ids,
            filtered_outputs=filtered_output_ids,
            uninitialized_outputs=uninitialized_output_ids,
            failed_outputs=failed_output_ids,
        )
        summary = {
            "rule_id": event.rule_id,
            "previous_state": event.previous_state.value if event.previous_state is not None else None,
            "current_state": event.current_state.value,
            "occurred_at": event.occurred_at,
            "matched_outputs": matched_output_ids,
            "delivered_outputs": delivered_output_ids,
            "filtered_outputs": filtered_output_ids,
            "uninitialized_outputs": uninitialized_output_ids,
            "failed_outputs": failed_output_ids,
        }
        if not matched_output_ids:
            logger.info(
                "alert event for rule '%s' (%s -> %s) had no matching outputs",
                event.rule_id,
                event.previous_state.value if event.previous_state is not None else "-",
                event.current_state.value,
            )
        elif not initialized_output_ids:
            logger.warning(
                "alert event for rule '%s' (%s -> %s) had matching outputs but none were initialized",
                event.rule_id,
                event.previous_state.value if event.previous_state is not None else "-",
                event.current_state.value,
            )
        return summary

    def _derive_alert_event(
        self,
        *,
        previous: Alert | None,
        alert: Alert,
        occurred_at: datetime,
    ) -> AlertEvent | None:
        previous_state = previous.state if previous is not None else None
        previous_severity = previous.severity if previous is not None else None
        current_state = alert.state
        current_severity = alert.severity

        if previous is None:
            return self._make_alert_event(
                alert=alert,
                occurred_at=occurred_at,
                previous_state=None,
                previous_severity=None,
                current_state=current_state,
                current_severity=current_severity,
                transition=None,
            )
        if previous_state != current_state:
            return self._make_alert_event(
                alert=alert,
                occurred_at=occurred_at,
                previous_state=previous_state,
                previous_severity=previous_severity,
                current_state=current_state,
                current_severity=current_severity,
                transition=None,
            )
        if previous_severity is not None and previous_severity < current_severity:
            return self._make_alert_event(
                alert=alert,
                occurred_at=occurred_at,
                previous_state=previous_state,
                previous_severity=previous_severity,
                current_state=current_state,
                current_severity=current_severity,
                transition=ESCALATED,
            )
        if previous_severity is not None and previous_severity > current_severity:
            return self._make_alert_event(
                alert=alert,
                occurred_at=occurred_at,
                previous_state=previous_state,
                previous_severity=previous_severity,
                current_state=current_state,
                current_severity=current_severity,
                transition=DEESCALATED,
            )
        return None

    def _make_alert_event(
        self,
        *,
        alert: Alert,
        occurred_at: datetime,
        previous_state: AlertState | None,
        previous_severity,
        current_state: AlertState,
        current_severity,
        transition: TransitionKind | None,
    ) -> AlertEvent:
        return AlertEvent(
            rule_id=alert.rule_id,
            previous_state=previous_state,
            current_state=current_state,
            previous_severity=previous_severity,
            current_severity=current_severity,
            transition=transition,
            alert=alert,
            occurred_at=occurred_at,
        )

    def _is_rule_excluded(self, rule_id: str) -> bool:
        return any(fnmatch(rule_id, pattern) for pattern in self._exclude_rule_patterns)

    def _propagate_remote_ack(self, rule: Rule, *, operator: str, reason: str | None) -> None:
        acknowledge_remote = getattr(rule, "acknowledge_remote", None)
        if callable(acknowledge_remote):
            acknowledge_remote(self, operator=operator, reason=reason)

    def _propagate_remote_unack(self, rule: Rule, *, operator: str, reason: str | None) -> None:
        unacknowledge_remote = getattr(rule, "unacknowledge_remote", None)
        if callable(unacknowledge_remote):
            unacknowledge_remote(self, operator=operator, reason=reason)

    def _propagate_remote_silence(
        self,
        *,
        operator: str,
        reason: str | None,
        start_at: datetime,
        end_at: datetime,
        rule_patterns: tuple[str, ...],
        tags: tuple[str, ...],
    ) -> tuple[str, ...]:
        remote_refs: list[str] = []
        seen_rule_ids: set[str] = set()
        for rule in self._matching_rules_for_targets(rule_patterns=rule_patterns, tags=tags):
            if rule.rule_id in seen_rule_ids:
                continue
            seen_rule_ids.add(rule.rule_id)
            create_remote_silence = getattr(rule, "create_remote_silence", None)
            if not callable(create_remote_silence):
                continue
            remote_silence_id = create_remote_silence(
                self,
                operator=operator,
                reason=reason,
                start_at=start_at.isoformat(),
                end_at=end_at.isoformat(),
            )
            if remote_silence_id:
                remote_refs.append(f"{rule.source}:{remote_silence_id}")
        return tuple(remote_refs)

    def _cancel_remote_silence_refs(
        self,
        remote_silence_refs: tuple[str, ...],
        *,
        operator: str,
        reason: str | None,
    ) -> None:
        for ref in remote_silence_refs:
            source_id, _, remote_silence_id = ref.partition(":")
            if not source_id or not remote_silence_id:
                continue
            source = self.sources.get(source_id)
            cancel_remote_silence = getattr(source, "cancel_remote_silence", None)
            if callable(cancel_remote_silence):
                cancel_remote_silence(remote_silence_id, operator=operator, reason=reason)

    def _initialize_output(self, output_id: str, output: Output) -> None:
        status = self._plugin_status("output", output_id)
        try:
            output.init({"engine": self})
            status.state = "READY"
            status.init_ok = True
            status.last_error = None
            status.last_error_detail = None
            status.last_success_at = self._now_fn()
            status.last_updated_at = status.last_success_at
        except Exception as exc:
            status.state = "FAILED"
            status.init_ok = False
            status.last_error = str(exc)
            status.last_error_detail = traceback.format_exc()
            status.last_failure_at = self._now_fn()
            status.last_updated_at = status.last_failure_at
            logger.exception("output '%s' init failed", output_id)

    def _terminate_output(self, output_id: str, output: Output) -> None:
        status = self._plugin_status("output", output_id)
        try:
            output.terminate({"engine": self})
        except Exception as exc:
            status.state = "FAILED"
            status.last_error = str(exc)
            status.last_error_detail = traceback.format_exc()
            status.last_failure_at = self._now_fn()
            status.last_updated_at = status.last_failure_at
            logger.exception("output '%s' terminate failed", output_id)

    def record_source_failure(self, source_id: str, error: str, *, now: datetime | None = None) -> None:
        when = now or self._now_fn()
        status = self._plugin_status("source", source_id)
        status.state = "FAILED"
        status.last_error = error
        status.last_error_detail = None
        status.run_count += 1
        status.last_run_at = when
        status.last_failure_at = when
        status.last_updated_at = when

    def _initialize_source(self, source: Source) -> None:
        status = self._plugin_status("source", source.source_id)
        try:
            source.init({"engine": self})
            status.state = "READY"
            status.init_ok = True
            status.last_error = None
            status.last_error_detail = None
            status.last_success_at = self._now_fn()
            status.last_updated_at = status.last_success_at
        except Exception as exc:
            status.state = "FAILED"
            status.init_ok = False
            status.last_error = str(exc)
            status.last_error_detail = traceback.format_exc()
            status.last_failure_at = self._now_fn()
            status.last_updated_at = status.last_failure_at
            raise

    def _terminate_source(self, source: Source) -> None:
        status = self._plugin_status("source", source.source_id)
        try:
            source.terminate({"engine": self})
        except Exception as exc:
            status.state = "FAILED"
            status.last_error = str(exc)
            status.last_error_detail = traceback.format_exc()
            status.last_failure_at = self._now_fn()
            status.last_updated_at = status.last_failure_at
            raise

    def _evaluate_rule(
        self,
        rule: Rule,
        source_payload: dict[str, object],
        source_state: SourceState,
        now: datetime,
    ) -> None:
        status = self._plugin_status("rule", rule.rule_id)
        try:
            dependency_state = self._resolve_dependency_state(rule, source_payload)
            if dependency_state is not None:
                self._apply_evaluation(
                    rule,
                    dependency_state.state,
                    dependency_state.payload,
                    dependency_state.message,
                    dependency_state.severity,
                    now,
                )
            else:
                evaluation = rule.normalize_evaluation(
                    rule.evaluate(
                        source_payload,
                        RuleContext(
                            now=now,
                            source_id=rule.source,
                            source_state=source_state,
                            previous_alert=self.alerts.get(rule.rule_id),
                        ),
                    ),
                    source_payload,
                )
                operator_state = self._resolve_operator_state(
                    rule,
                    evaluation.state,
                    evaluation.payload,
                    evaluation.message,
                    evaluation.severity or rule.severity,
                    now,
                )
                if operator_state is not None:
                    self._apply_evaluation(
                        rule,
                        operator_state.state,
                        operator_state.payload,
                        operator_state.message,
                        operator_state.severity,
                        now,
                    )
                    status.state = "READY"
                    status.init_ok = True
                    status.last_error = None
                    status.last_error_detail = None
                    status.run_count += 1
                    status.last_run_at = now
                    status.last_success_at = now
                    status.last_updated_at = now
                    return
                self._apply_evaluation(
                    rule,
                    evaluation.state,
                    evaluation.payload,
                    evaluation.message,
                    evaluation.severity or rule.severity,
                    now,
                )
            status.state = "READY"
            status.init_ok = True
            status.last_error = None
            status.last_error_detail = None
            status.run_count += 1
            status.last_run_at = now
            status.last_success_at = now
            status.last_updated_at = now
        except Exception as exc:
            status.state = "FAILED"
            status.init_ok = True
            status.last_error = str(exc)
            status.last_error_detail = traceback.format_exc()
            status.run_count += 1
            status.last_run_at = now
            status.last_failure_at = now
            status.last_updated_at = now
            logger.exception("rule '%s' failed", rule.rule_id)

    def _evaluate_rule_preview(
        self,
        rule: Rule,
        source_payload: dict[str, object],
        source_state: SourceState,
        now: datetime,
    ) -> Alert:
        dependency_state = self._resolve_dependency_state(rule, source_payload)
        if dependency_state is not None:
            return dependency_state

        evaluation = rule.normalize_evaluation(
            rule.evaluate(
                source_payload,
                RuleContext(
                    now=now,
                    source_id=rule.source,
                    source_state=source_state,
                    previous_alert=self.alerts.get(rule.rule_id),
                ),
            ),
            source_payload,
        )
        return self._resolve_operator_state(
            rule,
            evaluation.state,
            evaluation.payload,
            evaluation.message,
            evaluation.severity or rule.severity,
            now,
        ) or Alert(
            rule_id=rule.rule_id,
            state=evaluation.state,
            severity=evaluation.severity or rule.severity,
            owner=rule.owner,
            tags=tuple(rule.tags),
            payload=evaluation.payload,
            message=evaluation.message,
        )

    def _plugin_key(self, plugin_type: str, plugin_id: str) -> str:
        return f"{plugin_type}:{plugin_id}"

    def _plugin_status(self, plugin_type: str, plugin_id: str) -> PluginStatus:
        key = self._plugin_key(plugin_type, plugin_id)
        return self.plugin_states.setdefault(key, PluginStatus(plugin_type, plugin_id))

    def _rebuild_plugin_states(self) -> None:
        next_states: dict[str, PluginStatus] = {}
        for source_id in self.sources:
            key = self._plugin_key("source", source_id)
            next_states[key] = self.plugin_states.get(key, PluginStatus("source", source_id))
        for rule_id in self.rules:
            key = self._plugin_key("rule", rule_id)
            next_states[key] = self.plugin_states.get(key, PluginStatus("rule", rule_id))
        for output_id in self.outputs:
            key = self._plugin_key("output", output_id)
            next_states[key] = self.plugin_states.get(key, PluginStatus("output", output_id))
        self.plugin_states = next_states
        self._sync_plugin_definition_files()

    def _sync_plugin_definition_files(self) -> None:
        for source_id, source in self.sources.items():
            status = self._plugin_status("source", source_id)
            status.loaded = True
            status.definition_file = getattr(source.__class__, "__kanary_definition_file__", None)
        for rule_id, rule in self.rules.items():
            status = self._plugin_status("rule", rule_id)
            status.loaded = True
            status.definition_file = getattr(rule.__class__, "__kanary_definition_file__", None)
        for output_id, output in self.outputs.items():
            status = self._plugin_status("output", output_id)
            status.loaded = True
            status.definition_file = getattr(output.__class__, "__kanary_definition_file__", None)
