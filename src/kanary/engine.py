from collections import deque
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from concurrent.futures import Future, TimeoutError as FutureTimeoutError
import copy
import dataclasses
from dataclasses import dataclass
from datetime import datetime, timezone
from fnmatch import fnmatch
from functools import partial
import logging
import socket
import sqlite3
import time
import traceback
from uuid import uuid4
import threading
from typing import Callable

from .helpers.buffer import discarding_changes
from .dispatch import SHUTDOWN_DRAIN_SECONDS, OutputWorker, current_worker
from .constants import AlertState, DEESCALATED, ESCALATED, Severity, TransitionKind, UNACK, coerce_severity, severity_name
from .models import Acknowledgement, Alert, AlertEvent, Evaluation, InputValue, PluginStatus, PollResult, Silence, SourceSnapshot, SourceState
from .models import normalize_source_output
from .output import Output
from .registry import (
    get_output_registry,
    get_rule_registry,
    get_source_registry,
    prepare_output_class,
    prepare_rule_class,
    prepare_source_class,
)
from .selectors import normalize_rule_inputs, qualify_input_name, resolve_rule_sources
from .rule import Rule, RuleContext
from .store import NullStore, RestoredJudgment, RestoredRuleState
from .source import Source
from .validation import matching_outputs
from .values import coerce_datetime, to_datetime

logger = logging.getLogger("kanary.engine")

# A rule's evaluate() runs while every other evaluation waits; slower than this is reported.
SLOW_EVALUATE_SECONDS = 0.1
# How long a test fire waits for the outputs to send before answering "still queued".
TEST_FIRE_WAIT_SECONDS = 30.0
# How long a reload waits for a new output's init() (which runs after the send in progress).
OUTPUT_INIT_WAIT_SECONDS = 60.0
# How long start() waits for the outputs' init(): a slower one goes on on its own thread.
START_OUTPUT_INIT_WAIT_SECONDS = 10.0


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

    def report(self, output_id: str, delivered: bool, reason: str | None = None, *, skipped: bool = False) -> None:
        """How the output finished with the event; skipped: it was not for this output after
        all (an OK of an alert it was never sent), recorded as filtered."""
        with self._lock:
            if output_id not in self._pending:
                return
            self._pending.discard(output_id)
            if skipped:
                self.matched = [matched for matched in self.matched if matched != output_id]
            (self.filtered if skipped else self.delivered if delivered else self.failed).append(output_id)
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
            matched = list(self.matched)
            filtered = list(self.filtered)
            delivered = list(self.delivered)
            failed = list(self.failed)
        event = self.event
        return {
            "rule_id": event.rule_id,
            "previous_state": event.previous_state.value if event.previous_state is not None else None,
            "current_state": event.current_state.value,
            "occurred_at": event.occurred_at,
            "matched_outputs": matched,
            "delivered_outputs": delivered,
            "emit_skipped_outputs": list(self.emit_skipped),
            "filtered_outputs": filtered,
            "uninitialized_outputs": list(self.uninitialized),
            "failed_outputs": failed,
            "pending_outputs": pending,
        }


class _InitRetry:
    """Calls an output's init() again while it has failed: after 10 s, then twice as long, up to 5 min.

    A timer of the output's worker, so it runs on the output's thread between events.
    """

    FIRST_WAIT = 10.0
    LONGEST_WAIT = 300.0

    def __init__(self, engine: "Engine", output_id: str, worker: OutputWorker) -> None:
        self.engine = engine
        self.output_id = output_id
        self.worker = worker
        self.attempts = 0
        self._lock = threading.Lock()
        self._due: float | None = time.monotonic() + self.FIRST_WAIT

    def next_deadline(self) -> float | None:
        with self._lock:
            return self._due

    def run_due(self) -> None:
        with self._lock:
            if self._due is None or time.monotonic() < self._due:
                return
            self._due = None  # set again by failed() if this attempt fails too
        self.engine._initialize_output(self.output_id, self.worker.output)

    def failed(self) -> None:
        with self._lock:
            self.attempts += 1
            self._due = time.monotonic() + min(self.FIRST_WAIT * 2 ** self.attempts, self.LONGEST_WAIT)
        self.worker.wake()

    def stop(self) -> None:
        with self._lock:
            self._due = None
        self.worker.remove_timer(self)
        if self.engine._output_init_retries.get(self.output_id) is self:
            del self.engine._output_init_retries[self.output_id]


# A write the state database refused for now (locked by another process past SQLite's
# busy timeout, a disk that is full) is tried again after this long, doubling up to the longest.
HISTORY_RETRY_FIRST_SECONDS = 1.0
HISTORY_RETRY_LONGEST_SECONDS = 30.0
# Writes waiting for the state database; beyond this the oldest are dropped (and counted).
MAX_PENDING_HISTORY_WRITES = 100_000
# On shutdown, how long the writes still waiting may take before they are given up.
HISTORY_DRAIN_SECONDS = 10.0
# A history read waits this long for the writes just made, then reads what is there.
HISTORY_READ_WAIT_SECONDS = 0.5


class _HistoryWriter:
    """Writes to the state database on a thread of its own, in the order they were made.

    Evaluation and notifications never wait for the database. A write the database
    refuses for now (sqlite3.OperationalError: locked, busy, full) is tried again, so
    a database locked for a while delays the history but loses nothing of it, and what
    a restart restores matches what happened. A write that cannot work (a bug) is dropped.
    """

    def __init__(self, engine: "Engine") -> None:
        self._engine = engine
        self._jobs: deque[tuple[str, Callable[..., object], tuple, dict]] = deque()  # waiting
        self._current: tuple[str, Callable[..., object], tuple, dict] | None = None  # being written
        self._condition = threading.Condition()
        self._closing = False
        self._deadline: float | None = None
        self.dropped = 0
        self._thread = threading.Thread(target=self._run, name="kanary-state-db", daemon=True)
        self._thread.start()

    def submit(self, what: str, write: Callable[..., object], args: tuple, kwargs: dict) -> None:
        with self._condition:
            self._jobs.append((what, write, args, kwargs))
            if len(self._jobs) > MAX_PENDING_HISTORY_WRITES:
                lost, *_ = self._jobs.popleft()  # the oldest waiting; never the one being written
                self.dropped += 1
                if self.dropped == 1 or self.dropped % 1000 == 0:
                    logger.error(
                        "%d writes were given up: more than %d wait for the state database (the last: %s)",
                        self.dropped, MAX_PENDING_HISTORY_WRITES, lost,
                    )
            self._condition.notify_all()

    @property
    def pending(self) -> int:
        with self._condition:
            return len(self._jobs) + (1 if self._current is not None else 0)

    def flush(self, timeout: float) -> bool:
        """Wait until every write made so far is in the database; False on timeout."""
        deadline = time.monotonic() + timeout
        with self._condition:
            while self._jobs or self._current is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not self._thread.is_alive():
                    return False
                self._condition.wait(remaining)
        return True

    def close(self, timeout: float) -> int:
        """Write what is waiting (for at most timeout), then stop; returns the writes given up."""
        with self._condition:
            self._closing = True
            self._deadline = time.monotonic() + timeout
            self._condition.notify_all()
        self._thread.join(timeout + 1.0)
        with self._condition:
            left = len(self._jobs) + (1 if self._current is not None else 0)
            self._jobs.clear()  # a write still running finishes by itself; nothing comes after it
            return left

    def _run(self) -> None:
        delay = HISTORY_RETRY_FIRST_SECONDS
        while True:
            with self._condition:
                if self._current is None:
                    while not self._jobs and not self._closing:
                        self._condition.wait()
                    if not self._jobs:
                        return
                    self._current = self._jobs.popleft()
                what, write, args, kwargs = self._current
            done = self._write(what, write, args, kwargs)
            with self._condition:
                if done:
                    self._current = None
                    delay = HISTORY_RETRY_FIRST_SECONDS
                    self._condition.notify_all()
                    continue
                wait = delay
                if self._closing and self._deadline is not None:
                    wait = self._deadline - time.monotonic()
                    if wait <= 0:
                        return  # close() counts what is left
                self._condition.wait(min(delay, wait))
                delay = min(delay * 2, HISTORY_RETRY_LONGEST_SECONDS)

    def _write(self, what: str, write: Callable[..., object], args: tuple, kwargs: dict) -> bool:
        """True when the write is finished with: written, or dropped because it cannot work."""
        engine = self._engine
        try:
            write(*args, **kwargs)
        except sqlite3.OperationalError as exc:
            if _may_work_later(exc):  # locked, busy, full: waiting helps
                engine._note_history_failure(f"{what} waits for the state database (tried again): {exc}")
                return False
            engine._note_history_failure(f"{what} was not written to the state database: {exc}")
            return True  # waiting does not help ("no such table"): the writes after it go on
        except Exception as exc:
            engine._note_history_failure(f"{what} was not written to the state database: {type(exc).__name__}: {exc}")
            return True
        engine._note_history_written()
        return True


# SQLite errors that waiting can end: another process holds the database, the disk is
# full or failing, the file is read-only for now. Others (a missing table) do not.
_TRANSIENT_SQLITE_ERRORS = ("SQLITE_BUSY", "SQLITE_LOCKED", "SQLITE_FULL", "SQLITE_IOERR", "SQLITE_READONLY", "SQLITE_CANTOPEN", "SQLITE_PROTOCOL")


def _may_work_later(exc: sqlite3.OperationalError) -> bool:
    name = getattr(exc, "sqlite_errorname", None)
    if name is None:  # older SQLite bindings: by the message
        return any(word in str(exc).lower() for word in ("locked", "busy", "full", "disk i/o", "readonly", "unable to open"))
    return name.startswith(_TRANSIENT_SQLITE_ERRORS)


@dataclass(slots=True)
class _RuleEvaluation:
    state: AlertState
    payload: dict[str, object]
    message: str | None
    severity: Severity
    evaluated_at: datetime
    active_since: datetime | None
    restored: bool = False  # the last evaluation before a restart, as the state database has it


