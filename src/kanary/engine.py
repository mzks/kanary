from collections.abc import Mapping
from concurrent.futures import Future, TimeoutError as FutureTimeoutError
import dataclasses
from dataclasses import dataclass
from datetime import datetime, timezone
from fnmatch import fnmatch
from functools import partial
import logging
import socket
import time
import traceback
from uuid import uuid4
import threading
from typing import Callable

from .helpers.buffer import discarding_changes
from .dispatch import SHUTDOWN_DRAIN_SECONDS, OutputWorker, current_worker
from .constants import AlertState, DEESCALATED, ESCALATED, Severity, TransitionKind, UNACK
from .models import Acknowledgement, Alert, AlertEvent, Evaluation, PluginStatus, Silence, SourceResult, SourceSnapshot, SourceState
from .output import Output
from .models import normalize_source_output
from .registry import (
    get_output_registry,
    get_rule_registry,
    get_source_registry,
    prepare_output_class,
    prepare_rule_class,
    prepare_source_class,
)
from .selectors import normalize_rule_inputs, resolve_rule_sources
from .rule import Rule, RuleContext
from .signature_compat import invoke_compat
from .store import NullStore
from .source import Source

logger = logging.getLogger("kanary.engine")

# A rule's evaluate() runs while every other evaluation waits; slower than this is reported.
SLOW_EVALUATE_SECONDS = 0.1
# How long a test fire waits for the outputs to send before answering "still queued".
TEST_FIRE_WAIT_SECONDS = 30.0
# How long a reload waits for a new output's init() (which runs after the send in progress).
OUTPUT_INIT_WAIT_SECONDS = 60.0


def _retry_delay(attempt: int) -> float:
    """Seconds to wait before retry number attempt of a failed emit() (1, 4, 9, ...)."""
    return float(attempt ** 2)


def _plain_sleep(seconds: float) -> bool:
    time.sleep(seconds)
    return False


def _wait_for(futures: list[Future], timeout: float | None) -> None:
    deadline = None if timeout is None else time.monotonic() + timeout
    for future in futures:
        try:
            future.result(None if deadline is None else max(0.0, deadline - time.monotonic()))
        except FutureTimeoutError:
            logger.warning("an output init() is still running after %.0f s; its status updates when it ends", timeout)
        except Exception:
            logger.exception("output init job failed")


class _Dispatch:
    """Where one alert event went; recorded once every output has finished with it."""

    def __init__(
        self,
        engine: "Engine",
        event: AlertEvent,
        *,
        matched: list[str],
        filtered: list[str],
        uninitialized: list[str],
        emit_skipped: list[str],
        pending: list[str],
    ) -> None:
        self._engine = engine
        self.event = event
        self.matched = matched
        self.filtered = filtered
        self.uninitialized = uninitialized
        self.emit_skipped = emit_skipped
        self.delivered: list[str] = []
        self.failed: list[str] = []
        self._pending = set(pending)
        self._lock = threading.Lock()
        self._started = False
        self._finished = False
        self.done = threading.Event()

    def start(self) -> None:
        with self._lock:
            self._started = True
            finish = not self._pending and not self._finished
            self._finished = self._finished or finish
        if finish:
            self._finish()

    def report(self, output_id: str, delivered: bool, reason: str | None = None) -> None:
        with self._lock:
            if output_id not in self._pending:
                return
            self._pending.discard(output_id)
            (self.delivered if delivered else self.failed).append(output_id)
            finish = self._started and not self._pending and not self._finished
            self._finished = self._finished or finish
        if not delivered and reason:
            logger.warning("output '%s' did not send %s %s: %s", output_id, self.event.rule_id, self.event.current_state.value, reason)
        if finish:
            self._finish()

    def _finish(self) -> None:
        self._engine._record_dispatch(self)
        self.done.set()

    def summary(self) -> dict[str, object]:
        with self._lock:
            pending = sorted(self._pending)
            delivered = list(self.delivered)
            failed = list(self.failed)
        event = self.event
        return {
            "rule_id": event.rule_id,
            "previous_state": event.previous_state.value if event.previous_state is not None else None,
            "current_state": event.current_state.value,
            "occurred_at": event.occurred_at,
            "matched_outputs": list(self.matched),
            "delivered_outputs": delivered,
            "emit_skipped_outputs": list(self.emit_skipped),
            "filtered_outputs": list(self.filtered),
            "uninitialized_outputs": list(self.uninitialized),
            "failed_outputs": failed,
            "pending_outputs": pending,
        }


@dataclass(slots=True)
class _RuleEvaluation:
    state: AlertState
    payload: dict[str, object]
    message: str | None
    severity: Severity
    evaluated_at: datetime
    active_since: datetime | None