def _read_test_evaluation(
    rule_id: str, evaluation: Mapping[str, object]
) -> tuple[AlertState, Severity, str | None, dict[str, object]]:
    """(state, severity, message, payload) from the result of Engine.test_evaluate()."""
    if not isinstance(evaluation, Mapping):
        raise ValueError("the evaluation must be a JSON object: the output of test-evaluate")
    judged_rule = evaluation.get("rule_id")
    if judged_rule is not None and judged_rule != rule_id:
        raise ValueError(f"the evaluation is of rule '{judged_rule}', not '{rule_id}'")
    missing = [key for key in ("state", "severity") if evaluation.get(key) is None]
    if missing:
        raise ValueError(f"the evaluation has no {' or '.join(missing)}: give the output of test-evaluate")
    raw_state, raw_severity = evaluation["state"], evaluation["severity"]
    try:
        state = AlertState(str(raw_state).upper())
        severity = coerce_severity(raw_severity)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"the evaluation has an invalid state or severity: {raw_state!r}, {raw_severity!r}") from exc
    message = evaluation.get("message")
    payload = evaluation.get("payload")
    return state, severity, str(message) if message is not None else None, dict(payload) if isinstance(payload, Mapping) else {}


def _coerce_test_timestamp(value: object) -> object:
    if isinstance(value, str):
        return to_datetime(value)
    return value


def _test_input(value: object, timestamp: object, metadata: object) -> dict[str, object]:
    """One input of a test-evaluate payload, read as a poll result's input is (NaN is no value)."""
    item = InputValue(
        name="test",
        value=value,
        timestamp=_coerce_test_timestamp(timestamp),
        metadata=dict(metadata) if isinstance(metadata, Mapping) else {},
    )
    return {"value": item.value, "timestamp": item.timestamp, "metadata": item.metadata}


def _silence_targets(value: object, name: str) -> list[str]:
    """Rule patterns or tags of a silence: a list of names, or one name as a string."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if not isinstance(value, (list, tuple, set, frozenset)) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"silence {name} must be a name or a list of names, not {value!r}")
    return [item for item in value if item.strip()]


def source_initialized(source: Source) -> bool:
    """This source instance's init() succeeded and its terminate() has not run since."""
    return bool(getattr(source, "_kanary_initialized", False))


class UnknownTarget(LookupError):
    """An operator action names a rule or silence that does not exist (HTTP 404)."""


class EngineRestarting(RuntimeError):
    """A full restart is replacing this engine: an operator action now would be lost (HTTP 503)."""


class NotApplicable(ValueError):
    """An operator action that does not apply to the alert as it is now (HTTP 409)."""


def _suppressed_message(parents: list[str], state: AlertState, message: str | None) -> str:
    """'suppressed by link', with what the rule itself judged when that is FIRING."""
    text = f"suppressed by {', '.join(parents)}"
    if state == AlertState.FIRING:
        text += f"; would fire: {message}" if message else "; would fire"
    return text


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
        # Without a registry the decorators' global one is used; an empty one stays empty.
        self._source_registry = get_source_registry() if source_registry is None else source_registry
        self._rule_registry = get_rule_registry() if rule_registry is None else rule_registry
        self._output_registry = get_output_registry() if output_registry is None else output_registry
        self._exclude_rule_patterns = exclude_rule_patterns or []
        self._now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self.node_id = node_id or socket.gethostname()
        self.output_emit_enabled = output_emit_enabled
        self._silence_changed_callback = silence_changed_callback
        self.started_at = self._now_fn()
        self.last_reload_at: datetime | None = None
        self.store = store or NullStore()
        self._history: _HistoryWriter | None = None  # started once the plugins exist (below)
        # The last failed write to the state database, until a write works again.
        self.state_db_error: str | None = None
        self.state_db_error_at: datetime | None = None
        self.state_db_failed_writes = 0
        self._lock = threading.RLock()
        # Evaluations wait while a reload holds this gate (holding_evaluations()).
        self._gate = threading.Condition()
        self._gate_holders: dict[int, int] = {}
        self._closed = False  # shut down: it evaluates nothing more
        self._frozen = False  # a full restart is saving it: operator actions are refused
        self.sources = self._instantiate_sources()
        self.rules = self._instantiate_rules()
        self.outputs = self._instantiate_outputs()
        self._refresh_rule_resolutions()  # now that the outputs exist
        self._output_workers: dict[str, OutputWorker] = {}
        self.plugin_states: dict[str, PluginStatus] = {}
        self.alerts: dict[str, Alert] = {}
        self._rule_evaluations: dict[str, _RuleEvaluation] = {}
        # Rules whose last judgment is newer than the one in the state database (the values
        # changed, the state did not): written at shutdown and before reload --full.
        self._unsaved_judgments: set[str] = set()
        # When each rule's own judgment last left FIRING: the rules it suppresses come back
        # once their sources give a value read after that (not with values read during it).
        self._recovered_at: dict[str, datetime] = {}
        # Per output, the rules it was sent a non-OK event for, with the severity it was sent
        # last: an OK goes only where it closes one, and is matched with that severity.
        self._open_by_output: dict[str, dict[str, Severity]] = {}
        # The same as each output's thread saw it sent: what an OK, when its turn comes, closes.
        # Written on the output threads, so behind its own lock.
        self._delivered_open: dict[str, dict[str, Severity]] = {}
        self._delivered_lock = threading.Lock()
        self._output_init_retries: dict[str, "_InitRetry"] = {}
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
        # Writes to the state database run on their own thread (none without a database).
        if getattr(self.store, "enabled", False):
            self._history = _HistoryWriter(self)

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
        # Shown by the viewer and kept with each event: worked out again whenever outputs change.
        rule.matched_outputs = matching_outputs(rule, getattr(self, "outputs", {}))

    def _call_source_init(self, source: Source) -> None:
        source._kanary_engine = self
        source.init()

    def _call_source_poll(self, source: Source, *, now: datetime) -> object:
        source._kanary_engine = self
        source._kanary_poll_now = now
        return source.poll()

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
        source.terminate()

    def _call_rule_evaluate(self, rule: Rule, ctx: RuleContext) -> object:
        started = time.perf_counter()
        try:
            return rule.evaluate(ctx)
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

    def _call_output_init(self, output: Output) -> None:
        output._kanary_engine = self
        output.init()

    def _call_output_emit(self, output: Output, event: AlertEvent) -> None:
        output._kanary_engine = self
        output.emit(event)

    def _call_output_terminate(self, output: Output) -> None:
        output._kanary_engine = self
        output.terminate()

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

    def start(self, *, init_sources: bool = True) -> None:
        """Restore the state, init the sources (unless init_sources is False), and start the outputs.

        The runtime passes init_sources=False: each source's thread calls its init()
        and keeps trying while it fails. An output's init() runs on its own thread;
        start() waits for it briefly, and one that fails is tried again there.
        """
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
            self._restore_alerts(restored.latest_rule_states, restored.judgments)
            # An alert that was not OK when KANARY stopped was most likely sent: its OK is still due.
            still_open = {rule_id: alert.severity for rule_id, alert in self.alerts.items() if alert.state != AlertState.OK}
            notices = restored.output_notices
            if notices is None:
                # A database that kept no notices: count each alert not OK as sent, with its
                # severity now, and keep that from here on.
                self._open_by_output = {output_id: dict(still_open) for output_id in self.outputs}
                for output_id, open_rules in self._open_by_output.items():
                    for rule_id, severity in open_rules.items():
                        self._record("what outputs were sent", self.store.record_output_notice, output_id, rule_id, severity)
            else:
                # What each output was sent, with the severity it was sent last: the OK of an
                # alert that went down to WARN still reaches the output that got its CRITICAL.
                self._open_by_output = {
                    output_id: {
                        rule_id: severity
                        for rule_id, severity in notices.get(output_id, {}).items()
                        if rule_id in still_open
                    }
                    for output_id in self.outputs
                }
            with self._delivered_lock:
                self._delivered_open = {output_id: dict(open_rules) for output_id, open_rules in self._open_by_output.items()}
            for source in self.sources.values() if init_sources else ():
                try:
                    self._initialize_source(source)
                except Exception:
                    logger.exception("source '%s' init failed during startup", source.source_id)
            output_inits = [self._start_output_worker(output_id, output) for output_id, output in self.outputs.items()]
            # Silences that began or ended while KANARY was stopped apply now, not at the
            # rule's next value (which may come much later): with the outputs in place.
            self._reconcile_restored_silences(self._now_fn())
            self._refresh_rule_plugin_resolution_status()
        _wait_for(output_inits, START_OUTPUT_INIT_WAIT_SECONDS)

    def _reconcile_restored_silences(self, now: datetime) -> None:
        """Show each restored alert as the silences of now say: a silence that ended while
        KANARY was stopped ends at the start, one that began then applies at once."""
        for rule in self._rules_in_dependency_order():
            alert = self.alerts.get(rule.rule_id)
            evaluation = self._rule_evaluations.get(rule.rule_id)
            if alert is None or evaluation is None or not alert.restored:
                continue
            silenced_now = bool(self._matching_active_silences(rule, now))
            if silenced_now != (alert.state == AlertState.SILENCED):
                self._apply_effective_evaluation(rule, evaluation, now)

    def _restore_alerts(self, states: dict[str, RestoredRuleState], judgments: Mapping[str, RestoredJudgment] | None = None) -> None:
        """Show each rule's alert as KANARY stopped with it, until the rule's first evaluation.

        Nothing is sent: the state has not changed. The first evaluation compares with
        it as usual, so hysteresis and the alert's active since carry over the restart.
        The rule's own judgment comes back apart from what was shown: a parent that was
        SILENCED still holds its children, and a silence that ends shows its alert.
        """
        now = self._now_fn()
        judgments = judgments or {}
        for rule_id, last in states.items():
            rule = self.rules.get(rule_id)
            if rule is None or self._is_rule_excluded(rule_id):
                continue
            acknowledgement = self.acknowledgements.get(rule_id) if last.state == AlertState.ACKED else None
            judgment = judgments.get(rule_id)
            payload, message = last.payload, last.message
            if judgment is not None and judgment.evaluated_at >= last.occurred_at:
                # Evaluated after the last change of state: the values it last had. The message
                # too, unless a silence or a parent wrote it.
                payload = judgment.payload
                if last.state not in (AlertState.SILENCED, AlertState.SUPPRESSED):
                    message = judgment.message
            self.alerts[rule_id] = Alert(
                rule_id=rule_id,
                state=last.state,
                severity=last.severity,
                owner=rule.owner,
                tags=tuple(rule.tags),
                payload=dict(payload),
                message=message,
                active_since=last.active_since,
                acked_at=acknowledgement.created_at if acknowledgement else None,
                acked_by=acknowledgement.operator if acknowledgement else None,
                ack_reason=acknowledgement.reason if acknowledgement else None,
                active_silence_ids=tuple(silence.silence_id for silence in self._matching_active_silences(rule, now)),
                runbook=getattr(rule, "runbook", None),
                restored=True,
            )
            if judgment is not None:
                self._rule_evaluations[rule_id] = _RuleEvaluation(
                    state=judgment.state,
                    payload=dict(judgment.payload),
                    message=judgment.message,
                    severity=judgment.severity,
                    evaluated_at=judgment.evaluated_at,
                    active_since=judgment.active_since,
                    restored=True,
                )
            elif last.state in (AlertState.FIRING, AlertState.ACKED):
                # A database from before judgments were kept: the rule last judged FIRING
                # (ACKED is FIRING with an ack), so a silence or an ack before its first
                # evaluation applies to it as to any alert.
                self._rule_evaluations[rule_id] = _RuleEvaluation(
                    state=AlertState.FIRING,
                    payload=dict(last.payload),
                    message=last.message,
                    severity=last.severity,
                    evaluated_at=last.occurred_at,
                    active_since=last.active_since,
                    restored=True,
                )

    def shutdown(self, *, terminate_sources: bool = True) -> None:
        """Terminate sources, send what the outputs still have queued (briefly), then close the store.

        terminate() is called for the sources whose init() succeeded. The runtime passes
        terminate_sources=False: each source's thread terminates its own source, after
        its poll() has returned.
        """
        with self._gate:
            self._closed = True
            self._gate.notify_all()
        if self._history is not None:
            self.save_latest_judgments()
        with self._lock:
            for source in self.sources.values() if terminate_sources else ():
                if not source_initialized(source):
                    continue
                try:
                    self._terminate_source(source)
                except Exception:
                    logger.exception("source '%s' terminate failed during shutdown", source.source_id)
            workers = self._output_workers
            self._output_workers = {}
        self._close_output_workers(workers)  # their deliveries are recorded too
        if self._history is not None:
            given_up = self._history.close(HISTORY_DRAIN_SECONDS)
            if given_up:
                logger.error("%d writes were not written to the state database at shutdown: %s", given_up, self.state_db_error)
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

    def busy_outputs(self) -> set[str]:
        """The outputs with events still waiting or being sent."""
        return {output_id for output_id, worker in list(self._output_workers.items()) if worker.backlog}

    def output_backlog(self, output_id: str) -> dict[str, int]:
        """Events waiting for (or being sent by) an output, and how many it dropped."""
        worker = self._output_workers.get(output_id)
        if worker is None:
            return {"queued": 0, "dropped": 0}
        return {"queued": worker.backlog, "dropped": worker.dropped}

    def freeze(self) -> None:
        """Refuse operator actions (EngineRestarting) and silence changes until thaw():
        a full restart saves this engine's state, and a change made after that would be lost."""
        with self._lock:
            self._frozen = True

    def thaw(self) -> None:
        with self._lock:
            self._frozen = False

    def _require_running(self) -> None:
        if self._frozen or self._closed:
            raise EngineRestarting("KANARY is restarting its engine; try again in a moment")

    @contextmanager
    def holding_evaluations(self) -> Iterator[None]:
        """Evaluations wait while this is held: a reload changes sources, outputs, and rules
        together, and a value read meanwhile is judged once all of them are in place (so
        the first alert of a new source also reaches a new output). Reads and operator
        actions go on."""
        holder = threading.get_ident()
        with self._gate:
            self._gate_holders[holder] = self._gate_holders.get(holder, 0) + 1
        try:
            yield
        finally:
            with self._gate:
                self._gate_holders[holder] -= 1
                if not self._gate_holders[holder]:
                    del self._gate_holders[holder]
                self._gate.notify_all()

    def _wait_for_evaluations(self, cancelled: Callable[[], bool] | None = None) -> bool:
        """Wait while a reload holds evaluations; False when cancelled() said to give up,
        or when this engine was shut down (a full restart replaced it)."""
        with self._gate:
            while self._held_by_another():
                if self._closed or (cancelled is not None and cancelled()):
                    return False
                self._gate.wait(0.05)
            return not self._closed and not (cancelled is not None and cancelled())

    def _held_by_another(self) -> bool:
        return bool(self._gate_holders) and threading.get_ident() not in self._gate_holders

    @contextmanager
    def _evaluating(self, cancelled: Callable[[], bool] | None = None) -> Iterator[bool]:
        """The engine lock for an evaluation, taken only while no reload holds evaluations.

        The gate is checked again once the lock is held: a reload that began between the
        check and the lock is waited for, so nothing is judged against half its changes.
        Yields False when the evaluation is not to run (cancelled, or the engine shut down).
        """
        while True:
            if not self._wait_for_evaluations(cancelled):
                yield False
                return
            self._lock.acquire()
            with self._gate:
                held = self._held_by_another()
            if held:
                self._lock.release()
                continue
            try:
                yield True
            finally:
                self._lock.release()
            return

    def evaluate_source(
        self,
        source_id: str,
        payload: Mapping[str, object] | PollResult,
        *,
        now: datetime | None = None,
        cancelled: Callable[[], bool] | None = None,
        source: Source | None = None,
    ) -> dict[str, Alert]:
        """Judge the rules of a source with a poll result. While a reload holds evaluations
        this waits; with cancelled (the runtime's "this source was stopped") it gives up.

        ``source`` is the instance that read the value: when a reload replaced or removed
        it meanwhile, the value is not judged (the new instance's values are the ones now).
        """
        with self._evaluating(cancelled) as go:
            if not go or (source is not None and self.sources.get(source_id) is not source):
                return dict(self.alerts)
            current_time = now or self._now_fn()
            try:
                normalized_payload = self._normalize_source_input(source_id, payload, current_time)
            except (TypeError, ValueError) as exc:
                # A code bug in the source, not a lost connection: it is not retried; the
                # source shows FAILED with the reason until a poll returns a valid result.
                self.record_source_failure(
                    source_id,
                    f"poll() returned an invalid result: {exc}",
                    now=current_time,
                    detail=traceback.format_exc(),
                )
                raise
            source_payload, source_state = self._apply_source_result(
                source_id,
                normalized_payload,
                observed_at=current_time,
            )
            if source_payload is None or source_state is None:
                return dict(self.alerts)
            evaluated: set[str] = set()
            changed: set[str] = set()
            for rule in self._rules_in_dependency_order():  # a parent before the rules it holds back
                if source_id not in getattr(rule, "resolved_sources", []):
                    continue
                if self._is_rule_excluded(rule.rule_id):
                    continue
                was_firing = self._judged_firing(rule.rule_id)
                self._evaluate_rule(rule, source_payload, source_state, current_time)
                evaluated.add(rule.rule_id)
                if self._judged_firing(rule.rule_id) != was_firing:
                    changed.add(rule.rule_id)
            self._update_dependents(changed, evaluated, current_time)
            return dict(self.alerts)

    def evaluate_rule_timer(
        self,
        rule_id: str,
        *,
        now: datetime | None = None,
    ) -> dict[str, Alert]:
        """Evaluate one rule from saved source snapshots without polling a Source."""
        with self._evaluating() as go:
            return self._evaluate_rule_from_last_values(rule_id, now=now) if go else dict(self.alerts)

    def _evaluate_rule_from_last_values(self, rule_id: str, *, now: datetime | None = None) -> dict[str, Alert]:
        with self._lock:
            rule = self.rules.get(rule_id)
            if rule is None or self._is_rule_excluded(rule_id):
                return dict(self.alerts)
            current_time = now or self._now_fn()
            resolved_sources = tuple(getattr(rule, "resolved_sources", []))
            if not resolved_sources:
                return dict(self.alerts)
            source_payload = (
                dict(self.source_states[resolved_sources[0]].current.payload)
                if len(resolved_sources) == 1
                else {}
            )
            was_firing = self._judged_firing(rule_id)
            self._evaluate_rule_from_snapshots(rule, source_payload, current_time)
            if self._judged_firing(rule_id) != was_firing:
                self._update_dependents({rule_id}, {rule_id}, current_time)
            return dict(self.alerts)

    def prepare_plugins(self, plugin_type: str, classes: Mapping[str, type]) -> tuple[dict[str, object], dict[str, str]]:
        """New instances of these classes, and the error of each one that cannot be created.

        Nothing in the engine changes: a reload creates every replacement first, then
        applies the ones that could be created (the others keep their running instance).
        """
        prepare = {"source": prepare_source_class, "rule": prepare_rule_class, "output": prepare_output_class}[plugin_type]
        instances: dict[str, object] = {}
        errors: dict[str, str] = {}
        for plugin_id, cls in classes.items():
            try:
                instances[plugin_id] = prepare(cls)()
            except Exception as exc:
                errors[plugin_id] = f"{type(exc).__name__}: {exc}"
                logger.exception("%s '%s' could not be created; the running one is kept", plugin_type, plugin_id)
        return instances, errors

    def reload_rule_plugins(
        self,
        *,
        replacements: dict[str, type[Rule]],
        removed_rule_ids: set[str] | None = None,
        prepared: Mapping[str, Rule] | None = None,
        evaluate: bool = True,
    ) -> dict[str, str]:
        """Replace and remove rules; returns the error of each rule that could not be created.

        A rule that cannot be created keeps its running instance. With evaluate=False
        the reloaded rules are not judged here: the runtime calls evaluate_reloaded_rules()
        once the sources and outputs of the same reload are in place.
        """
        instances, errors = self._prepared_instances("rule", replacements, prepared)
        with self._lock:
            old_rules = dict(self.rules)
            removed_rule_ids = set(removed_rule_ids or ())
            now = self._now_fn()
            for rule_id in removed_rule_ids:
                if rule_id not in self.rules:
                    continue
                self._record_rule_removal(
                    rule_id,
                    rule=old_rules.get(rule_id),
                    alert=self.alerts.get(rule_id),
                    had_ack=rule_id in self.acknowledgements,
                    now=now,
                )
                self.alerts.pop(rule_id, None)
                self.acknowledgements.pop(rule_id, None)
                self._rule_evaluations.pop(rule_id, None)
                self._recovered_at.pop(rule_id, None)
                for open_rules in self._open_by_output.values():
                    open_rules.pop(rule_id, None)
                with self._delivered_lock:
                    for open_rules in self._delivered_open.values():
                        open_rules.pop(rule_id, None)
                self.rules.pop(rule_id, None)
                self._rule_registry.pop(rule_id, None)

            for rule_id, rule in instances.items():
                self._rule_registry[rule_id] = replacements[rule_id]
                self._rule_evaluations.pop(rule_id, None)
                self._configure_rule(rule)
                self.rules[rule_id] = rule
                status = self._plugin_status("rule", rule_id)
                # What the old code's evaluate() did is not the new code's: its next evaluation tells.
                status.last_error = status.last_error_detail = status.warning = None

            self._rebuild_plugin_states()
            self._sync_plugin_definition_files()
            self._refresh_rule_plugin_resolution_status()
            if evaluate:
                self._evaluate_reloaded_rules(instances)
        return errors

    def evaluate_reloaded_rules(self, rule_ids: Iterable[str]) -> None:
        """Judge these rules now with the last values of their sources (see _evaluate_reloaded_rules)."""
        with self._lock:
            self._evaluate_reloaded_rules(rule_ids)

    def _prepared_instances(
        self,
        plugin_type: str,
        replacements: Mapping[str, type],
        prepared: Mapping[str, object] | None,
    ) -> tuple[dict[str, object], dict[str, str]]:
        """The new instances of a reload: those given, else created here."""
        if prepared is not None:
            return {plugin_id: prepared[plugin_id] for plugin_id in replacements if plugin_id in prepared}, {}
        return self.prepare_plugins(plugin_type, replacements)

    def _evaluate_reloaded_rules(self, rule_ids: Iterable[str]) -> None:
        """Judge reloaded rules at once with the values their sources gave last.

        Otherwise a changed rule waits for its source's next value, which for a source
        that waits for pushed values may take long. A rule with a source that has given
        no value since the start waits for it, as before.
        """
        wanted = set(rule_ids)
        now = self._now_fn()
        for rule in self._rules_in_dependency_order():
            if rule.rule_id not in wanted or self._is_rule_excluded(rule.rule_id):
                continue
            source_ids = tuple(getattr(rule, "resolved_sources", []))
            if not source_ids or any(
                source_id not in self.source_states or self.source_states[source_id].current.observed_at is None
                for source_id in source_ids
            ):
                continue
            logger.info("rule '%s' reloaded: evaluating it with the last values of %s", rule.rule_id, ", ".join(source_ids))
            self._evaluate_rule_from_last_values(rule.rule_id, now=now)

    def _record_rule_removal(self, rule_id: str, *, rule: Rule | None, alert: Alert | None, had_ack: bool, now: datetime) -> None:
        """Into the state database: the rule is gone, with its ack, its judgment, and what
        outputs were sent of it. A rule of the same id added later starts afresh."""
        if alert is not None:
            self._record(
                f"the removal of rule '{rule_id}'",
                self.store.record_rule_removed,
                rule_id=rule_id,
                definition_file=getattr(rule.__class__, "__kanary_definition_file__", None) if rule is not None else None,
                previous_state=alert.state.value,
                previous_severity=severity_name(alert.severity),
                operator="system",
                reason="rule removed during reload",
                created_at=now,
                had_ack=had_ack,
                active_silence_ids=list(alert.active_silence_ids),
            )
        self._record(f"the removal of the judgment of '{rule_id}'", self.store.forget_judgment, rule_id)
        self._record(f"the notices of removed rule '{rule_id}'", self.store.forget_output_notices, rule_id=rule_id)

    def forget_removed_plugins(self, previous: "Engine") -> dict[str, list[str]]:
        """After reload --full: what the previous engine had and this one does not is removed
        as a reload removes it (a rule's ack, judgment, and notices; an output's notices).
        Returns the removed ids by plugin type."""
        now = self._now_fn()
        with self._lock:
            removed = {
                "sources": sorted(set(previous.sources) - set(self.sources)),
                "rules": sorted(set(previous.rules) - set(self.rules)),
                "outputs": sorted(set(previous.outputs) - set(self.outputs)),
            }
            for rule_id in removed["rules"]:
                self._record_rule_removal(
                    rule_id,
                    rule=previous.rules.get(rule_id),
                    alert=previous.alerts.get(rule_id),
                    had_ack=rule_id in previous.acknowledgements,
                    now=now,
                )
            for output_id in removed["outputs"]:
                self._record(f"the notices of removed output '{output_id}'", self.store.forget_output_notices, output_id=output_id)
        return removed

    def reload_output_plugins(
        self,
        *,
        replacements: dict[str, type[Output]],
        removed_output_ids: set[str] | None = None,
        prepared: Mapping[str, Output] | None = None,
        after: list[Callable[[], None]] | None = None,
    ) -> dict[str, str]:
        """Replace and remove outputs; returns the error of each output that could not be created.

        The outputs are in place when this returns. Waiting for the removed ones to stop and
        the new ones' init() follows; with ``after`` those waits are added to it instead,
        for the caller to run (the runtime does, once evaluations go on again).
        """
        instances, errors = self._prepared_instances("output", replacements, prepared)
        closing: dict[str, OutputWorker] = {}
        inits: list[Future] = []
        with self._lock:
            removed_output_ids = set(removed_output_ids or ())
            for output_id in removed_output_ids:
                self.outputs.pop(output_id, None)
                self._open_by_output.pop(output_id, None)
                with self._delivered_lock:
                    self._delivered_open.pop(output_id, None)
                    self._record(f"the notices of removed output '{output_id}'", self.store.forget_output_notices, output_id=output_id)
                self._output_init_retries.pop(output_id, None)
                self._output_registry.pop(output_id, None)
                worker = self._output_workers.pop(output_id, None)
                if worker is not None:
                    closing[output_id] = worker

            for output_id, output in instances.items():
                self._output_registry[output_id] = replacements[output_id]
                # On the output's thread: terminate the old instance, init the new one;
                # events still queued are then sent by the new one.
                inits.append(self._install_output(output_id, output))

            self._refresh_rule_resolutions()  # which outputs each rule reaches
            self._rebuild_plugin_states()
            self._sync_plugin_definition_files()
        waits = [partial(self._close_output_workers, closing), partial(_wait_for, inits, OUTPUT_INIT_WAIT_SECONDS)]
        if after is not None:
            after.extend(waits)
        else:
            for wait in waits:
                wait()
        return errors

    def reload_source_plugins(
        self,
        *,
        replacements: dict[str, type[Source]],
        removed_source_ids: set[str] | None = None,
        prepared: Mapping[str, Source] | None = None,
        start_sources: bool = True,
    ) -> dict[str, str]:
        """Replace and remove sources; returns the error of each source that could not be created.

        With start_sources (an engine without a runtime) the old instances are terminated
        and the new ones initialized here. The runtime passes start_sources=False: each
        source's thread calls init(), poll(), and terminate() of its own instance.
        """
        instances, errors = self._prepared_instances("source", replacements, prepared)
        stopping: list[Source] = []
        with self._lock:
            removed_source_ids = set(removed_source_ids or ())
            for source_id in removed_source_ids:
                source = self.sources.pop(source_id, None)
                self._source_registry.pop(source_id, None)
                self.source_states.pop(source_id, None)
                if source is not None:
                    stopping.append(source)

            for source_id, source in instances.items():
                existing = self.sources.get(source_id)
                if existing is not None:
                    stopping.append(existing)
                self._source_registry[source_id] = replacements[source_id]
                self.sources[source_id] = source
                self.source_states.setdefault(source_id, SourceState(source_id=source_id))
                # The new instance has not run init() yet (the old one's status said it had).
                self._plugin_status("source", source_id).init_ok = False

            self._refresh_rule_resolutions()
            self._rebuild_plugin_states()
            self._sync_plugin_definition_files()
            self._refresh_rule_plugin_resolution_status()
        if not start_sources:
            return errors
        # terminate() and init() may connect somewhere, so they run without the lock.
        for source in stopping:
            if source_initialized(source):
                try:
                    self._terminate_source(source, record=False)
                except Exception:
                    logger.exception("source '%s' terminate failed during reload", source.source_id)
        for source in instances.values():
            try:
                self._initialize_source(source)
            except Exception:
                logger.exception("source '%s' init failed during reload", source.source_id)
        return errors

    def acknowledge(
        self, rule_id: str, *, operator: str, reason: str | None = None, warnings: list[str] | None = None
    ) -> Alert:
        """Acknowledge a FIRING alert. An imported (remote) one is acknowledged at its peer
        too; when the peer cannot be reached the local ack still holds, and why the peer
        did not get it is added to ``warnings`` (and logged)."""
        with self._lock:
            self._require_running()
            rule = self._operator_rule(rule_id)
            self._require_firing(rule_id)
        self._to_peer(
            warnings, f"the ack of '{rule_id}' did not reach its peer",
            getattr(rule, "acknowledge_remote", None), self, operator=operator, reason=reason,
        )
        with self._lock:
            self._require_running()
            rule = self._operator_rule(rule_id)
            alert = self._require_firing(rule_id)
            now = self._now_fn()
            previous_state = alert.state
            acknowledgement = Acknowledgement(
                rule_id=rule_id,
                operator=operator,
                reason=reason,
                created_at=now,
            )
            self.acknowledgements[rule_id] = acknowledgement
            self._record(f"the ack of '{rule_id}'", self.store.record_acknowledgement, acknowledgement, warnings=warnings)
            alert.state = AlertState.ACKED
            alert.acked_at = now
            alert.acked_by = operator
            alert.ack_reason = reason
            alert.last_evaluated_at = now
            self._record(
                f"the ACKED event of '{rule_id}'",
                self.store.append_alert_event,
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
                warnings=warnings,
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

    def unacknowledge(
        self, rule_id: str, *, operator: str, reason: str | None = None, warnings: list[str] | None = None
    ) -> Alert:
        with self._lock:
            self._require_running()
            rule = self._operator_rule(rule_id)
            alert = self.alerts.get(rule_id)
            if rule_id not in self.acknowledgements and (alert is None or alert.state != AlertState.ACKED):
                raise NotApplicable(f"rule '{rule_id}' is not acknowledged")
        self._to_peer(
            warnings, f"the unack of '{rule_id}' did not reach its peer",
            getattr(rule, "unacknowledge_remote", None), self, operator=operator, reason=reason,
        )
        with self._lock:
            self._require_running()
            rule = self._operator_rule(rule_id)
            now = self._now_fn()
            alert = self.alerts.get(rule_id)
            if alert is None:
                raise NotApplicable(f"rule '{rule_id}' has no alert")
            previous_state = alert.state
            self.acknowledgements.pop(rule_id, None)
            self._record(
                f"the unack of '{rule_id}'",
                self.store.record_unacknowledgement,
                rule_id=rule_id,
                operator=operator,
                reason=reason,
                created_at=now,
                warnings=warnings,
            )
            if alert.state == AlertState.ACKED:
                alert.state = AlertState.FIRING
                alert.acked_at = None
                alert.acked_by = None
                alert.ack_reason = None
                alert.last_evaluated_at = now
                self._record(
                    f"the UNACK event of '{rule_id}'",
                    self.store.append_alert_event,
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
                    warnings=warnings,
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
        warnings: list[str] | None = None,
    ) -> Silence:
        """Silence the matching rules. Imported (remote) alarms are silenced at their peers
        too; a peer that cannot be reached does not stop the local silence (see ``warnings``)."""
        start_at, end_at = coerce_datetime(start_at), coerce_datetime(end_at)  # local time without a zone
        if end_at <= start_at:
            raise ValueError("silence end_at must be later than start_at")
        rule_patterns, tags = _silence_targets(rule_patterns, "rule_patterns"), _silence_targets(tags, "tags")
        if not (rule_patterns or tags):
            raise ValueError("silence requires at least one rule pattern or tag")
        with self._lock:
            self._require_running()
        remote_silence_refs = self._propagate_remote_silence(
            operator=operator,
            reason=reason,
            start_at=start_at,
            end_at=end_at,
            rule_patterns=tuple(rule_patterns or []),
            tags=tuple(tags or []),
            warnings=warnings,
        )
        with self._lock:
            self._require_running()
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
            self._record(f"silence {silence.silence_id}", self.store.create_silence, silence, warnings=warnings)
            self.reconcile_silences(now=silence.created_at)
            self._notify_silence_changed()
            return silence

    def cancel_silence(
        self, silence_id: str, *, operator: str, reason: str | None = None, warnings: list[str] | None = None
    ) -> Silence:
        """Cancel a silence here, and its copies at the peers; one a peer did not cancel
        ends there at its end_at (see ``warnings``)."""
        with self._lock:
            self._require_running()
            silence = self.silences.get(silence_id)
            if silence is None:
                raise UnknownTarget(f"unknown silence '{silence_id}'")
            if silence.cancelled_at is not None:
                raise NotApplicable(f"silence '{silence_id}' is already cancelled")
        self._cancel_remote_silence_refs(
            silence.remote_silence_refs,
            operator=operator,
            reason=reason,
            warnings=warnings,
        )
        with self._lock:
            self._require_running()
            silence.cancelled_at = self._now_fn()
            silence.cancelled_by = operator
            silence.cancel_reason = reason
            self._record(f"the cancel of silence {silence.silence_id}", self.store.cancel_silence, silence, warnings=warnings)
            self.reconcile_silences(now=silence.cancelled_at)
            self._notify_silence_changed()
            return silence

    def _operator_rule(self, rule_id: str) -> Rule:
        rule = self.rules.get(rule_id)
        if rule is None:
            raise UnknownTarget(f"unknown rule '{rule_id}'")
        return rule

    def _require_firing(self, rule_id: str) -> Alert:
        """The alert, when it can be acknowledged: FIRING (also as restored after a restart)."""
        alert = self.alerts.get(rule_id)
        if alert is None or alert.state != AlertState.FIRING:
            shown = "WAITING (not evaluated yet)" if alert is None else alert.state.value
            raise NotApplicable(f"rule '{rule_id}' is {shown}: only a FIRING alert can be acknowledged")
        return alert

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
            if self._frozen or self._closed:
                return  # the engine that replaces it applies them at its start
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
        patterns = _silence_targets(rule_patterns, "rule_patterns")
        silence_tags = set(_silence_targets(tags, "tags"))
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

    def get_rule_history(self, rule_id: str, *, limit: int | None = None) -> dict[str, list[dict]]:
        self._flush_for_reading()
        with self._lock:
            rule = self.rules.get(rule_id)
            tags = list(getattr(rule, "tags", [])) if rule is not None else []
        # Read without the engine lock, as the alert history is: evaluations do not wait for it.
        return self.store.get_rule_history(rule_id, tags, limit=limit)

    def get_alert_history(
        self,
        *,
        limit: int = 100,
        after_alert_id: int | None = None,
        after_action_id: int | None = None,
    ) -> dict[str, object]:
        # History is an independent SQLite snapshot; do not stall source evaluation
        # behind the engine-wide state lock while the viewer reads it.
        self._flush_for_reading()
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
        return self._test_poll_output(source_id, result, now=current_time)

    def _test_poll_output(self, source_id: str, result: object, *, now: datetime) -> dict[str, object]:
        """What kanaryctl test-poll prints: the snapshot, with source:input names.

        It is a test-evaluate payload as it is, and those of several sources can be
        put together into one for a rule that reads them all.
        """
        snapshot = self._normalize_source_result(result, now=now)
        output: dict[str, object] = {"source_id": source_id}
        output.update((key, value) for key, value in snapshot.items() if key != "inputs")
        output["inputs"] = {
            qualify_input_name(source_id, name): value for name, value in snapshot["inputs"].items()
        }
        return output

    def test_evaluate(
        self,
        rule_id: str,
        payload: Mapping[str, object] | PollResult,
        *,
        now: datetime | None = None,
    ) -> dict[str, object]:
        with self._lock:
            rule = self.rules[rule_id]
            current_time = now or self._now_fn()
            preview_states = self._build_test_evaluate_source_states(rule, payload, current_time)
            judge = self._preview_rule(rule)
            trigger_source_id = next(iter(getattr(rule, "resolved_sources", []) or [getattr(rule, "source", "")]), "")
            source_state = preview_states.get(trigger_source_id, SourceState(source_id=trigger_source_id))
            original_source_states = self.source_states
            self.source_states = preview_states
            try:
                with discarding_changes():  # what it adds to a kanary.Buffer is dropped
                    alert = self._evaluate_rule_preview(judge, source_state.current.payload, source_state, current_time)
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
                "severity": severity_name(alert.severity),
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

    def _preview_rule(self, rule: Rule) -> Rule:
        """A copy of the rule for test-evaluate: what it changes on self stays out of the live rule.

        A deep copy judges with the state the live rule has now (counters, last values);
        a rule that cannot be copied (it holds a lock or a connection) is judged by a
        new instance of its class, as just after a reload.
        """
        try:
            return copy.deepcopy(rule)
        except Exception as exc:
            logger.info("test-evaluate of rule '%s' uses a new instance (it cannot be copied: %s)", rule.rule_id, exc)
            fresh = prepare_rule_class(type(rule))()
            self._configure_rule(fresh)
            return fresh

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
        state: AlertState | None = None,
        message: str | None = None,
        reason: str | None = None,
        now: datetime | None = None,
        evaluation: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        """Send a made-up event of the rule through the outputs; the live alert is not changed.

        Give either ``state`` or ``evaluation``, the result of test_evaluate(): then the
        event has the state, severity, message, and values that evaluation judged.
        """
        if (state is None) == (evaluation is None):
            raise ValueError("test fire needs either a state or an evaluation (the output of test-evaluate)")
        with self._lock:
            rule = self.rules[rule_id]
            current_time = now or self._now_fn()
            previous_alert = self.alerts.get(rule_id)
            severity = rule.severity
            judged_payload: dict[str, object] = {}
            if evaluation is not None:
                state, severity, judged_message, judged_payload = _read_test_evaluation(rule_id, evaluation)
                message = message or judged_message
            base_message = message or f"synthetic {state.value} notification test"
            synthetic_message = f"[TEST-FIRE] {base_message}"
            synthetic_payload: dict[str, object] = {
                **judged_payload,
                "synthetic": True,
                "test_fire": {
                    "reason": reason,
                    "created_at": current_time.isoformat(),
                },
            }
            alert = Alert(
                rule_id=rule.rule_id,
                state=state,
                severity=severity,
                owner=rule.owner,
                tags=tuple(rule.tags),
                payload=synthetic_payload,
                message=synthetic_message,
                last_evaluated_at=current_time,
                runbook=getattr(rule, "runbook", None),
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
                synthetic=True,
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
        values: dict[str, dict[str, object]] = {}
        for item in normalized_result.inputs:
            values[item.name] = {
                "value": item.value,
                "timestamp": item.timestamp,
                "metadata": item.metadata,
            }

        payload: dict[str, object] = {"status": normalized_result.status}
        if normalized_result.reason is not None:
            payload["reason"] = normalized_result.reason
        if normalized_result.metadata:
            payload["metadata"] = normalized_result.metadata
        payload["inputs"] = values
        return payload

    def _normalize_source_input(
        self,
        source_id: str,
        payload: Mapping[str, object] | PollResult,
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
        payload: Mapping[str, object] | PollResult,
        now: datetime,
    ) -> dict[str, SourceState]:
        if isinstance(payload, PollResult):
            resolved_sources = list(getattr(rule, "resolved_sources", []))
            if len(resolved_sources) != 1:
                raise ValueError("a poll result as test payload needs a rule that reads exactly one source")
            normalized = self._normalize_source_result(payload, now=now)
            return self._overlay_test_source_payloads(rule, {resolved_sources[0]: {"current": normalized}}, now)

        normalized_payload = dict(payload)
        if "inputs" in normalized_payload or "channels" in normalized_payload:
            # test-evaluate-template, test-poll, or an alert's payload; "channels" is
            # how test-poll wrote it before 0.8.0.
            return self._overlay_test_source_payloads(
                rule,
                self._normalize_test_input_map_payload(rule, normalized_payload),
                now,
            )

        if "value" in normalized_payload or "prev_value" in normalized_payload:
            normalized_inputs = list(getattr(rule, "inputs", []))
            if len(normalized_inputs) != 1 or ":" not in normalized_inputs[0] or "*" in normalized_inputs[0]:
                raise ValueError("single-input shorthand requires exactly one explicit input selector")
            return self._overlay_test_source_payloads(
                rule,
                self._normalize_test_input_map_payload(rule, {"inputs": {normalized_inputs[0]: normalized_payload}}),
                now,
            )

        raise ValueError(
            "test payload must be one of: {'inputs': {'<source_id>:<input>': {'value': ...}}}, "
            "which is also what test-poll prints; or, for a rule with one input, "
            "{'value': ..., 'timestamp': ..., 'prev_value': ...}"
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
            # Copies: what the rule under test changes in its inputs stays out of the live snapshots.
            current_payload = copy.deepcopy(dict(payloads.get("current", existing_state.current.payload)))
            previous_payload = copy.deepcopy(dict(payloads.get("previous", existing_state.current.payload)))
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
        rule: Rule,
        payload: Mapping[str, object],
    ) -> dict[str, dict[str, dict[str, object]]]:
        key = "inputs" if "inputs" in payload else "channels"
        raw_inputs = payload.get(key)
        if not isinstance(raw_inputs, Mapping):
            raise ValueError(f"payload.{key} must be an object")
        status = payload.get("status", "ok")
        reason = payload.get("reason")
        top_metadata = payload.get("metadata")
        if top_metadata is not None and not isinstance(top_metadata, Mapping):
            raise ValueError("payload.metadata must be an object when set")
        # A name without "source:" belongs to the payload's source_id (test-poll
        # prints it), or else to the rule's one source (an alert's payload).
        named_source = payload.get("source_id")
        resolved_sources = list(getattr(rule, "resolved_sources", []))
        if isinstance(named_source, str) and named_source:
            default_source: str | None = named_source
        else:
            named_source = None
            default_source = resolved_sources[0] if len(resolved_sources) == 1 else None
        grouped: dict[str, dict[str, dict[str, object]]] = {}
        if named_source is not None and status not in ("no_update", "skip"):
            grouped[named_source] = {}  # its snapshot, even an empty one
        for name, raw in raw_inputs.items():
            if not isinstance(name, str) or not name:
                raise ValueError("input names must be non-empty strings")
            if not isinstance(raw, Mapping):
                raise ValueError(f"input '{name}' must be an object")
            if key == "inputs" and ":" in name:
                source_id, input_name = name.split(":", 1)
            elif default_source is not None:
                source_id, input_name = default_source, name
            else:
                read = ", ".join(resolved_sources) or "no source"
                raise ValueError(f"input '{name}' needs its source: write '<source_id>:{name}' (rule '{rule.rule_id}' reads {read})")
            current_inputs = grouped.setdefault(source_id, {}).setdefault("current", {}).setdefault("inputs", {})
            previous_inputs = grouped[source_id].setdefault("previous", {}).setdefault("inputs", {})
            current_inputs[input_name] = _test_input(raw.get("value"), raw.get("timestamp"), raw.get("metadata", {}))
            if "prev_value" in raw or "prev_timestamp" in raw or "prev_metadata" in raw:
                previous_inputs[input_name] = _test_input(raw.get("prev_value"), raw.get("prev_timestamp"), raw.get("prev_metadata", {}))
        for payloads in grouped.values():
            current_payload = payloads.setdefault("current", {})
            current_payload["status"] = status
            if reason is not None:
                current_payload["reason"] = reason
            if isinstance(top_metadata, Mapping) and top_metadata:
                current_payload["metadata"] = dict(top_metadata)
            current_payload["inputs"] = current_payload.pop("inputs", {})
            payloads.setdefault("previous", {}).setdefault("inputs", {})
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
        restored: bool = False,
    ) -> None:
        previous = self.alerts.get(rule.rule_id)  # may be restored from before a restart
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
            runbook=getattr(rule, "runbook", None),
            restored=restored,
        )
        self.alerts[rule.rule_id] = alert

        event = self._derive_alert_event(
            previous=previous,
            alert=alert,
            occurred_at=now,
        )

        if event is not None:
            # A failed write (the database locked by a backup, a full disk) is shown on the
            # engine's state-database status; the event is still sent below.
            self._record(
                f"the {event.current_state.value} event of '{rule.rule_id}'",
                self.store.append_alert_event,
                event,
                definition_file=getattr(rule.__class__, "__kanary_definition_file__", None),
                matched_outputs=list(getattr(rule, "matched_outputs", [])),
            )

        if event is not None and (
            previous is not None or alert.state == AlertState.FIRING
        ):
            self._emit_alert_event(event)

    def _store_rule_evaluation(
        self,
        rule: Rule,
        evaluation: Evaluation,
        now: datetime,
    ) -> _RuleEvaluation:
        if self._judged_firing(rule.rule_id) and evaluation.state == AlertState.OK:
            self._recovered_at[rule.rule_id] = now
        previous = last = self._rule_evaluations.get(rule.rule_id)
        if previous is None:
            existing = self.alerts.get(rule.rule_id)
            if existing is not None and existing.state != AlertState.OK and existing.active_since is not None:
                # Still alerting after a restart or a reload of the rule: it began before.
                previous = _RuleEvaluation(
                    state=AlertState.FIRING,
                    payload=existing.payload,
                    message=existing.message,
                    severity=existing.severity,
                    evaluated_at=now,
                    active_since=existing.active_since,
                )
        # Any judgment but OK goes on with the alert: a RemoteAlarm judges the remote
        # state, which may be ACKED, SILENCED, or SUPPRESSED there.
        active = evaluation.state != AlertState.OK
        continuing = active and previous is not None and previous.state != AlertState.OK and previous.active_since is not None
        active_since = previous.active_since if continuing else now if active else None
        stored = _RuleEvaluation(
            state=evaluation.state,
            payload=dict(evaluation.payload or {}),
            message=evaluation.message,
            severity=evaluation.severity or rule.severity,
            evaluated_at=now,
            active_since=active_since,
        )
        self._rule_evaluations[rule.rule_id] = stored
        if last is None or (last.state, last.severity, last.active_since) != (stored.state, stored.severity, stored.active_since):
            # Kept for a restart: what a silence or a parent shows may hide it (see _restore_alerts).
            self._record_judgment(rule.rule_id, stored)
        else:
            self._unsaved_judgments.add(rule.rule_id)  # the same judgment, with newer values
        return stored

    def _record_judgment(self, rule_id: str, judgment: _RuleEvaluation) -> None:
        self._unsaved_judgments.discard(rule_id)
        self._record(
            f"the judgment of '{rule_id}'",
            self.store.record_judgment,
            rule_id,
            RestoredJudgment(
                state=judgment.state,
                severity=judgment.severity,
                message=judgment.message,
                payload=judgment.payload,
                evaluated_at=judgment.evaluated_at,
                active_since=judgment.active_since,
            ),
        )

    def save_latest_judgments(self) -> None:
        """Write each rule's last judgment where only its values changed since the one written:
        after a restart, an alert that a silence hid comes back with the values it last had."""
        with self._lock:
            for rule_id in sorted(self._unsaved_judgments):
                judgment = self._rule_evaluations.get(rule_id)
                if judgment is not None and rule_id in self.rules and not judgment.restored:
                    self._record_judgment(rule_id, judgment)
            self._unsaved_judgments.clear()

    def _close_acknowledgement(self, rule: Rule, evaluation: _RuleEvaluation) -> Acknowledgement | None:
        """An ack belongs to one alert: it ends when the rule itself judges that alert over.

        Also while a silence or a parent hides the recovery, so the next alert of the
        rule is not shown as acknowledged by whoever acknowledged the last one. Only OK
        ends it: a RemoteAlarm judges the remote state, which may be ACKED or SILENCED.
        """
        if evaluation.state != AlertState.OK:
            return None
        return self.acknowledgements.pop(rule.rule_id, None)

    def _record_closed_acknowledgement(self, rule: Rule, closed: Acknowledgement | None, now: datetime) -> None:
        """Keep an ack that ended without an OK event in the history (and for a restart)."""
        alert = self.alerts.get(rule.rule_id)
        if closed is None or alert is None or alert.state == AlertState.OK:
            return  # the OK event shows the end of the alert, and of its ack
        self._record(
            f"the end of the ack of '{rule.rule_id}'",
            self.store.record_unacknowledgement,
            rule_id=rule.rule_id,
            operator="system",
            reason=f"the rule recovered while {alert.state.value}; the ack by {closed.operator} ended with its alert",
            created_at=now,
        )

    def _apply_effective_evaluation(
        self,
        rule: Rule,
        evaluation: _RuleEvaluation,
        now: datetime,
    ) -> None:
        if self._holding_parents(rule, rule.depends_on):
            self._apply_blocked(rule, now)
            return
        suppressing = self._holding_parents(rule, rule.suppressed_by)
        if suppressing:
            self._apply_evaluation(
                rule,
                AlertState.SUPPRESSED,
                evaluation.payload,
                _suppressed_message(suppressing, evaluation.state, evaluation.message),
                evaluation.severity,
                now,
                active_since=evaluation.active_since,
                evaluated_at=evaluation.evaluated_at,
                restored=evaluation.restored,
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
                restored=evaluation.restored,
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
            restored=evaluation.restored,
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

    def _judged_firing(self, rule_id: str) -> bool:
        """The rule's own last judgment is an alert, whatever silence, an ack, or a parent show of it.

        That is FIRING for a rule; a RemoteAlarm also judges ACKED, SILENCED, or SUPPRESSED
        when the remote alert is: it is still an alert.
        """
        evaluation = self._rule_evaluations.get(rule_id)
        return evaluation is not None and evaluation.state != AlertState.OK

    def _holding_parents(self, rule: Rule, parent_ids: Iterable[str]) -> list[str]:
        """The parents (suppressed_by or depends_on) that hold the rule back now.

        A parent holds it while its own judgment is FIRING, and after that until each
        of the rule's sources gives a value read after the parent recovered: the values
        read during the trouble (a link down, a power cut) would judge the rule wrongly.
        A source that has given no value at all has none from the trouble either.
        """
        parent_ids = list(parent_ids)
        if not parent_ids:
            return []
        holding = [parent_id for parent_id in parent_ids if self._judged_firing(parent_id)]
        oldest = min(
            (
                state.current.observed_at
                for source_id in getattr(rule, "resolved_sources", [])
                if (state := self.source_states.get(source_id)) is not None and state.current.observed_at is not None
            ),
            default=None,
        )
        if oldest is not None:
            for parent_id in parent_ids:
                recovered_at = self._recovered_at.get(parent_id)
                if parent_id not in holding and recovered_at is not None and oldest < recovered_at:
                    holding.append(f"{parent_id} (recovered; waiting for a new value)")
        return holding

    def _apply_blocked(self, rule: Rule, now: datetime) -> None:
        """depends_on: the rule is not evaluated while a parent holds it; it shows SUPPRESSED."""
        self._apply_held(rule, f"blocked by {', '.join(self._holding_parents(rule, rule.depends_on))} (not evaluated)", now)

    def _apply_held(self, rule: Rule, message: str, now: datetime) -> None:
        """Show the rule SUPPRESSED without a new judgment (its last one is kept)."""
        last = self._rule_evaluations.get(rule.rule_id)
        self._apply_evaluation(
            rule,
            AlertState.SUPPRESSED,
            dict(last.payload) if last else {},
            message,
            last.severity if last else rule.severity,
            now,
            active_since=last.active_since if last else None,
            evaluated_at=last.evaluated_at if last else now,
        )

    def _update_dependents(self, changed: set[str], evaluated: set[str], now: datetime) -> None:
        """Rules whose parents' judgment changed show it at once; they are not evaluated here.

        A rule on the same source was already evaluated after its parent (dependency
        order). One on another source becomes SUPPRESSED when its parent fires, and
        comes back with its next value after the parent recovered.
        """
        if not changed:
            return
        for rule in self._rules_in_dependency_order():
            if rule.rule_id in evaluated or self._is_rule_excluded(rule.rule_id):
                continue
            if not any(parent_id in changed for parent_id in (*rule.depends_on, *rule.suppressed_by)):
                continue
            if self._holding_parents(rule, rule.depends_on):
                self._apply_blocked(rule, now)
                continue
            evaluation = self._rule_evaluations.get(rule.rule_id)
            if evaluation is not None:
                self._apply_effective_evaluation(rule, evaluation, now)

    def _hold_failed_rule(self, rule: Rule, now: datetime) -> None:
        """evaluate() raised while a suppressed_by parent holds the rule: it still shows SUPPRESSED."""
        suppressing = self._holding_parents(rule, rule.suppressed_by)
        if not suppressing:
            return
        try:
            self._apply_held(rule, f"suppressed by {', '.join(suppressing)}; evaluate() failed", now)
        except Exception:
            logger.exception("rule '%s' could not be shown as suppressed", rule.rule_id)

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
        targets: list[tuple[str, OutputWorker, AlertEvent]] = []
        for output_id, output in self.outputs.items():
            open_rules = self._open_by_output.setdefault(output_id, {})
            sent = event
            if not first and event.current_state == AlertState.OK:
                if event.rule_id not in open_rules:
                    # This output was sent nothing but OK for the rule: there is nothing to close.
                    filtered_output_ids.append(output_id)
                    logger.debug(
                        "output '%s' skipped for rule '%s': OK, and it was sent no alert to close",
                        output_id,
                        event.rule_id,
                    )
                    continue
                # The OK closes the alert this output was sent: it has that alert's severity,
                # also when the alert went down to a severity the output does not take.
                sent = dataclasses.replace(event, previous_severity=open_rules[event.rule_id])
            if not output.matches(sent):
                filtered_output_ids.append(output_id)
                logger.debug(
                    "output '%s' skipped for rule '%s': event does not match output filters",
                    output_id,
                    event.rule_id,
                )
                continue
            if not first:  # a test fire is not part of the rule's story
                if event.current_state == AlertState.OK:
                    del open_rules[event.rule_id]
                else:
                    open_rules[event.rule_id] = event.current_severity
            matched_output_ids.append(output_id)
            worker = self._output_workers.get(output_id)
            if worker is None:  # being removed
                uninitialized_output_ids.append(output_id)
                logger.warning("output '%s' skipped for rule '%s': output has no thread", output_id, event.rule_id)
                continue
            if not self.output_emit_enabled:
                emit_skipped_output_ids.append(output_id)
                continue
            targets.append((output_id, worker, sent))

        dispatch = _Dispatch(
            self,
            event,
            matched=matched_output_ids,
            filtered=filtered_output_ids,
            uninitialized=uninitialized_output_ids,
            emit_skipped=emit_skipped_output_ids,
            pending=[output_id for output_id, _worker, _sent in targets],
        )
        for output_id, worker, sent in targets:
            deliver = partial(self._deliver_event, output_id, sent, dispatch)
            if first:
                worker.post(deliver)
            else:
                worker.submit(deliver, on_drop=partial(dispatch.report, output_id, False))
        dispatch.start()
        return dispatch

    def _deliver_event(self, output_id: str, event: AlertEvent, dispatch: "_Dispatch", output: Output) -> None:
        """Send one event; runs on the output's thread."""
        if event.current_state == AlertState.OK and not event.synthetic:
            # Whether an OK closes something is known here, in the order the output sent its
            # events: an alert whose FIRING failed to send has nothing to close.
            with self._delivered_lock:
                told = self._delivered_open.get(output_id, {}).get(event.rule_id)
            if told is None:
                dispatch.report(output_id, False, skipped=True)
                return
            event = dataclasses.replace(event, previous_severity=told)  # the severity it was sent
        status = self._plugin_status("output", output_id)
        if not status.init_ok:
            # init() failed before (at start, or the destination was down): try it now.
            self._initialize_output(output_id, output)
            if not status.init_ok:
                dispatch.report(output_id, False, f"init() failed: {status.last_error}")
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
        self._delivered(output_id, status, event)
        dispatch.report(output_id, True)

    def _delivered(self, output_id: str, status: PluginStatus, event: AlertEvent) -> None:
        """emit() worked: keep for a restart what this output was told (not for a test fire)."""
        self._record_output_success(status, event)
        # Not for a test fire, nor for an output removed meanwhile (its last events go out
        # as it stops: what it was told no longer matters).
        if event.synthetic:
            return
        severity = None if event.current_state == AlertState.OK else event.current_severity
        with self._delivered_lock:  # with the removal of the output, one or the other first
            if output_id not in self.outputs:
                return
            told = self._delivered_open.setdefault(output_id, {})
            if severity is None:
                told.pop(event.rule_id, None)
            else:
                told[event.rule_id] = severity
            self._record("what an output was sent", self.store.record_output_notice, output_id, event.rule_id, severity)

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
        self._record(
            f"the output dispatch of '{event.rule_id}'",
            self.store.append_output_dispatch,
            event=event,
            matched_outputs=list(dispatch.matched),
            delivered_outputs=list(dispatch.delivered),
            filtered_outputs=list(dispatch.filtered),
            uninitialized_outputs=list(dispatch.uninitialized),
            failed_outputs=list(dispatch.failed),
        )

    def _record(self, what: str, write: Callable[..., object], *args: object, warnings: list[str] | None = None, **kwargs: object) -> None:
        """Write to the state database on its own thread (see _HistoryWriter): what is being
        recorded (an alert's notification, an ack, a silence) never waits for it. While the
        database refuses writes, ``warnings`` says so; the write goes in once it takes them."""
        writer = self._history
        if writer is None:
            write(*args, **kwargs)  # no state database: nothing waits on it
            return
        if warnings is not None and self.state_db_error is not None:
            warning = f"the state database does not take writes now; this is written once it does ({self.state_db_error})"
            if warning not in warnings:
                warnings.append(warning)
        # The engine changes some alerts in place (an ack): the write keeps what was there now.
        args = tuple(dataclasses.replace(arg, alert=dataclasses.replace(arg.alert)) if isinstance(arg, AlertEvent) else arg for arg in args)
        kwargs = {key: dataclasses.replace(value, alert=dataclasses.replace(value.alert)) if isinstance(value, AlertEvent) else value
                  for key, value in kwargs.items()}
        writer.submit(what, write, args, kwargs)

    def _note_history_failure(self, message: str) -> None:
        self.state_db_failed_writes += 1
        if message != self.state_db_error:
            logger.error("%s", message)
        self.state_db_error = message
        self.state_db_error_at = self._now_fn()

    def _note_history_written(self) -> None:
        if self.state_db_error is not None:
            logger.info("the state database takes writes again (%d failed attempts before)", self.state_db_failed_writes)
            self.state_db_error = None

    def flush_history(self, timeout: float = 2.0) -> bool:
        """Wait until every write so far is in the state database; False on timeout."""
        return self._history.flush(timeout) if self._history is not None else True

    def _flush_for_reading(self) -> None:
        """Before reading the history back: what just happened is in it. Not while the
        database refuses writes: the reader then gets what is there at once."""
        if self.state_db_error is None:
            self.flush_history(HISTORY_READ_WAIT_SECONDS)  # short: a busy database is not waited out

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

    def _to_peer(self, warnings: list[str] | None, what: str, call: object, *args: object, **kwargs: object) -> object:
        """Forward an operator action to a peer; a failure is a warning, not the action failing.

        During an incident the peer may be the thing that is down: the local ack or
        silence must still work then.
        """
        if not callable(call):
            return None
        try:
            return call(*args, **kwargs)
        except Exception as exc:
            message = f"{what}: {type(exc).__name__}: {exc}"
            logger.warning("%s", message)
            if warnings is not None:
                warnings.append(message)
            return None

    def _propagate_remote_silence(
        self,
        *,
        operator: str,
        reason: str | None,
        start_at: datetime,
        end_at: datetime,
        rule_patterns: tuple[str, ...],
        tags: tuple[str, ...],
        warnings: list[str] | None = None,
    ) -> tuple[str, ...]:
        remote_refs: list[str] = []
        seen_rule_ids: set[str] = set()
        for rule in self._matching_rules_for_targets(rule_patterns=rule_patterns, tags=tags):
            if rule.rule_id in seen_rule_ids:
                continue
            seen_rule_ids.add(rule.rule_id)
            remote_silence_id = self._to_peer(
                warnings, f"the silence of '{rule.rule_id}' did not reach its peer",
                getattr(rule, "create_remote_silence", None), self,
                operator=operator, reason=reason, start_at=start_at.isoformat(), end_at=end_at.isoformat(),
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
        warnings: list[str] | None = None,
    ) -> None:
        for ref in remote_silence_refs:
            source_id, _, remote_silence_id = ref.partition(":")
            if not source_id or not remote_silence_id:
                continue
            source = self.sources.get(source_id)
            self._to_peer(
                warnings, f"silence {remote_silence_id} at '{source_id}' was not cancelled (it ends at its end time)",
                getattr(source, "cancel_remote_silence", None), remote_silence_id, operator=operator, reason=reason,
            )

    def _initialize_output(self, output_id: str, output: Output) -> None:
        """init() on the output's thread; after a failure it is tried again while the output is idle."""
        status = self._plugin_status("output", output_id)
        retry = self._output_init_retries.get(output_id)
        try:
            self._call_output_init(output)
            self._set_plugin_ready(status)
            status.init_ok = True
            status.last_error = None
            status.last_error_detail = None
            status.last_success_at = self._now_fn()
            status.last_updated_at = status.last_success_at
            if retry is not None:
                retry.stop()
                logger.info("output '%s' init() succeeded after %d attempt(s)", output_id, retry.attempts + 1)
        except Exception as exc:
            self._set_plugin_failed(status)
            status.init_ok = False
            status.last_error = str(exc)
            status.last_error_detail = traceback.format_exc()
            status.last_failure_at = self._now_fn()
            status.last_updated_at = status.last_failure_at
            worker = current_worker()
            if retry is None and worker is not None:
                retry = _InitRetry(self, output_id, worker)
                self._output_init_retries[output_id] = retry
                worker.add_timer(retry)
                logger.exception("output '%s' init failed; trying again while it is idle", output_id)
            elif retry is not None:
                retry.failed()
                logger.warning("output '%s' init failed again: %s", output_id, exc)
            else:
                logger.exception("output '%s' init failed", output_id)

    def _terminate_output(self, output_id: str, output: Output) -> None:
        try:
            self._call_output_terminate(output)
        except Exception as exc:
            logger.exception("output '%s' terminate failed", output_id)
            # A removed output has no status any more (it runs after the removal, on the
            # output's thread): do not bring one back for it.
            status = self.plugin_states.get(self._plugin_key("output", output_id))
            if status is None:
                return
            self._set_plugin_failed(status)
            status.last_error = str(exc)
            status.last_error_detail = traceback.format_exc()
            status.last_failure_at = self._now_fn()
            status.last_updated_at = status.last_failure_at

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
            self._delivered(output_id, status, event)
            return True

        self._record_output_failure(status, event, last_exc, last_detail)
        logger.error("output '%s' failed: %s", output_id, last_exc)
        return False

    def record_source_failure(
        self,
        source_id: str,
        error: str,
        *,
        now: datetime | None = None,
        detail: str | None = None,
    ) -> None:
        when = now or self._now_fn()
        status = self._plugin_status("source", source_id)
        self._set_plugin_failed(status)
        status.last_error = error
        status.last_error_detail = detail
        status.run_count += 1
        status.last_run_at = when
        status.last_failure_at = when
        status.last_updated_at = when

    def _initialize_source(self, source: Source) -> None:
        status = self._plugin_status("source", source.source_id)
        try:
            self._call_source_init(source)
            source._kanary_initialized = True  # this instance: terminate() is due
            self._set_plugin_ready(status)
            status.init_ok = True
            status.last_error = None
            status.last_error_detail = None
            status.last_success_at = self._now_fn()
            status.last_updated_at = status.last_success_at
        except Exception as exc:
            source._kanary_initialized = False
            self._set_plugin_failed(status)
            status.init_ok = False
            status.last_error = str(exc)
            status.last_error_detail = traceback.format_exc()
            status.last_failure_at = self._now_fn()
            status.last_updated_at = status.last_failure_at
            raise

    def _terminate_source(self, source: Source, *, record: bool = True) -> None:
        """terminate(); a failure is shown on the source's status unless record is False
        (an instance that a reload replaced: the status is its successor's now)."""
        source._kanary_initialized = False
        try:
            self._call_source_terminate(source)
        except Exception as exc:
            # A removed source has no status any more: do not bring one back for it.
            status = self.plugin_states.get(self._plugin_key("source", source.source_id)) if record else None
            if status is not None:
                self._set_plugin_failed(status)
                status.last_error = str(exc)
                status.last_error_detail = traceback.format_exc()
                status.last_failure_at = self._now_fn()
                status.last_updated_at = status.last_failure_at
            raise

    def _rule_context(self, rule: Rule, now: datetime) -> RuleContext:
        last = self._rule_evaluations.get(rule.rule_id)
        return RuleContext(
            now=now,
            source_states=self.source_states,
            declared_inputs=tuple(getattr(rule, "inputs", [])),
            resolved_sources=tuple(getattr(rule, "resolved_sources", [])),
            previous_alert=self.alerts.get(rule.rule_id),
            last_judgment=last.state if last is not None else None,
        )

    def _evaluate_rule(
        self,
        rule: Rule,
        source_payload: dict[str, object],
        source_state: SourceState,
        now: datetime,
    ) -> None:
        self._judge_rule(rule, source_payload, now, source_state=source_state, failure="rule '%s' failed")

    def _evaluate_rule_from_snapshots(
        self,
        rule: Rule,
        source_payload: dict[str, object],
        now: datetime,
    ) -> None:
        self._judge_rule(rule, source_payload, now, source_state=None, failure="timer evaluation for rule '%s' failed")

    def _judge_rule(
        self,
        rule: Rule,
        source_payload: dict[str, object],
        now: datetime,
        *,
        source_state: SourceState | None,
        failure: str,
    ) -> None:
        """Evaluate one rule, unless a depends_on parent holds it, and show the result."""
        status = self._plugin_status("rule", rule.rule_id)
        try:
            if self._holding_parents(rule, rule.depends_on):
                self._apply_blocked(rule, now)
            else:
                evaluation = rule.normalize_evaluation(
                    self._call_rule_evaluate(rule, self._rule_context(rule, now)),
                    source_payload,
                )
                stored_evaluation = self._store_rule_evaluation(rule, evaluation, now)
                closed = self._close_acknowledgement(rule, stored_evaluation)
                self._apply_effective_evaluation(rule, stored_evaluation, now)
                self._record_closed_acknowledgement(rule, closed, now)
            self._set_plugin_ready(status)
            status.init_ok = True
            status.last_error = None
            status.last_error_detail = None
            status.run_count += 1
            status.last_run_at = now
            status.last_success_at = now
            status.last_updated_at = now
        except Exception as exc:
            self._hold_failed_rule(rule, now)
            self._set_plugin_failed(status)
            status.init_ok = True
            status.last_error = str(exc)
            status.last_error_detail = traceback.format_exc()
            status.run_count += 1
            status.last_run_at = now
            status.last_failure_at = now
            status.last_updated_at = now
            logger.exception(failure, rule.rule_id)

    def _evaluate_rule_preview(
        self,
        rule: Rule,
        source_payload: dict[str, object],
        source_state: SourceState,
        now: datetime,
    ) -> Alert:
        blocking = self._holding_parents(rule, rule.depends_on)
        if blocking:
            return Alert(
                rule_id=rule.rule_id,
                state=AlertState.SUPPRESSED,
                severity=rule.severity,
                owner=rule.owner,
                tags=tuple(rule.tags),
                payload=dict(source_payload),
                message=f"blocked by {', '.join(blocking)} (not evaluated)",
            )
        ctx = self._rule_context(rule, now)
        ctx.previous_alert = copy.deepcopy(ctx.previous_alert)  # the live alert stays as it is
        evaluation = rule.normalize_evaluation(rule.evaluate(ctx), source_payload)
        severity = evaluation.severity or rule.severity
        suppressing = self._holding_parents(rule, rule.suppressed_by)
        if suppressing:
            return Alert(
                rule_id=rule.rule_id,
                state=AlertState.SUPPRESSED,
                severity=severity,
                owner=rule.owner,
                tags=tuple(rule.tags),
                payload=evaluation.payload,
                message=_suppressed_message(suppressing, evaluation.state, evaluation.message),
            )
        return self._resolve_operator_state(
            rule,
            evaluation.state,
            evaluation.payload,
            evaluation.message,
            severity,
            now,
        ) or Alert(
            rule_id=rule.rule_id,
            state=evaluation.state,
            severity=severity,
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