def _coerce_test_timestamp(value: object) -> object:
    if isinstance(value, str):
        return datetime.fromisoformat(value)
    return value


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
        output_emit_enabled: bool = True,
        silence_changed_callback: Callable[[], None] | None = None,
    ) -> None:
        self._source_registry = source_registry or get_source_registry()
        self._rule_registry = rule_registry or get_rule_registry()
        self._output_registry = output_registry or get_output_registry()
        self._exclude_rule_patterns = exclude_rule_patterns or []
        self._now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self.node_id = node_id or socket.gethostname()
        self.output_emit_enabled = output_emit_enabled
        self._silence_changed_callback = silence_changed_callback
        self.started_at = self._now_fn()
        self.last_reload_at: datetime | None = None
        self.store = store or NullStore()
        self._lock = threading.RLock()
        self.sources = self._instantiate_sources()
        self.rules = self._instantiate_rules()
        self.outputs = self._instantiate_outputs()
        self._output_workers: dict[str, OutputWorker] = {}
        self.plugin_states: dict[str, PluginStatus] = {}
        self.alerts: dict[str, Alert] = {}
        self._restored_rule_states: dict[str, tuple[AlertState, Severity]] = {}
        self._rule_evaluations: dict[str, _RuleEvaluation] = {}
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
        return {
            source_id: prepare_source_class(cls)()
            for source_id, cls in self._source_registry.items()
        }

    def _instantiate_rules(self) -> dict[str, Rule]:
        rules: dict[str, Rule] = {}
        for rule_id, cls in self._rule_registry.items():
            rule = prepare_rule_class(cls)()
            self._configure_rule(rule)
            rules[rule_id] = rule
        return rules

    def _instantiate_outputs(self) -> dict[str, Output]:
        return {
            output_id: prepare_output_class(cls)()
            for output_id, cls in self._output_registry.items()
        }

    def _configure_rule(self, rule: Rule) -> None:
        rule.inputs = normalize_rule_inputs(
            getattr(rule, "inputs", None),
            source=getattr(rule, "source", None),
        )
        rule.resolved_sources = resolve_rule_sources(rule.inputs, self.sources.keys())

    def _source_runtime_ctx(self, now: datetime | None = None) -> dict[str, object]:
        ctx: dict[str, object] = {"engine": self}
        if now is not None:
            ctx["now"] = now
        return ctx

    def _call_source_init(self, source: Source) -> None:
        source._kanary_engine = self
        invoke_compat(
            source.init,
            style=getattr(source.__class__, "__kanary_init_style__", "new"),
            new_args=(),
            legacy_args=(self._source_runtime_ctx(),),
        )

    def _call_source_poll(self, source: Source, *, now: datetime) -> object:
        source._kanary_engine = self
        source._kanary_poll_now = now
        return invoke_compat(
            source.poll,
            style=getattr(source.__class__, "__kanary_poll_style__", "new"),
            new_args=(),
            legacy_args=(self._source_runtime_ctx(now),),
        )

    def _call_source_test_poll(self, source: Source, *, now: datetime) -> object:
        """What poll() would return now, without changing the source (Source.test_poll).

        What it adds to a kanary.Buffer is dropped when it returns.
        """
        with discarding_changes():
            test_poll = getattr(type(source), "test_poll", None)
            if test_poll is None or test_poll is Source.test_poll:
                return self._call_source_poll(source, now=now)  # the default is poll() itself
            source._kanary_engine = self
            source._kanary_poll_now = now
            return source.test_poll()

    def _call_source_terminate(self, source: Source) -> None:
        source._kanary_engine = self
        invoke_compat(
            source.terminate,
            style=getattr(source.__class__, "__kanary_terminate_style__", "new"),
            new_args=(),
            legacy_args=(self._source_runtime_ctx(),),
        )

    def _call_rule_evaluate(
        self,
        rule: Rule,
        source_payload: dict[str, object],
        ctx: RuleContext,
    ) -> object:
        started = time.perf_counter()
        try:
            return invoke_compat(
                rule.evaluate,
                style=getattr(rule.__class__, "__kanary_evaluate_style__", "new"),
                new_args=(ctx,),
                legacy_args=(source_payload, ctx),
            )
        finally:
            self._note_rule_duration(rule.rule_id, time.perf_counter() - started)

    def _note_rule_duration(self, rule_id: str, seconds: float) -> None:
        """Warn while a rule's evaluate() is slow: every other evaluation waits for it."""
        status = self._plugin_status("rule", rule_id)
        if seconds >= SLOW_EVALUATE_SECONDS:
            if status.warning is None:
                logger.warning(
                    "rule '%s' evaluate() took %.0f ms; evaluations run one at a time, so keep it fast and move I/O into a source",
                    rule_id,
                    seconds * 1000,
                )
            status.warning = f"evaluate() took {seconds * 1000:.0f} ms (over {SLOW_EVALUATE_SECONDS * 1000:.0f} ms)"
        elif status.warning is not None:
            logger.info("rule '%s' evaluate() is fast again (%.0f ms)", rule_id, seconds * 1000)
            status.warning = None

    def _output_runtime_ctx(self) -> dict[str, object]:
        return {"engine": self}

    def _call_output_init(self, output: Output) -> None:
        output._kanary_engine = self
        invoke_compat(
            output.init,
            style=getattr(output.__class__, "__kanary_init_style__", "new"),
            new_args=(),
            legacy_args=(self._output_runtime_ctx(),),
        )

    def _call_output_emit(self, output: Output, event: AlertEvent) -> None:
        output._kanary_engine = self
        invoke_compat(
            output.emit,
            style=getattr(output.__class__, "__kanary_emit_style__", "new"),
            new_args=(event,),
            legacy_args=(event, self._output_runtime_ctx()),
        )

    def _call_output_terminate(self, output: Output) -> None:
        output._kanary_engine = self
        invoke_compat(
            output.terminate,
            style=getattr(output.__class__, "__kanary_terminate_style__", "new"),
            new_args=(),
            legacy_args=(self._output_runtime_ctx(),),
        )

    def _refresh_rule_resolutions(self) -> None:
        for rule in self.rules.values():
            self._configure_rule(rule)

    def _refresh_rule_plugin_resolution_status(self) -> None:
        now = self._now_fn()
        for rule_id, rule in self.rules.items():
            status = self._plugin_status("rule", rule_id)
            if getattr(rule, "resolved_sources", []):
                if status.state == "FAILED" and status.last_error == "rule resolved zero sources or inputs":
                    self._set_plugin_ready(status)
                    status.last_error = None
                    status.last_error_detail = None
                    status.last_updated_at = now
                continue
            self._set_plugin_failed(status)
            status.init_ok = True
            status.last_error = "rule resolved zero sources or inputs"
            status.last_error_detail = None
            status.last_failure_at = now
            status.last_updated_at = now

    def start(self) -> None:
        with self._lock:
            if not self.output_emit_enabled:
                logger.warning(
                    "output emit is disabled; Output plugins will be initialized and routed but emit() will not be called"
                )
            self.store.initialize()
            restored = self.store.load_runtime_state()
            self.acknowledgements = {
                rule_id: acknowledgement
                for rule_id, acknowledgement in restored.acknowledgements.items()
                if rule_id in self.rules
            }
            self.silences = restored.silences
            self._restored_rule_states = {
                rule_id: state
                for rule_id, state in restored.latest_rule_states.items()
                if rule_id in self.rules
            }
            for source in self.sources.values():
                try:
                    self._initialize_source(source)
                except Exception:
                    logger.exception("source '%s' init failed during startup", source.source_id)
            output_inits = [self._start_output_worker(output_id, output) for output_id, output in self.outputs.items()]
            self._refresh_rule_plugin_resolution_status()
        _wait_for(output_inits, None)

    def shutdown(self) -> None:
        """Terminate sources, send what the outputs still have queued (briefly), then close the store."""
        with self._lock:
            for source in self.sources.values():
                try:
                    self._terminate_source(source)
                except Exception:
                    logger.exception("source '%s' terminate failed during shutdown", source.source_id)
            workers = self._output_workers
            self._output_workers = {}
        self._close_output_workers(workers)
        with self._lock:
            self.store.close()

    # Output threads (dispatch.OutputWorker): one per output; its init(), emit(),
    # followup callbacks and terminate() all run there, one at a time.

    def _start_output_worker(self, output_id: str, output: Output) -> Future:
        worker = OutputWorker(output_id, output, on_error=partial(self._record_output_error, output_id))
        self._output_workers[output_id] = worker
        return worker.post(partial(self._initialize_output, output_id))

    def _install_output(self, output_id: str, output: Output) -> Future:
        """Use a new output instance; events already queued are sent by it."""
        self.outputs[output_id] = output
        worker = self._output_workers.get(output_id)
        if worker is None:
            return self._start_output_worker(output_id, output)
        return worker.post(partial(self._swap_output, output_id, worker, output))

    def _swap_output(self, output_id: str, worker: OutputWorker, new_output: Output, old_output: Output) -> None:
        if old_output is not new_output:
            self._terminate_output(output_id, old_output)
        worker.output = new_output
        self._initialize_output(output_id, new_output)

    def _close_output_workers(
        self,
        workers: dict[str, OutputWorker],
        drain_seconds: float = SHUTDOWN_DRAIN_SECONDS,
    ) -> None:
        for output_id, worker in workers.items():
            worker.close(drain_seconds=drain_seconds, finalize=partial(self._terminate_output, output_id))
        deadline = time.monotonic() + drain_seconds + 2.0
        for output_id, worker in workers.items():
            if not worker.join(max(0.0, deadline - time.monotonic())):
                logger.warning("output '%s' was still sending when it had to stop", output_id)

    def flush_outputs(self, timeout: float = 10.0) -> bool:
        """Wait until every output has sent what is queued; False on timeout."""
        deadline = time.monotonic() + timeout
        for worker in list(self._output_workers.values()):
            if not worker.flush(max(0.0, deadline - time.monotonic())):
                return False
        return True

    def output_backlog(self, output_id: str) -> dict[str, int]:
        """Events waiting for (or being sent by) an output, and how many it dropped."""
        worker = self._output_workers.get(output_id)
        if worker is None:
            return {"queued": 0, "dropped": 0}
        return {"queued": worker.backlog, "dropped": worker.dropped}

    def evaluate_source(
        self,
        source_id: str,
        payload: Mapping[str, object] | SourceResult,
        *,
        now: datetime | None = None,
    ) -> dict[str, Alert]:
        with self._lock:
            current_time = now or self._now_fn()
            normalized_payload = self._normalize_source_input(source_id, payload, current_time)
            source_payload, source_state = self._apply_source_result(
                source_id,
                normalized_payload,
                observed_at=current_time,
            )
            if source_payload is None or source_state is None:
                return dict(self.alerts)
            for rule in self.rules.values():
                if source_id not in getattr(rule, "resolved_sources", []):
                    continue
                if self._is_rule_excluded(rule.rule_id):
                    continue
                self._evaluate_rule(rule, source_payload, source_state, current_time)
            return dict(self.alerts)

    def evaluate_rule_timer(
        self,
        rule_id: str,
        *,
        now: datetime | None = None,
    ) -> dict[str, Alert]:
        """Evaluate one rule from saved source snapshots without polling a Source."""
        with self._lock:
            rule = self.rules.get(rule_id)
            if rule is None or self._is_rule_excluded(rule_id):
                return dict(self.alerts)
            current_time = now or self._now_fn()
            resolved_sources = tuple(getattr(rule, "resolved_sources", []))
            if not resolved_sources:
                return dict(self.alerts)
            if getattr(rule.__class__, "__kanary_evaluate_style__", "new") == "legacy":
                if len(resolved_sources) != 1:
                    raise ValueError(
                        f"timer evaluation for legacy rule '{rule_id}' requires exactly one source"
                    )
                source_payload = dict(self.source_states[resolved_sources[0]].current.payload)
            else:
                source_payload = (
                    dict(self.source_states[resolved_sources[0]].current.payload)
                    if len(resolved_sources) == 1
                    else {}
                )
            self._evaluate_rule_from_snapshots(rule, source_payload, current_time)
            return dict(self.alerts)

    def reload(
        self,
        *,
        source_registry: dict[str, type[Source]] | None = None,
        rule_registry: dict[str, type[Rule]] | None = None,
        output_registry: dict[str, type[Output]] | None = None,
    ) -> None:
        closing_outputs: dict[str, OutputWorker] = {}
        output_inits: list[Future] = []
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
                self._refresh_rule_resolutions()
                self._rebuild_plugin_states()
                for source in self.sources.values():
                    self._initialize_source(source)

            if rule_registry is not None:
                self._rule_registry = rule_registry
                self.rules = self._instantiate_rules()
                self._rule_evaluations.clear()
                self._rebuild_plugin_states()

            if output_registry is not None:
                self._output_registry = output_registry
                new_outputs = self._instantiate_outputs()
                for output_id in set(old_outputs) - set(new_outputs):
                    worker = self._output_workers.pop(output_id, None)
                    if worker is not None:
                        closing_outputs[output_id] = worker
                self.outputs = {}
                for output_id, output in new_outputs.items():
                    output_inits.append(self._install_output(output_id, output))
                self._rebuild_plugin_states()

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
                self._rule_evaluations.pop(rule_id, None)
            self._refresh_rule_resolutions()
            self._sync_plugin_definition_files()
            self._refresh_rule_plugin_resolution_status()
        # Waiting for output threads happens without the lock: they may be sending.
        self._close_output_workers(closing_outputs)
        _wait_for(output_inits, OUTPUT_INIT_WAIT_SECONDS)

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
                self._rule_evaluations.pop(rule_id, None)
                self.rules.pop(rule_id, None)
                self._rule_registry.pop(rule_id, None)

            for rule_id, rule_cls in replacements.items():
                self._rule_registry[rule_id] = rule_cls
                self._rule_evaluations.pop(rule_id, None)
                rule = rule_cls()
                self._configure_rule(rule)
                self.rules[rule_id] = rule

            self._rebuild_plugin_states()
            self._sync_plugin_definition_files()
            self._refresh_rule_plugin_resolution_status()

    def reload_output_plugins(
        self,
        *,
        replacements: dict[str, type[Output]],
        removed_output_ids: set[str] | None = None,
    ) -> None:
        closing: dict[str, OutputWorker] = {}
        inits: list[Future] = []
        with self._lock:
            removed_output_ids = set(removed_output_ids or ())
            for output_id in removed_output_ids:
                self.outputs.pop(output_id, None)
                self._output_registry.pop(output_id, None)
                worker = self._output_workers.pop(output_id, None)
                if worker is not None:
                    closing[output_id] = worker

            for output_id, output_cls in replacements.items():
                self._output_registry[output_id] = output_cls
                # On the output's thread: terminate the old instance, init the new one;
                # events still queued are then sent by the new one.
                inits.append(self._install_output(output_id, output_cls()))

            self._rebuild_plugin_states()
            self._sync_plugin_definition_files()
        self._close_output_workers(closing)
        _wait_for(inits, OUTPUT_INIT_WAIT_SECONDS)

    def reload_source_plugins(
        self,
        *,
        replacements: dict[str, type[Source]],
        removed_source_ids: set[str] | None = None,
    ) -> None:
        stopping: list[Source] = []
        starting: list[Source] = []
        with self._lock:
            removed_source_ids = set(removed_source_ids or ())
            for source_id in removed_source_ids:
                source = self.sources.pop(source_id, None)
                self._source_registry.pop(source_id, None)
                self.source_states.pop(source_id, None)
                if source is not None:
                    stopping.append(source)

            for source_id, source_cls in replacements.items():
                existing = self.sources.get(source_id)
                if existing is not None:
                    stopping.append(existing)
                self._source_registry[source_id] = source_cls
                source = source_cls()
                self.sources[source_id] = source
                self.source_states.setdefault(source_id, SourceState(source_id=source_id))
                starting.append(source)

            self._refresh_rule_resolutions()
            self._rebuild_plugin_states()
            self._sync_plugin_definition_files()
            self._refresh_rule_plugin_resolution_status()
        # The runtime has stopped these sources' threads. terminate() and init() may
        # connect somewhere, so they run without the lock.
        for source in stopping:
            self._terminate_source(source)
        for source in starting:
            self._initialize_source(source)

    def acknowledge(self, rule_id: str, *, operator: str, reason: str | None = None) -> Alert:
        with self._lock:
            rule = self.rules[rule_id]
        # An imported (remote) alarm is acknowledged at its peer first, over the network.
        self._propagate_remote_ack(rule, operator=operator, reason=reason)
        with self._lock:
            rule = self.rules[rule_id]
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
        with self._lock:
            rule = self.rules[rule_id]
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
        if end_at <= start_at:
            raise ValueError("silence end_at must be later than start_at")
        if not (rule_patterns or tags):
            raise ValueError("silence requires at least one rule pattern or tag")
        # Imported (remote) alarms are silenced at their peers first, over the network.
        remote_silence_refs = self._propagate_remote_silence(
            operator=operator,
            reason=reason,
            start_at=start_at,
            end_at=end_at,
            rule_patterns=tuple(rule_patterns or []),
            tags=tuple(tags or []),
        )
        with self._lock:
            silence = Silence(
                silence_id=uuid4().hex,
                created_by=operator,
                reason=reason,
                created_at=self._now_fn(),
                start_at=start_at,
                end_at=end_at,
                rule_patterns=tuple(rule_patterns or []),
                tags=tuple(tags or []),
                remote_silence_refs=remote_silence_refs,
            )
            self.silences[silence.silence_id] = silence
            self.store.create_silence(silence)
            self.reconcile_silences(now=silence.created_at)
            self._notify_silence_changed()
            return silence

    def cancel_silence(self, silence_id: str, *, operator: str, reason: str | None = None) -> Silence:
        with self._lock:
            silence = self.silences[silence_id]
        self._cancel_remote_silence_refs(
            silence.remote_silence_refs,
            operator=operator,
            reason=reason,
        )
        with self._lock:
            silence.cancelled_at = self._now_fn()
            silence.cancelled_by = operator
            silence.cancel_reason = reason
            self.store.cancel_silence(silence)
            self.reconcile_silences(now=silence.cancelled_at)
            self._notify_silence_changed()
            return silence

    def next_silence_boundary(self, *, now: datetime | None = None) -> datetime | None:
        with self._lock:
            current_time = now or self._now_fn()
            boundaries = [
                boundary
                for silence in self.silences.values()
                if silence.cancelled_at is None
                for boundary in (silence.start_at, silence.end_at)
                if boundary > current_time
            ]
            return min(boundaries) if boundaries else None

    def reconcile_silences(self, *, now: datetime | None = None) -> None:
        """Apply silence boundaries using the last completed Rule evaluations."""
        with self._lock:
            current_time = now or self._now_fn()
            for rule in self._rules_in_dependency_order():
                evaluation = self._rule_evaluations.get(rule.rule_id)
                if evaluation is None:
                    continue
                self._apply_effective_evaluation(rule, evaluation, current_time)

    def _notify_silence_changed(self) -> None:
        if self._silence_changed_callback is not None:
            self._silence_changed_callback()

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

    def get_alert_history(
        self,
        *,
        limit: int = 100,
        after_alert_id: int | None = None,
        after_action_id: int | None = None,
    ) -> dict[str, object]:
        # History is an independent SQLite snapshot; do not stall source evaluation
        # behind the engine-wide state lock while the viewer reads it.
        return self.store.get_alert_history(
            limit=limit,
            after_alert_id=after_alert_id,
            after_action_id=after_action_id,
        )

    def test_poll(self, source_id: str) -> dict[str, object]:
        """Call the source's test_poll() here and return the result, not evaluated.

        Only for an engine without a runtime; the runtime runs it on the source's
        own thread instead (EngineRuntime.test_poll), so it never overlaps poll().
        """
        with self._lock:
            source = self.sources[source_id]
        current_time = self._now_fn()
        result = self._call_source_test_poll(source, now=current_time)
        return self._normalize_source_result(result, now=current_time)

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
            preview_states = self._build_test_evaluate_source_states(rule, payload, current_time)
            trigger_source_id = next(iter(getattr(rule, "resolved_sources", []) or [getattr(rule, "source", "")]), "")
            source_state = preview_states.get(trigger_source_id, SourceState(source_id=trigger_source_id))
            original_source_states = self.source_states
            self.source_states = preview_states
            try:
                alert = self._evaluate_rule_preview(rule, source_state.current.payload, source_state, current_time)
            finally:
                self.source_states = original_source_states
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
                "inputs": list(getattr(rule, "inputs", [])),
                "resolved_sources": list(getattr(rule, "resolved_sources", [])),
                "matched_outputs": matched_outputs,
                "would_emit_outputs": would_emit_outputs,
            }

    def test_evaluate_template(self, rule_id: str) -> dict[str, object]:
        with self._lock:
            rule = self.rules[rule_id]
            now = self._now_fn()
            ctx = RuleContext(
                now=now,
                source_states=self.source_states,
                declared_inputs=tuple(getattr(rule, "inputs", [])),
                resolved_sources=tuple(getattr(rule, "resolved_sources", [])),
            )

            template_inputs: dict[str, dict[str, object]] = {}
            unresolved_selectors: list[str] = []
            for selector in getattr(rule, "inputs", []):
                matched_names = ctx.names(selector)
                if matched_names:
                    for name in matched_names:
                        template_inputs.setdefault(
                            name,
                            {
                                "value": None,
                                "timestamp": now.isoformat(),
                            },
                        )
                    continue
                if ":" in selector and "*" not in selector:
                    template_inputs.setdefault(
                        selector,
                        {
                            "value": None,
                            "timestamp": now.isoformat(),
                        },
                    )
                    continue
                unresolved_selectors.append(selector)

            result: dict[str, object] = {
                "rule_id": rule.rule_id,
                "inputs": list(getattr(rule, "inputs", [])),
                "resolved_sources": list(getattr(rule, "resolved_sources", [])),
                "payload": {
                    "inputs": template_inputs,
                    "status": "ok",
                },
            }
            if len(template_inputs) == 1:
                result["single_input_shorthand"] = {
                    "value": None,
                    "timestamp": now.isoformat(),
                    "prev_value": None,
                    "prev_timestamp": now.isoformat(),
                }
            if unresolved_selectors:
                result["unresolved_selectors"] = unresolved_selectors
            return result

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
            dispatch = self._emit_alert_event(event, first=True)
        dispatch.done.wait(TEST_FIRE_WAIT_SECONDS)
        summary = dispatch.summary()
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

    def _normalize_source_result(self, result: object, *, now: datetime | None = None) -> dict[str, object]:
        normalized_result = normalize_source_output(result, now=now)
        if normalized_result.status not in {"ok", "empty", "no_update", "skip"}:
            raise ValueError(
                "source result status must be one of 'ok', 'empty', 'no_update', or 'skip'"
            )
        channels: dict[str, dict[str, object]] = {}
        for measurement in normalized_result.measurements:
            channels[measurement.name] = {
                "value": measurement.value,
                "timestamp": measurement.timestamp,
                "metadata": measurement.metadata,
            }

        payload: dict[str, object] = {
            "channels": channels,
            "status": normalized_result.status,
        }
        if normalized_result.error is not None:
            payload["error"] = normalized_result.error
        if normalized_result.reason is not None:
            payload["reason"] = normalized_result.reason
        if normalized_result.metadata:
            payload["metadata"] = normalized_result.metadata
        return payload

    def _normalize_source_input(
        self,
        source_id: str,
        payload: Mapping[str, object] | SourceResult,
        now: datetime,
    ) -> dict[str, object]:
        status = self._plugin_status("source", source_id)
        normalized = self._normalize_source_result(payload, now=now)
        self._set_plugin_ready(status)
        status.init_ok = True
        status.last_error = None
        status.last_error_detail = None
        status.run_count += 1
        status.last_run_at = now
        status.last_success_at = now
        status.last_updated_at = now
        return normalized

    def _apply_source_result(
        self,
        source_id: str,
        payload: dict[str, object],
        *,
        observed_at: datetime,
    ) -> tuple[dict[str, object] | None, SourceState | None]:
        status = payload.get("status", "ok")
        if not isinstance(status, str):
            raise ValueError("source result status must be a string")
        if status == "skip":
            return None, None
        if status == "no_update":
            state = self.source_states.setdefault(source_id, SourceState(source_id=source_id))
            return dict(state.current.payload), state
        state = self._update_source_state(
            source_id,
            payload,
            observed_at=observed_at,
        )
        return dict(state.current.payload), state

    def _build_test_evaluate_source_states(
        self,
        rule: Rule,
        payload: Mapping[str, object] | SourceResult,
        now: datetime,
    ) -> dict[str, SourceState]:
        if isinstance(payload, SourceResult):
            resolved_sources = list(getattr(rule, "resolved_sources", []))
            if len(resolved_sources) != 1:
                raise ValueError("SourceResult test payload requires rule with exactly one resolved source")
            normalized = self._normalize_source_result(payload, now=now)
            return self._overlay_test_source_payloads(
                rule,
                {
                    resolved_sources[0]: {
                        "current": normalized,
                    }
                },
                now,
            )

        normalized_payload = dict(payload)
        if "inputs" in normalized_payload:
            return self._overlay_test_source_payloads(
                rule,
                self._normalize_test_input_map_payload(normalized_payload),
                now,
            )

        shorthand_keys = {"value", "timestamp", "metadata", "prev_value", "prev_timestamp", "prev_metadata"}
        if shorthand_keys.intersection(normalized_payload):
            normalized_inputs = list(getattr(rule, "inputs", []))
            if len(normalized_inputs) != 1 or ":" not in normalized_inputs[0] or "*" in normalized_inputs[0]:
                raise ValueError("single-input shorthand requires exactly one explicit input selector")
            return self._overlay_test_source_payloads(
                rule,
                self._normalize_test_input_map_payload(
                    {
                        "inputs": {
                            normalized_inputs[0]: normalized_payload,
                        }
                    }
                ),
                now,
            )

        raise ValueError(
            "test payload must contain 'inputs' or the single-input shorthand fields "
            "('value', 'timestamp', 'metadata', 'prev_value', 'prev_timestamp', 'prev_metadata')"
        )

    def _overlay_test_source_payloads(
        self,
        rule: Rule,
        source_payloads: dict[str, dict[str, dict[str, object]]],
        now: datetime,
    ) -> dict[str, SourceState]:
        preview_states: dict[str, SourceState] = {}
        all_source_ids = sorted(set(getattr(rule, "resolved_sources", [])) | set(source_payloads))
        for source_id in all_source_ids:
            existing_state = self.source_states.get(source_id, SourceState(source_id=source_id))
            payloads = source_payloads.get(source_id, {})
            current_payload = dict(payloads.get("current", existing_state.current.payload))
            previous_payload = dict(payloads.get("previous", existing_state.current.payload))
            preview_states[source_id] = SourceState(
                source_id=source_id,
                current=SourceSnapshot(payload=current_payload, observed_at=now),
                previous=SourceSnapshot(payload=previous_payload, observed_at=existing_state.current.observed_at),
                updated_at=now,
                poll_count=existing_state.poll_count,
            )
        return preview_states

    def _normalize_test_input_map_payload(
        self,
        payload: Mapping[str, object],
    ) -> dict[str, dict[str, dict[str, object]]]:
        raw_inputs = payload.get("inputs", {})
        if not isinstance(raw_inputs, Mapping):
            raise ValueError("payload.inputs must be an object")
        status = payload.get("status", "ok")
        error = payload.get("error")
        top_metadata = payload.get("metadata")
        if top_metadata is not None and not isinstance(top_metadata, Mapping):
            raise ValueError("payload.metadata must be an object when set")
        grouped: dict[str, dict[str, dict[str, object]]] = {}
        for full_name, raw in raw_inputs.items():
            if not isinstance(full_name, str) or ":" not in full_name:
                raise ValueError("input names must be '<source_id>:<input_name>'")
            if not isinstance(raw, Mapping):
                raise ValueError(f"input '{full_name}' must be an object")
            source_id, input_name = full_name.split(":", 1)
            current_channels = grouped.setdefault(source_id, {}).setdefault("current", {}).setdefault("channels", {})
            previous_channels = grouped.setdefault(source_id, {}).setdefault("previous", {}).setdefault("channels", {})
            current_channels[input_name] = {
                "value": raw.get("value"),
                "timestamp": _coerce_test_timestamp(raw.get("timestamp")),
                "metadata": dict(raw.get("metadata", {})) if isinstance(raw.get("metadata", {}), Mapping) else {},
            }
            if "prev_value" in raw or "prev_timestamp" in raw or "prev_metadata" in raw:
                previous_channels[input_name] = {
                    "value": raw.get("prev_value"),
                    "timestamp": _coerce_test_timestamp(raw.get("prev_timestamp")),
                    "metadata": dict(raw.get("prev_metadata", {})) if isinstance(raw.get("prev_metadata", {}), Mapping) else {},
                }
        for source_id, payloads in grouped.items():
            current_payload = payloads.setdefault("current", {})
            current_payload.setdefault("channels", {})
            current_payload["status"] = status
            if error is not None:
                current_payload["error"] = error
            if isinstance(top_metadata, Mapping) and top_metadata:
                current_payload["metadata"] = dict(top_metadata)
            previous_payload = payloads.setdefault("previous", {})
            previous_payload.setdefault("channels", {})
        return grouped

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
        *,
        active_since: datetime | None,
        evaluated_at: datetime,
    ) -> None:
        previous = self.alerts.get(rule.rule_id)
        restored = self._restored_rule_states.pop(rule.rule_id, None) if previous is None else None
        comparison_previous = previous
        if comparison_previous is None and restored is not None:
            restored_state, restored_severity = restored
            comparison_previous = Alert(
                rule_id=rule.rule_id,
                state=restored_state,
                severity=restored_severity,
                owner=rule.owner,
                tags=tuple(rule.tags),
            )
        acknowledgement = self.acknowledgements.get(rule.rule_id)
        acked_at = acknowledgement.created_at if acknowledgement and state == AlertState.ACKED else None
        acked_by = acknowledgement.operator if acknowledgement and state == AlertState.ACKED else None
        ack_reason = acknowledgement.reason if acknowledgement and state == AlertState.ACKED else None
        active_silence_ids = tuple(silence.silence_id for silence in self._matching_active_silences(rule, now))

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
            last_evaluated_at=evaluated_at,
            acked_at=acked_at,
            acked_by=acked_by,
            ack_reason=ack_reason,
            active_silence_ids=active_silence_ids,
        )
        self.alerts[rule.rule_id] = alert

        event = self._derive_alert_event(
            previous=comparison_previous,
            alert=alert,
            occurred_at=now,
        )

        if event is not None:
            self.store.append_alert_event(
                event,
                definition_file=getattr(rule.__class__, "__kanary_definition_file__", None),
                matched_outputs=list(getattr(rule, "matched_outputs", [])),
            )

        if event is not None and (
            comparison_previous is not None or alert.state == AlertState.FIRING
        ):
            self._emit_alert_event(event)

    def _store_rule_evaluation(
        self,
        rule: Rule,
        evaluation: Evaluation,
        now: datetime,
    ) -> _RuleEvaluation:
        previous = self._rule_evaluations.get(rule.rule_id)
        active_since = (
            previous.active_since
            if evaluation.state == AlertState.FIRING
            and previous is not None
            and previous.state == AlertState.FIRING
            else now if evaluation.state == AlertState.FIRING else None
        )
        stored = _RuleEvaluation(
            state=evaluation.state,
            payload=dict(evaluation.payload or {}),
            message=evaluation.message,
            severity=evaluation.severity or rule.severity,
            evaluated_at=now,
            active_since=active_since,
        )
        self._rule_evaluations[rule.rule_id] = stored
        return stored

    def _apply_effective_evaluation(
        self,
        rule: Rule,
        evaluation: _RuleEvaluation,
        now: datetime,
    ) -> None:
        dependency_state = self._resolve_dependency_state(rule, evaluation.payload)
        if dependency_state is not None:
            self._apply_evaluation(
                rule,
                dependency_state.state,
                dependency_state.payload,
                dependency_state.message,
                dependency_state.severity,
                now,
                active_since=evaluation.active_since,
                evaluated_at=evaluation.evaluated_at,
            )
            return

        operator_state = self._resolve_operator_state(
            rule,
            evaluation.state,
            evaluation.payload,
            evaluation.message,
            evaluation.severity,
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
                active_since=evaluation.active_since,
                evaluated_at=evaluation.evaluated_at,
            )
            return

        self._apply_evaluation(
            rule,
            evaluation.state,
            evaluation.payload,
            evaluation.message,
            evaluation.severity,
            now,
            active_since=evaluation.active_since,
            evaluated_at=evaluation.evaluated_at,
        )

    def _rules_in_dependency_order(self) -> list[Rule]:
        ordered: list[Rule] = []
        visited: set[str] = set()
        visiting: set[str] = set()

        def visit(rule_id: str) -> None:
            if rule_id in visited or rule_id in visiting:
                return
            rule = self.rules.get(rule_id)
            if rule is None:
                return
            visiting.add(rule_id)
            for dependency_rule_id in [*rule.suppressed_by, *rule.depends_on]:
                visit(dependency_rule_id)
            visiting.remove(rule_id)
            visited.add(rule_id)
            ordered.append(rule)

        for rule_id in self.rules:
            visit(rule_id)
        return ordered

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
        for rule in list(self.rules.values()):
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

    def _emit_alert_event(self, event: AlertEvent, *, first: bool = False) -> "_Dispatch":
        """Route an event to the matching outputs' queues; sending happens on their threads.

        Called with the engine lock held, so it only decides and enqueues. ``first``
        puts the event ahead of queued ones (test fire).
        """
        # The engine changes some alerts in place (ACK); a queued event keeps what it saw.
        event = dataclasses.replace(event, alert=dataclasses.replace(event.alert))
        matched_output_ids: list[str] = []
        filtered_output_ids: list[str] = []
        uninitialized_output_ids: list[str] = []
        emit_skipped_output_ids: list[str] = []
        targets: list[tuple[str, OutputWorker]] = []
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
            worker = self._output_workers.get(output_id)
            if not status.init_ok or worker is None:
                uninitialized_output_ids.append(output_id)
                logger.warning(
                    "output '%s' skipped for rule '%s': output is not initialized",
                    output_id,
                    event.rule_id,
                )
                continue
            if not self.output_emit_enabled:
                emit_skipped_output_ids.append(output_id)
                continue
            targets.append((output_id, worker))

        dispatch = _Dispatch(
            self,
            event,
            matched=matched_output_ids,
            filtered=filtered_output_ids,
            uninitialized=uninitialized_output_ids,
            emit_skipped=emit_skipped_output_ids,
            pending=[output_id for output_id, _worker in targets],
        )
        for output_id, worker in targets:
            deliver = partial(self._deliver_event, output_id, event, dispatch)
            if first:
                worker.post(deliver)
            else:
                worker.submit(deliver, on_drop=partial(dispatch.report, output_id, False))
        dispatch.start()
        return dispatch

    def _deliver_event(self, output_id: str, event: AlertEvent, dispatch: "_Dispatch", output: Output) -> None:
        """Send one event; runs on the output's thread."""
        status = self._plugin_status("output", output_id)
        if not status.init_ok:
            dispatch.report(output_id, False, "output is not initialized")
            return
        worker = current_worker()
        sleep = worker.sleep if worker is not None else _plain_sleep
        try:
            self._call_output_emit(output, event)
        except Exception as exc:
            if status.state == "FAILED":
                # Already failing: one attempt per event, so a dead destination does not
                # hold every later event through the full retry sequence.
                self._record_output_failure(status, event, exc, traceback.format_exc())
                dispatch.report(output_id, False, f"{type(exc).__name__}: {exc}")
                return
            delivered = self._recover_output_emit(output_id, output, event, status, exc, sleep=sleep)
            dispatch.report(output_id, delivered, None if delivered else "emit failed after retries")
            return
        self._record_output_success(status, event)
        dispatch.report(output_id, True)

    def _record_output_success(self, status: PluginStatus, event: AlertEvent) -> None:
        self._set_plugin_ready(status)
        status.last_error = None
        status.last_error_detail = None
        status.run_count += 1
        status.last_run_at = event.occurred_at
        status.last_success_at = event.occurred_at
        status.last_updated_at = event.occurred_at

    def _record_output_failure(self, status: PluginStatus, event: AlertEvent, exc: BaseException, detail: str) -> None:
        self._set_plugin_failed(status)
        status.last_error = str(exc)
        status.last_error_detail = detail
        status.last_failure_at = event.occurred_at
        status.last_updated_at = event.occurred_at

    def _record_output_error(self, output_id: str, what: str, exc: BaseException) -> None:
        """An error outside emit() on the output's thread, such as a followup callback."""
        status = self._plugin_status("output", output_id)
        now = self._now_fn()
        status.last_error = f"{what}: {exc}"
        status.last_error_detail = "".join(traceback.format_exception(exc))
        status.last_failure_at = now
        status.last_updated_at = now

    def _record_dispatch(self, dispatch: "_Dispatch") -> None:
        """Log and store where an event went, once every output has finished with it."""
        event = dispatch.event
        logger.info(
            "alert dispatch summary: rule=%s transition=%s->%s matched=%s delivered=%s emit_skipped=%s filtered=%s uninitialized=%s failed=%s",
            event.rule_id,
            event.previous_state.value if event.previous_state is not None else "-",
            event.current_state.value,
            ",".join(dispatch.matched) or "-",
            ",".join(dispatch.delivered) or "-",
            ",".join(dispatch.emit_skipped) or "-",
            ",".join(dispatch.filtered) or "-",
            ",".join(dispatch.uninitialized) or "-",
            ",".join(dispatch.failed) or "-",
        )
        if not dispatch.matched:
            logger.info(
                "alert event for rule '%s' (%s -> %s) had no matching outputs",
                event.rule_id,
                event.previous_state.value if event.previous_state is not None else "-",
                event.current_state.value,
            )
        elif len(dispatch.uninitialized) == len(dispatch.matched):
            logger.warning(
                "alert event for rule '%s' (%s -> %s) had matching outputs but none were initialized",
                event.rule_id,
                event.previous_state.value if event.previous_state is not None else "-",
                event.current_state.value,
            )
        try:
            self.store.append_output_dispatch(
                event=event,
                matched_outputs=list(dispatch.matched),
                delivered_outputs=list(dispatch.delivered),
                filtered_outputs=list(dispatch.filtered),
                uninitialized_outputs=list(dispatch.uninitialized),
                failed_outputs=list(dispatch.failed),
            )
        except Exception:
            logger.exception("could not record the output dispatch for rule '%s'", event.rule_id)

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
            self._call_output_init(output)
            self._set_plugin_ready(status)
            status.init_ok = True
            status.last_error = None
            status.last_error_detail = None
            status.last_success_at = self._now_fn()
            status.last_updated_at = status.last_success_at
        except Exception as exc:
            self._set_plugin_failed(status)
            status.init_ok = False
            status.last_error = str(exc)
            status.last_error_detail = traceback.format_exc()
            status.last_failure_at = self._now_fn()
            status.last_updated_at = status.last_failure_at
            logger.exception("output '%s' init failed", output_id)

    def _terminate_output(self, output_id: str, output: Output) -> None:
        status = self._plugin_status("output", output_id)
        try:
            self._call_output_terminate(output)
        except Exception as exc:
            self._set_plugin_failed(status)
            status.last_error = str(exc)
            status.last_error_detail = traceback.format_exc()
            status.last_failure_at = self._now_fn()
            status.last_updated_at = status.last_failure_at
            logger.exception("output '%s' terminate failed", output_id)

    def _recover_output_emit(
        self,
        output_id: str,
        output: Output,
        event: AlertEvent,
        status: PluginStatus,
        initial_exc: Exception,
        *,
        sleep: Callable[[float], bool],
    ) -> bool:
        """Retry emit() max_retry times, then re-init and retry max_reinit times.

        Runs on the output's thread; sleep() returns True when shutdown cuts the waits short.
        """
        last_exc: BaseException = initial_exc
        last_detail = traceback.format_exc()
        attempt = 0
        plan = ["retry"] * getattr(output, "max_retry", 1) + ["reinit"] * getattr(output, "max_reinit", 1)
        for step in plan:
            attempt += 1
            if sleep(_retry_delay(attempt)):
                break
            if step == "reinit":
                try:
                    self._call_output_terminate(output)
                except Exception:
                    last_detail = traceback.format_exc()
                try:
                    self._call_output_init(output)
                    status.init_ok = True
                except Exception as exc:
                    last_exc = exc
                    last_detail = traceback.format_exc()
                    continue
            try:
                self._call_output_emit(output, event)
            except Exception as exc:
                last_exc = exc
                last_detail = traceback.format_exc()
                continue
            self._record_output_success(status, event)
            return True

        self._record_output_failure(status, event, last_exc, last_detail)
        logger.error("output '%s' failed: %s", output_id, last_exc)
        return False

    def record_source_failure(self, source_id: str, error: str, *, now: datetime | None = None) -> None:
        when = now or self._now_fn()
        status = self._plugin_status("source", source_id)
        self._set_plugin_failed(status)
        status.last_error = error
        status.last_error_detail = None
        status.run_count += 1
        status.last_run_at = when
        status.last_failure_at = when
        status.last_updated_at = when

    def _initialize_source(self, source: Source) -> None:
        status = self._plugin_status("source", source.source_id)
        try:
            self._call_source_init(source)
            self._set_plugin_ready(status)
            status.init_ok = True
            status.last_error = None
            status.last_error_detail = None
            status.last_success_at = self._now_fn()
            status.last_updated_at = status.last_success_at
        except Exception as exc:
            self._set_plugin_failed(status)
            status.init_ok = False
            status.last_error = str(exc)
            status.last_error_detail = traceback.format_exc()
            status.last_failure_at = self._now_fn()
            status.last_updated_at = status.last_failure_at
            raise

    def _terminate_source(self, source: Source) -> None:
        status = self._plugin_status("source", source.source_id)
        try:
            self._call_source_terminate(source)
        except Exception as exc:
            self._set_plugin_failed(status)
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
            ctx = RuleContext(
                now=now,
                source_id=source_state.source_id,
                source_state=source_state,
                source_states=self.source_states,
                declared_inputs=tuple(getattr(rule, "inputs", [])),
                resolved_sources=tuple(getattr(rule, "resolved_sources", [])),
                previous_alert=self.alerts.get(rule.rule_id),
            )
            dependency_state = self._resolve_dependency_state(rule, source_payload)
            if dependency_state is not None:
                previous_evaluation = self._rule_evaluations.get(rule.rule_id)
                self._apply_evaluation(
                    rule,
                    dependency_state.state,
                    dependency_state.payload,
                    dependency_state.message,
                    dependency_state.severity,
                    now,
                    active_since=previous_evaluation.active_since if previous_evaluation else None,
                    evaluated_at=previous_evaluation.evaluated_at if previous_evaluation else now,
                )
            else:
                evaluation = rule.normalize_evaluation(
                    self._call_rule_evaluate(rule, source_payload, ctx),
                    source_payload,
                )
                stored_evaluation = self._store_rule_evaluation(rule, evaluation, now)
                self._apply_effective_evaluation(rule, stored_evaluation, now)
            self._set_plugin_ready(status)
            status.init_ok = True
            status.last_error = None
            status.last_error_detail = None
            status.run_count += 1
            status.last_run_at = now
            status.last_success_at = now
            status.last_updated_at = now
        except Exception as exc:
            self._set_plugin_failed(status)
            status.init_ok = True
            status.last_error = str(exc)
            status.last_error_detail = traceback.format_exc()
            status.run_count += 1
            status.last_run_at = now
            status.last_failure_at = now
            status.last_updated_at = now
            logger.exception("rule '%s' failed", rule.rule_id)

    def _evaluate_rule_from_snapshots(
        self,
        rule: Rule,
        source_payload: dict[str, object],
        now: datetime,
    ) -> None:
        status = self._plugin_status("rule", rule.rule_id)
        try:
            ctx = RuleContext(
                now=now,
                source_id=None,
                source_state=None,
                source_states=self.source_states,
                declared_inputs=tuple(getattr(rule, "inputs", [])),
                resolved_sources=tuple(getattr(rule, "resolved_sources", [])),
                previous_alert=self.alerts.get(rule.rule_id),
            )
            evaluation = rule.normalize_evaluation(
                self._call_rule_evaluate(rule, source_payload, ctx),
                source_payload,
            )
            stored_evaluation = self._store_rule_evaluation(rule, evaluation, now)
            self._apply_effective_evaluation(rule, stored_evaluation, now)
            self._set_plugin_ready(status)
            status.init_ok = True
            status.last_error = None
            status.last_error_detail = None
            status.run_count += 1
            status.last_run_at = now
            status.last_success_at = now
            status.last_updated_at = now
        except Exception as exc:
            self._set_plugin_failed(status)
            status.init_ok = True
            status.last_error = str(exc)
            status.last_error_detail = traceback.format_exc()
            status.run_count += 1
            status.last_run_at = now
            status.last_failure_at = now
            status.last_updated_at = now
            logger.exception("timer evaluation for rule '%s' failed", rule.rule_id)

    def _evaluate_rule_preview(
        self,
        rule: Rule,
        source_payload: dict[str, object],
        source_state: SourceState,
        now: datetime,
    ) -> Alert:
        ctx = RuleContext(
            now=now,
            source_id=source_state.source_id,
            source_state=source_state,
            source_states=self.source_states,
            declared_inputs=tuple(getattr(rule, "inputs", [])),
            resolved_sources=tuple(getattr(rule, "resolved_sources", [])),
            previous_alert=self.alerts.get(rule.rule_id),
        )
        dependency_state = self._resolve_dependency_state(rule, source_payload)
        if dependency_state is not None:
            return dependency_state

        evaluation = rule.normalize_evaluation(
            self._call_rule_evaluate(rule, source_payload, ctx),
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

    def _set_plugin_ready(self, status: PluginStatus) -> None:
        if status.state in {"DISCOVERED", "DIRTY", "PENDING_REMOVE"}:
            return
        status.state = "READY"

    def _set_plugin_failed(self, status: PluginStatus) -> None:
        if status.state in {"DISCOVERED", "DIRTY", "PENDING_REMOVE"}:
            return
        status.state = "FAILED"

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
