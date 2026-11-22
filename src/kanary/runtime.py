from __future__ import annotations

import ast
from concurrent.futures import Future, TimeoutError as FutureTimeoutError
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import fnmatch
import hashlib
import inspect
import logging
from pathlib import Path
import signal
import threading
import time
import traceback
from typing import Any, Callable

from .api import ControlAPI, PollUnavailableError
from .constants import DEFAULT_VIEWER_TITLE
from .dispatch import SHUTDOWN_DRAIN_SECONDS
from .engine import HISTORY_DRAIN_SECONDS, Engine, source_initialized
from .loader import RuleDirectoryLoader, installed_file
from .models import PluginStatus
from .schedule import compiled_schedule
from .store import build_store

logger = logging.getLogger("kanary.runtime")

DEFAULT_LOG_LEVEL = "INFO"
LOG_LEVEL_CHOICES = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
AUTO_RELOAD_CHOICES = ("off", "dirty", "all")
# How long a test poll requested from the API (kanaryctl test-poll) may take.
TEST_POLL_TIMEOUT_SECONDS = 30.0
# A source whose init() failed is tried again this often at most (or at its interval, if shorter).
SOURCE_INIT_RETRY_SECONDS = 60.0
# How long a reload waits for a new source's init() (on the source's thread) before answering.
SOURCE_INIT_WAIT_SECONDS = 10.0
# How long stopping a source's thread waits for a poll() in progress to return.
SOURCE_STOP_WAIT_SECONDS = 2.0
PLUGIN_TYPES = ("source", "rule", "output")


@dataclass(slots=True)
class RuntimeConfig:
    rule_directories: list[Path]
    reload_interval: float = 1.0
    auto_reload: str = "off"
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    enable_default_viewer: bool = True
    viewer_title: str = DEFAULT_VIEWER_TITLE
    output_emit_enabled: bool = True
    exclude_plugins: list[str] | None = None
    log_level: str = DEFAULT_LOG_LEVEL
    state_db_path: Path | None = None
    node_id: str | None = None


@dataclass(slots=True)
class PluginMetadata:
    plugin_type: str
    plugin_id: str
    definition_file: str | None
    source_hash: str  # its class, with the classes it extends from plugin files
    file_hash: str  # the rest of its file: constants, functions, imports (not the other plugins)
    dependency_files: tuple[str, ...]  # the plugin-directory modules it imports, also indirectly
    dependency_hash: str

    @property
    def signature(self) -> tuple[str, str, str]:
        return (self.source_hash, self.file_hash, self.dependency_hash)


@dataclass(slots=True)
class _ReloadPlan:
    """What a targeted reload does to the plugins of one type."""

    plugin_type: str
    selected: set[str]
    classes: dict[str, type]  # the definitions on disk of the selected plugins
    removed: set[str]  # loaded, and no longer on disk
    instances: dict[str, object] = field(default_factory=dict)  # created: these replace the running ones
    errors: dict[str, str] = field(default_factory=dict)  # not applied: the running one goes on
    init_failed: dict[str, str] = field(default_factory=dict)  # applied, but init() failed (it keeps trying)

    def summary(self) -> dict[str, Any]:
        return {
            "matched": sorted(self.selected),
            "reloaded": sorted(self.instances),
            "removed": sorted(self.removed),
            "failed": sorted(self.errors),
            "init_failed": sorted(self.init_failed),
            "errors": {
                **{plugin_id: self.errors[plugin_id] for plugin_id in sorted(self.errors)},
                **{plugin_id: f"init() failed: {self.init_failed[plugin_id]}" for plugin_id in sorted(self.init_failed)},
            },
        }


class EngineRuntime:
    def __init__(self, config: RuntimeConfig) -> None:
        self.config = config
        self.loader = RuleDirectoryLoader(config.rule_directories)
        self.store = build_store(config.state_db_path)
        self._stop_event = threading.Event()
        self._reload_thread: threading.Thread | None = None
        self._api_thread: threading.Thread | None = None
        self._silence_thread: threading.Thread | None = None
        self._rule_timer_thread: threading.Thread | None = None
        self._silence_wakeup = threading.Event()
        self._source_threads: dict[str, threading.Thread] = {}
        self._source_stop_events: dict[str, threading.Event] = {}
        self._source_wake_events: dict[str, threading.Event] = {}
        self._source_init_done: dict[str, threading.Event] = {}  # set once a thread's first init() ended
        self._test_poll_requests: dict[str, list[Future]] = {}
        self._test_poll_lock = threading.Lock()
        self._reload_lock = threading.RLock()
        self.engine: Engine | None = None
        self._signature: tuple[tuple[str, int], ...] = ()
        self._discovered_snapshot = None
        self._discovered_metadata: dict[tuple[str, str], PluginMetadata] = {}
        self._loaded_metadata: dict[tuple[str, str], PluginMetadata] = {}
        self._untracked_files: list[str] = []
        self._last_loader_error: str | None = None
        self._failed_signature: tuple[tuple[str, int], ...] | None = None  # the files that failed to load last
        self._last_loader_error_at: datetime | None = None
        self.api = ControlAPI(
            engine_getter=lambda: self.engine,
            reload_callback=self.reload_now,
            meta_getter=self.meta_payload,
            test_poll_callback=self.test_poll,
            host=config.api_host,
            port=config.api_port,
            enable_default_viewer=config.enable_default_viewer,
            viewer_title=config.viewer_title,
        )

    def start(self) -> None:
        snapshot = self.loader.load(exclude_patterns=self.config.exclude_plugins)
        self._last_loader_error = None
        self._last_loader_error_at = None
        self._signature = self.loader.snapshot_signature()
        self._discovered_snapshot = snapshot
        self._discovered_metadata = self._collect_plugin_metadata(snapshot)
        self._loaded_metadata = dict(self._discovered_metadata)
        self.engine = Engine(
            source_registry=snapshot.sources,
            rule_registry=snapshot.rules,
            output_registry=snapshot.outputs,
            store=self.store,
            node_id=self.config.node_id,
            output_emit_enabled=self.config.output_emit_enabled,
            silence_changed_callback=self._wake_silence_scheduler,
        )
        self.engine.start(init_sources=False)  # each source's thread calls its init()
        self._publish_runtime_plugin_overlay()
        logger.info("engine started with %d sources, %d rules, %d outputs", len(self.engine.sources), len(self.engine.rules), len(self.engine.outputs))
        self._sync_source_threads()
        self._rule_timer_thread = threading.Thread(target=self._rule_timer_loop, daemon=True)
        self._rule_timer_thread.start()
        self._silence_thread = threading.Thread(target=self._silence_boundary_loop, daemon=True)
        self._silence_thread.start()
        self._api_thread = threading.Thread(target=self.api.start, daemon=True)
        self._api_thread.start()
        logger.info("control API listening on %s:%d", self.config.api_host, self.config.api_port)
        self._reload_thread = threading.Thread(target=self._watch_reload_loop, daemon=True)
        self._reload_thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._silence_wakeup.set()
        self.api.shutdown()
        if self._api_thread is not None:
            self._api_thread.join(timeout=2.0)
        if self._reload_thread is not None:
            self._reload_thread.join(timeout=2.0)
        if self._silence_thread is not None:
            self._silence_thread.join(timeout=2.0)
        if self._rule_timer_thread is not None:
            self._rule_timer_thread.join(timeout=2.0)
        for stop_event in self._source_stop_events.values():
            stop_event.set()
        for wake_event in self._source_wake_events.values():
            wake_event.set()
        for thread in self._source_threads.values():
            thread.join(timeout=SOURCE_STOP_WAIT_SECONDS)
        if self.engine is not None:
            # Each source's thread terminated its own source (or does once its poll() returns).
            self.engine.shutdown(terminate_sources=False)
        logger.info("engine stopped")

    def run_forever(self) -> None:
        """Run until Ctrl-C (SIGINT) or SIGTERM (systemctl stop, kill), then stop cleanly.

        Both end the same way: sources and outputs are terminated, and queued
        output events get SHUTDOWN_DRAIN_SECONDS to go out.
        """
        restore = self._stop_on_sigterm()
        try:
            self.start()
            try:
                while not self._stop_event.is_set():
                    self._stop_event.wait(0.5)
            except KeyboardInterrupt:
                pass
            finally:
                self.stop()
        finally:
            restore()

    def _stop_on_sigterm(self) -> Callable[[], None]:
        """Turn SIGTERM into a clean stop; returns a function that puts the old handler back."""
        if threading.current_thread() is not threading.main_thread():
            return lambda: None  # signal handlers can only be set from the main thread

        def stop(signum: int, frame: Any) -> None:
            logger.info("received SIGTERM, stopping")
            self._stop_event.set()

        previous = signal.signal(signal.SIGTERM, stop)
        return lambda: signal.signal(signal.SIGTERM, previous)

    def _watch_reload_loop(self) -> None:
        while not self._stop_event.wait(self.config.reload_interval):
            try:
                self.reload_now_if_changed()
            except Exception:
                # The watch must go on: a file renamed during the scan, a bug in a reload.
                logger.exception("watching the plugin directories failed; trying again")

    def _wake_silence_scheduler(self) -> None:
        self._silence_wakeup.set()

    def _silence_boundary_loop(self) -> None:
        while not self._stop_event.is_set():
            engine = self.engine
            if engine is None:
                return
            self._silence_wakeup.clear()
            boundary = engine.next_silence_boundary()
            timeout = None
            if boundary is not None:
                timeout = max(0.0, (boundary - datetime.now().astimezone()).total_seconds())
            if self._silence_wakeup.wait(timeout):
                continue
            if self._stop_event.is_set():
                return
            engine.reconcile_silences()

    def reload_now_if_changed(self) -> bool:
        with self._reload_lock:
            return self._reload_if_changed()

    def _reload_if_changed(self) -> bool:
        new_signature = self.loader.snapshot_signature()
        if new_signature == self._signature:
            return True
        if new_signature == self._failed_signature:
            return False  # these files failed to load already; wait for the next change
        refreshed = self._refresh_discovery(expected_signature=new_signature)
        if not refreshed:
            return False
        if self.config.auto_reload == "dirty":
            self._apply_reload_request({"target": "dirty"})
        elif self.config.auto_reload == "all":
            self._apply_reload_request({"target": "all"})
        return True

    def reload_now(
        self,
        request: dict[str, Any] | None = None,
        expected_signature: tuple[tuple[str, int], ...] | None = None,
    ) -> dict[str, Any] | bool:
        # One reload at a time: an operator's, another operator's, and auto-reload would
        # otherwise stop and start the same sources' threads under each other.
        with self._reload_lock:
            return self._reload(request, expected_signature)

    def _reload(
        self,
        request: dict[str, Any] | None,
        expected_signature: tuple[tuple[str, int], ...] | None,
    ) -> dict[str, Any] | bool:
        force_refresh = request is None
        if request is not None:
            target, _ = _parse_reload_request(request)
            force_refresh = target in {"all", "full"}
        if (
            force_refresh
            or expected_signature is not None
            or self.loader.snapshot_signature() != self._signature
        ):
            refreshed = self._refresh_discovery(expected_signature=expected_signature)
            if refreshed is False:
                # The plugin files did not load or validate: nothing was applied.
                failed = {"status": "reload_failed", "error": self._last_loader_error or "the engine is not running"}
                return failed if request is not None else False
        if request is None:
            return self._apply_reload_request({"target": "all"})
        return self._apply_reload_request(request)

    def _refresh_discovery(self, expected_signature: tuple[tuple[str, int], ...] | None = None) -> bool:
        new_signature = expected_signature or self.loader.snapshot_signature()
        try:
            snapshot = self.loader.load(exclude_patterns=self.config.exclude_plugins)
        except Exception as exc:
            self._last_loader_error = str(exc)
            self._last_loader_error_at = datetime.now(timezone.utc)
            self._failed_signature = new_signature
            logger.exception("reload failed while loading rule directory")
            return False
        self._failed_signature = None
        self._last_loader_error = None
        self._last_loader_error_at = None
        changed_files = _changed_files(self._signature, new_signature)
        self._signature = new_signature
        if self.engine is None:
            return False
        self._discovered_snapshot = snapshot
        self._discovered_metadata = self._collect_plugin_metadata(snapshot)
        self._untracked_files = self._compute_untracked_files(changed_files)
        self._publish_runtime_plugin_overlay()
        logger.info(
            "plugin scan: changed_files=%s dirty_rules=%d dirty_sources=%d dirty_outputs=%d untracked_files=%d",
            ",".join(changed_files) or "-",
            self._count_state("rule", "DIRTY"),
            self._count_state("source", "DIRTY"),
            self._count_state("output", "DIRTY"),
            len(self._untracked_files),
        )
        return True

    def meta_payload(self) -> dict[str, object]:
        return {
            "plugin_load_error": self._last_loader_error,
            "plugin_load_error_at": self._last_loader_error_at,
        }

    def _apply_reload_request(self, request: dict[str, Any]) -> dict[str, Any]:
        if self.engine is None or self._discovered_snapshot is None:
            return {"status": "reload_failed"}
        target, pattern = _parse_reload_request(request)
        if target == "full":
            return self._restart_engine_full()
        engine = self.engine
        plans = {plugin_type: self._reload_plan(plugin_type, target=target, pattern=pattern) for plugin_type in PLUGIN_TYPES}
        for plan in plans.values():
            # Every replacement is created before anything changes; one that cannot be
            # created is not applied, and its running instance goes on.
            plan.instances, plan.errors = engine.prepare_plugins(plan.plugin_type, plan.classes)
            for plugin_id in plan.selected:
                engine._plugin_status(plan.plugin_type, plugin_id).state = "RELOADING"

        # Sources first (rules resolve against them), then outputs, then rules, while
        # evaluations wait: a value read meanwhile, by a new source or an old one, is judged
        # once all of them are in place, so its alert also reaches an output added now.
        waits: list[Callable[[], None]] = []
        with engine.holding_evaluations():
            self._apply_plan(plans["source"], self._reload_sources)
            self._apply_plan(
                plans["output"],
                lambda plan: engine.reload_output_plugins(
                    replacements={output_id: plan.classes[output_id] for output_id in plan.instances},
                    removed_output_ids=plan.removed,
                    prepared=plan.instances,
                    after=waits,
                ),
            )
            self._apply_plan(
                plans["rule"],
                lambda plan: engine.reload_rule_plugins(
                    replacements={rule_id: plan.classes[rule_id] for rule_id in plan.instances},
                    removed_rule_ids=plan.removed,
                    prepared=plan.instances,
                    evaluate=False,
                ),
            )
        for wait in waits:  # removed outputs stopping, new outputs' init()
            wait()
        self._collect_init_failures(plans["source"], plans["output"])
        try:
            engine.evaluate_reloaded_rules(plans["rule"].instances)
        except Exception:
            logger.exception("evaluating the reloaded rules failed")

        for plan in plans.values():
            self._finish_plugin_reload(plan, target=target, pattern=pattern)
        self._publish_runtime_plugin_overlay()
        return _reload_summary(target, pattern, plans)

    def _reload_plan(self, plugin_type: str, *, target: str, pattern: str | None) -> _ReloadPlan:
        selected = self._select_plugin_ids(plugin_type, target=target, pattern=pattern)
        classes = {
            plugin_id: cls
            for plugin_id in selected
            if (cls := self._plugin_class_from_snapshot(plugin_type, plugin_id)) is not None
        }
        loaded_ids = {plugin_id for kind, plugin_id in self._loaded_metadata if kind == plugin_type}
        removed = {plugin_id for plugin_id in selected if plugin_id in loaded_ids and plugin_id not in classes}
        return _ReloadPlan(plugin_type=plugin_type, selected=selected, classes=classes, removed=removed)

    def _apply_plan(self, plan: _ReloadPlan, apply: Callable[[_ReloadPlan], Any]) -> None:
        if not plan.instances and not plan.removed:
            return
        try:
            apply(plan)
        except Exception as exc:
            logger.exception("reload of %ss failed", plan.plugin_type)
            for plugin_id in set(plan.instances) | plan.removed:
                plan.errors[plugin_id] = f"{type(exc).__name__}: {exc}"
            plan.instances = {}
            plan.removed = set()

    def _reload_sources(self, plan: _ReloadPlan) -> None:
        """Swap the sources on their threads: the old thread terminates its instance, a new one inits the new."""
        assert self.engine is not None
        engine = self.engine
        changing = set(plan.instances) | plan.removed
        unthreaded = {
            source_id: source
            for source_id in changing
            if source_id not in self._source_threads and (source := engine.sources.get(source_id)) is not None
        }
        self._stop_source_threads(changing)
        for source_id, source in unthreaded.items():  # an engine started without its threads (tests)
            self._retire_source(engine, source_id, source)
        try:
            engine.reload_source_plugins(
                replacements={source_id: plan.classes[source_id] for source_id in plan.instances},
                removed_source_ids=plan.removed,
                prepared=plan.instances,
                start_sources=False,
            )
        finally:
            self._start_source_threads({source_id for source_id in changing if source_id in engine.sources})

    def _collect_init_failures(self, sources: _ReloadPlan, outputs: _ReloadPlan) -> None:
        """Note the new sources and outputs whose first init() failed (they keep trying it)."""
        assert self.engine is not None
        deadline = time.monotonic() + SOURCE_INIT_WAIT_SECONDS
        for source_id, source in sources.instances.items():
            done = self._source_init_done.get(source_id)
            if done is not None:
                done.wait(max(0.0, deadline - time.monotonic()))
            if self.engine.sources.get(source_id) is source and not source_initialized(source):
                status = self.engine._plugin_status("source", source_id)
                sources.init_failed[source_id] = status.last_error or f"not done within {SOURCE_INIT_WAIT_SECONDS:.0f} s"
        for output_id in outputs.instances:
            status = self.engine._plugin_status("output", output_id)
            if not status.init_ok:
                outputs.init_failed[output_id] = status.last_error or "not done yet"

    def _finish_plugin_reload(self, plan: _ReloadPlan, *, target: str, pattern: str | None) -> None:
        assert self.engine is not None
        plugin_type = plan.plugin_type
        for plugin_id in plan.removed:
            self._loaded_metadata.pop((plugin_type, plugin_id), None)
            self.engine.plugin_states.pop(self.engine._plugin_key(plugin_type, plugin_id), None)
        for plugin_id in plan.instances:
            metadata = self._discovered_metadata.get((plugin_type, plugin_id))
            if metadata is not None:
                self._loaded_metadata[(plugin_type, plugin_id)] = metadata
        loaded_ids = self._plugin_ids_for_type(self.engine.plugin_states, plugin_type)
        for plugin_id in plan.selected - plan.removed:
            status = self.engine._plugin_status(plugin_type, plugin_id)
            status.reload_error = plan.errors.get(plugin_id)
            status.loaded = plugin_id in loaded_ids
            if plugin_id not in plan.errors:
                status.dirty_reason = None
            metadata = self._discovered_metadata.get((plugin_type, plugin_id)) or self._loaded_metadata.get((plugin_type, plugin_id))
            if metadata is not None:
                status.definition_file = metadata.definition_file
            if status.state == "RELOADING":
                status.state = _settled_state(status)
        logger.info(
            "reload applied: target=%s pattern=%s type=%s matched=%s reloaded=%s removed=%s failed=%s init_failed=%s",
            target,
            pattern or "-",
            plugin_type,
            ",".join(sorted(plan.selected)) or "-",
            ",".join(sorted(plan.instances)) or "-",
            ",".join(sorted(plan.removed)) or "-",
            ",".join(sorted(plan.errors)) or "-",
            ",".join(sorted(plan.init_failed)) or "-",
        )

    def _restart_engine_full(self) -> dict[str, Any]:
        if self.engine is None or self._discovered_snapshot is None:
            return {"status": "reload_failed", "target": "full"}

        old_engine = self.engine
        new_store = build_store(self.config.state_db_path)
        # A plugin whose init() fails does not stop the restart: its thread keeps trying
        # (a restart is most wanted while a database is down). What stops one is a plugin
        # that cannot even be created (its class or __init__ fails): then nothing changes.
        try:
            new_engine = Engine(
                source_registry=self._discovered_snapshot.sources,
                rule_registry=self._discovered_snapshot.rules,
                output_registry=self._discovered_snapshot.outputs,
                store=new_store,
                node_id=self.config.node_id,
                output_emit_enabled=self.config.output_emit_enabled,
                silence_changed_callback=self._wake_silence_scheduler,
            )
        except Exception as exc:
            logger.exception("full engine restart failed")
            return {"status": "reload_failed", "target": "full", "error": f"{type(exc).__name__}: {exc}"}

        old_source_ids = set(self._source_threads)
        unthreaded = {source_id: source for source_id, source in old_engine.sources.items() if source_id not in old_source_ids}

        def give_up(reason: str) -> dict[str, Any]:
            """The old engine goes on as it was: its sources' threads never stopped."""
            try:
                new_engine.shutdown(terminate_sources=False)
            except Exception:
                pass
            old_engine.thaw()
            logger.error("full engine restart not done; the running engine goes on: %s", reason)
            return {"status": "reload_failed", "target": "full", "error": reason}

        # The new engine starts from what the old one saved. From here on the old one
        # changes nothing: no evaluations (its sources go on polling, and what they read
        # waits), no acks or silences (the API answers 503 and asks to try again), so
        # what it saved is all it did. Its source threads stop only once the new engine
        # runs: a restart given up leaves them as they were.
        old_engine.freeze()
        with old_engine.holding_evaluations():
            # Every event of the old engine goes out (or fails) first. One still being sent
            # after the switch could reach its receiver after the new engine's next event
            # of the rule (its FIRING after the OK): the restart waits for the destination
            # instead, and is given up while it does not answer.
            if not old_engine.flush_outputs(timeout=SHUTDOWN_DRAIN_SECONDS):
                busy = ", ".join(sorted(old_engine.busy_outputs())) or "an output"
                return give_up(
                    f"output {busy} still sending after {SHUTDOWN_DRAIN_SECONDS:.0f} s; "
                    "try again once it has sent (or restart KANARY)"
                )
            old_engine.save_latest_judgments()  # the values each alert last had come along
            if not old_engine.flush_history(timeout=HISTORY_DRAIN_SECONDS):
                # Restored without them, the new engine would send again what was sent.
                return give_up(
                    f"the state database did not take the running engine's last writes within "
                    f"{HISTORY_DRAIN_SECONDS:.0f} s ({old_engine.state_db_error or 'still writing'})"
                )
            try:
                new_engine.start(init_sources=False)
            except Exception as exc:
                logger.exception("full engine restart failed")
                return give_up(f"{type(exc).__name__}: {exc}")
            # What is gone from the plugins is removed as a reload removes it: an ack of a
            # removed rule does not come back with a rule of the same id later.
            removed = new_engine.forget_removed_plugins(old_engine)
            self._stop_source_threads(old_source_ids)  # each thread terminates its own source
            for source_id, source in unthreaded.items():
                self._retire_source(old_engine, source_id, source)
            try:
                old_engine.shutdown(terminate_sources=False)  # closed: nothing evaluates it any more
            except Exception:
                logger.exception("old engine shutdown failed after full restart")

        self.engine = new_engine
        self.store = new_store
        self._loaded_metadata = dict(self._discovered_metadata)
        self.engine.last_reload_at = self.engine._now_fn()
        self._publish_runtime_plugin_overlay()
        self._sync_source_threads()
        self._wake_silence_scheduler()
        logger.info(
            "engine full restart applied: sources=%d, rules=%d, outputs=%d",
            len(self.engine.sources),
            len(self.engine.rules),
            len(self.engine.outputs),
        )
        everything = {
            plugin_type: {
                "matched": sorted(set(registry) | set(removed[plugin_type])),
                "reloaded": sorted(registry),
                "removed": removed[plugin_type],
                "failed": [],
                "init_failed": [],
                "errors": {},
            }
            for plugin_type, registry in (
                ("sources", self.engine.sources),
                ("rules", self.engine.rules),
                ("outputs", self.engine.outputs),
            )
        }
        return {"status": "reloaded", "target": "full", "pattern": None, **everything}

    def _sync_source_threads(self) -> None:
        """Give every source of the engine a thread (at start, and after a full restart)."""
        if self.engine is None:
            return
        gone = set(self._source_threads) - set(self.engine.sources)
        self._stop_source_threads(gone)
        self._start_source_threads(set(self.engine.sources))

    def _stop_source_threads(self, source_ids: set[str]) -> None:
        """Stop these sources' threads; each one terminates its own source on its way out.

        A thread still in poll() after SOURCE_STOP_WAIT_SECONDS is left to finish: it
        terminates its source once poll() returns, never while poll() runs.
        """
        stopping: list[tuple[str, threading.Thread]] = []
        for source_id in set(source_ids):
            stop_event = self._source_stop_events.pop(source_id, None)
            thread = self._source_threads.pop(source_id, None)
            wake_event = self._source_wake_events.pop(source_id, None)
            self._source_init_done.pop(source_id, None)
            if stop_event is None or thread is None:
                continue
            stop_event.set()
            if wake_event is not None:
                wake_event.set()
            stopping.append((source_id, thread))
        deadline = time.monotonic() + SOURCE_STOP_WAIT_SECONDS
        for source_id, thread in stopping:
            thread.join(max(0.0, deadline - time.monotonic()))
            if thread.is_alive():
                logger.warning(
                    "source '%s' is still in poll(); its old instance is terminated on its own thread when poll() returns",
                    source_id,
                )
            self._fail_test_polls(source_id, PollUnavailableError(f"source '{source_id}' was stopped"))

    def _start_source_threads(self, source_ids: set[str]) -> None:
        if self.engine is None:
            return
        for source_id in set(source_ids):
            if source_id not in self.engine.sources or source_id in self._source_threads:
                continue
            stop_event = threading.Event()
            wake_event = threading.Event()
            init_done = threading.Event()
            thread = threading.Thread(
                target=self._source_loop,
                args=(source_id, stop_event, wake_event, init_done),
                name=f"kanary-source-{source_id}",
                daemon=True,
            )
            self._source_stop_events[source_id] = stop_event
            self._source_wake_events[source_id] = wake_event
            self._source_init_done[source_id] = init_done
            self._source_threads[source_id] = thread
            thread.start()

    def _source_is_initialized(self, source_id: str) -> bool:
        source = self.engine.sources.get(source_id) if self.engine is not None else None
        return source is not None and source_initialized(source)

    def test_poll(self, source_id: str, timeout: float = TEST_POLL_TIMEOUT_SECONDS) -> dict[str, object]:
        """Run the source's test_poll() on its own thread, between its polls.

        The result is only returned: it is not evaluated, the source's state and
        status do not change, and its regular polls keep their schedule.
        """
        engine = self.engine
        if engine is None:
            raise PollUnavailableError("the engine is starting")
        if source_id not in engine.sources:
            raise KeyError(source_id)
        if not self._source_is_initialized(source_id):
            error = engine._plugin_status("source", source_id).last_error or "not yet"
            raise PollUnavailableError(f"source '{source_id}' is not running: its init() failed ({error}); KANARY keeps trying it")
        future: Future = Future()
        with self._test_poll_lock:
            wake_event = self._source_wake_events.get(source_id)
            if wake_event is None or source_id not in self._source_threads:
                raise PollUnavailableError(f"source '{source_id}' is not running (its init() failed, or it is stopped)")
            self._test_poll_requests.setdefault(source_id, []).append(future)
            wake_event.set()
        try:
            return future.result(timeout)
        except FutureTimeoutError:
            with self._test_poll_lock:
                waiting = self._test_poll_requests.get(source_id, [])
                if future in waiting:
                    waiting.remove(future)
            raise PollUnavailableError(f"source '{source_id}' did not finish a test poll within {timeout:.0f} s") from None

    def _serve_test_polls(self, engine: Engine, source_id: str, source: Any) -> None:
        """Answer waiting test-poll requests; called on the source's thread between polls."""
        with self._test_poll_lock:
            futures = self._test_poll_requests.pop(source_id, [])
        if not futures:
            return
        now = datetime.now().astimezone()
        try:
            result = engine._call_source_test_poll(source, now=now)
            payload = engine._test_poll_output(source_id, result, now=now)
        except Exception as exc:
            for future in futures:
                future.set_exception(exc)
            return
        for future in futures:
            future.set_result(payload)

    def _fail_test_polls(self, source_id: str, error: BaseException) -> None:
        with self._test_poll_lock:
            futures = self._test_poll_requests.pop(source_id, [])
        for future in futures:
            future.set_exception(error)

    def _source_loop(
        self,
        source_id: str,
        stop_event: threading.Event,
        wake_event: threading.Event,
        init_done: threading.Event,
    ) -> None:
        """One source instance's life: init(), poll() at its interval, terminate(), all on this thread.

        A reload or a full restart stops this thread and starts a new one for the new
        instance, so a connection that init() made is only ever used and closed here.
        """
        engine = self.engine
        source = engine.sources.get(source_id) if engine is not None else None
        if engine is None or source is None:
            init_done.set()
            return

        def stopped() -> bool:
            return stop_event.is_set() or self._stop_event.is_set()

        try:
            self._run_source(engine, source_id, source, stopped, stop_event, wake_event, init_done)
        except Exception as exc:
            # Not a failed poll (those are handled in the loop): the thread itself cannot go
            # on. Show it, rather than leaving a source that looks READY and never polls.
            logger.exception("source '%s' stopped polling", source_id)
            if engine.sources.get(source_id) is source:
                engine.record_source_failure(
                    source_id, f"the source stopped polling: {type(exc).__name__}: {exc}", detail=traceback.format_exc()
                )
        finally:
            init_done.set()
            self._retire_source(engine, source_id, source)

    def _run_source(self, engine: Engine, source_id: str, source: Any, stopped, stop_event, wake_event, init_done) -> None:
        next_run_at: datetime | None = None
        schedule = compiled_schedule(source)
        never = schedule is not None and schedule.never
        engine._plugin_status("source", source_id).warning = (
            f"schedule {schedule.expression!r} never runs (no such day): not polled" if never else None
        )
        while not stopped():
            wake_event.clear()
            if engine.sources.get(source_id) is not source:
                return  # replaced or removed
            if not source_initialized(source):
                initialized = self._init_source(engine, source_id, source, stop_event)
                init_done.set()
                if not initialized:
                    # Its database or device may still be starting (after a power cut, say): try again.
                    retry_at = time.monotonic() + min(float(source.interval or SOURCE_INIT_RETRY_SECONDS), SOURCE_INIT_RETRY_SECONDS)
                    while not stopped() and time.monotonic() < retry_at:
                        wake_event.wait(retry_at - time.monotonic())
                        wake_event.clear()
                    continue
            init_done.set()
            now = datetime.now().astimezone()
            if never:
                # A schedule such as "0 0 31 2 *" (cron's way to turn a job off): never polled;
                # test polls are still answered.
                self._serve_test_polls(engine, source_id, source)
                wake_event.wait()
                continue
            if schedule is not None:
                if next_run_at is None:
                    next_run_at = _initial_schedule_run_at(schedule, now)
                if now < next_run_at:
                    self._serve_test_polls(engine, source_id, source)
                    wake_event.wait((next_run_at - now).total_seconds())  # a test poll wakes it early
                    continue
            try:
                payload = self._poll_source_with_recovery(engine, source_id, source, now, stop_event)
                if stopped():
                    return  # stopped or reloaded while poll() was running
                # poll() may have waited for data, so stamp the result when it returned.
                observed_at = datetime.now().astimezone()
                engine.evaluate_source(source_id, payload, now=observed_at, cancelled=stopped, source=source)
            except Exception:
                logger.exception("source '%s' failed", source_id)
            self._serve_test_polls(engine, source_id, source)  # requests that came during the poll
            if schedule is not None:
                next_run_at = schedule.next_after(now)
                continue
            # Wait for the next poll; a test poll is served in between without moving it.
            next_poll_at = time.monotonic() + float(source.interval)
            while not stopped():
                remaining = next_poll_at - time.monotonic()
                if remaining <= 0:
                    break
                wake_event.wait(remaining)
                wake_event.clear()
                self._serve_test_polls(engine, source_id, source)

    def _retire_source(self, engine: Engine, source_id: str, source: Any) -> None:
        """terminate() an instance whose init() succeeded; its thread has stopped polling it."""
        if not source_initialized(source):
            return
        try:
            # A failure is shown on the status only while it is this instance's status.
            engine._terminate_source(source, record=engine.sources.get(source_id) is source)
        except Exception:
            logger.exception("source '%s' terminate failed", source_id)

    def _rule_timer_loop(self) -> None:
        deadlines: dict[str, float] = {}
        scheduled_intervals: dict[str, float] = {}
        while not self._stop_event.wait(0.1):
            engine = self.engine
            if engine is None:
                continue
            monotonic_now = time.monotonic()
            configured: dict[str, float] = {}
            for rule_id, rule in list(engine.rules.items()):
                interval = getattr(rule, "evaluation_interval", None)
                if interval is not None:
                    configured[rule_id] = float(interval)
            for rule_id in list(deadlines):
                if rule_id not in configured:
                    deadlines.pop(rule_id, None)
                    scheduled_intervals.pop(rule_id, None)
            for rule_id, interval in configured.items():
                if scheduled_intervals.get(rule_id) != interval:
                    scheduled_intervals[rule_id] = interval
                    deadlines[rule_id] = monotonic_now + interval
                deadline = deadlines.setdefault(rule_id, monotonic_now + interval)
                if monotonic_now < deadline:
                    continue
                missed = int((monotonic_now - deadline) // interval) + 1
                deadlines[rule_id] = deadline + missed * interval
                try:
                    engine.evaluate_rule_timer(rule_id)
                except Exception:
                    logger.exception("scheduled evaluation for rule '%s' failed", rule_id)

    def _init_source(self, engine: Engine, source_id: str, source, stop_event: threading.Event) -> bool:
        """init() on the source's thread, with the reinit attempts of a failed poll; True when it worked."""
        for attempt in range(1 + max(0, int(getattr(source, "max_reinit", 1)))):
            if attempt and (stop_event.wait(attempt ** 2) or self._stop_event.is_set()):
                return False
            try:
                engine._initialize_source(source)
            except Exception as exc:
                (logger.exception if attempt == 0 else logger.warning)(
                    "source '%s' init failed: %s", source_id, exc
                )
                continue
            if attempt:
                logger.info("source '%s' init() succeeded on attempt %d", source_id, attempt + 1)
            return True
        return False

    def _poll_source_with_recovery(
        self,
        engine: Engine,
        source_id: str,
        source,
        now: datetime,
        stop_event: threading.Event,
    ):
        """poll(); after an exception, retry max_retry times, then terminate() + init() + poll() max_reinit times.

        Only an exception from poll() (a lost connection, say) is retried. A result that
        breaks the poll() contract is reported by evaluate_source() and not retried.
        """
        last_exc: Exception | None = None
        attempt = 0

        try:
            return engine._call_source_poll(source, now=now)
        except Exception as exc:
            last_exc = exc

        for _ in range(getattr(source, "max_retry", 1)):
            attempt += 1
            if stop_event.wait(attempt ** 2) or self._stop_event.is_set():
                raise RuntimeError("source polling interrupted during recovery")
            try:
                return engine._call_source_poll(source, now=now)
            except Exception as exc:
                last_exc = exc

        for _ in range(getattr(source, "max_reinit", 1)):
            attempt += 1
            if stop_event.wait(attempt ** 2) or self._stop_event.is_set():
                raise RuntimeError("source polling interrupted during recovery")
            if source_initialized(source):
                try:
                    engine._terminate_source(source)
                except Exception:
                    pass
            try:
                engine._initialize_source(source)
            except Exception as exc:
                last_exc = exc
                continue
            try:
                return engine._call_source_poll(source, now=now)
            except Exception as exc:
                last_exc = exc

        assert last_exc is not None
        if stop_event.is_set():
            raise RuntimeError("source polling interrupted during recovery")  # a reload replaced it meanwhile
        engine.record_source_failure(source_id, str(last_exc), now=now)
        raise last_exc

    def _collect_plugin_metadata(self, snapshot) -> dict[tuple[str, str], PluginMetadata]:
        """What each plugin's code is made of, to tell which ones an edit changed: its class,
        the rest of its file (a LIMIT at the top of it), and the helper modules it imports,
        also through other helpers."""
        registries = (("source", snapshot.sources), ("rule", snapshot.rules), ("output", snapshot.outputs))
        dependencies_of = _build_dependency_map(self.loader.rule_directories)
        plugin_classes: dict[str, set[str]] = {}  # file -> names of the plugin classes in it
        for _plugin_type, registry in registries:
            for plugin_cls in registry.values():
                definition_file = getattr(plugin_cls, "__kanary_definition_file__", None)
                if definition_file:
                    plugin_classes.setdefault(_file_key(definition_file), set()).add(plugin_cls.__name__)
        file_hashes: dict[str, str] = {}
        metadata: dict[tuple[str, str], PluginMetadata] = {}
        for plugin_type, registry in registries:
            for plugin_id, plugin_cls in registry.items():
                definition_file = getattr(plugin_cls, "__kanary_definition_file__", None)
                key = _file_key(definition_file) if definition_file else None
                dependencies = tuple(sorted(dependencies_of.get(key, ()))) if key else ()
                if key is not None and key not in file_hashes:
                    file_hashes[key] = _hash_file_around_plugins(key, plugin_classes.get(key, set()))
                metadata[(plugin_type, plugin_id)] = PluginMetadata(
                    plugin_type=plugin_type,
                    plugin_id=plugin_id,
                    definition_file=str(definition_file) if definition_file else None,
                    source_hash=_hash_text(_class_sources(plugin_cls)),
                    file_hash=file_hashes.get(key, "") if key else "",
                    dependency_files=dependencies,
                    dependency_hash=_hash_dependencies(dependencies),
                )
        return metadata

    def _publish_runtime_plugin_overlay(self) -> None:
        if self.engine is None:
            return
        self.engine.runtime_untracked_files = list(self._untracked_files)
        discovered_classes: dict[tuple[str, str], type[Any]] = {}
        if self._discovered_snapshot is not None:
            for plugin_type, registry in (
                ("source", self._discovered_snapshot.sources),
                ("rule", self._discovered_snapshot.rules),
                ("output", self._discovered_snapshot.outputs),
            ):
                for plugin_id, plugin_cls in registry.items():
                    discovered_classes[(plugin_type, plugin_id)] = plugin_cls
        self.engine.runtime_discovered_plugin_classes = discovered_classes

        all_keys = set(self._loaded_metadata) | set(self._discovered_metadata)
        for plugin_type, plugin_id in sorted(all_keys):
            loaded = self._loaded_metadata.get((plugin_type, plugin_id))
            discovered = self._discovered_metadata.get((plugin_type, plugin_id))
            status = self.engine._plugin_status(plugin_type, plugin_id)
            status.definition_file = (
                discovered.definition_file if discovered is not None else loaded.definition_file if loaded is not None else None
            )
            if loaded is None and discovered is not None:
                status.state = "DISCOVERED"
                status.loaded = False
                status.dirty_reason = "added"
                continue
            if loaded is not None and discovered is None:
                status.state = "PENDING_REMOVE"
                status.loaded = True
                status.dirty_reason = "removed"
                continue
            if loaded is not None and discovered is not None and loaded.signature != discovered.signature:
                status.state = "DIRTY"
                status.loaded = True
                same_file = (loaded.source_hash, loaded.file_hash) == (discovered.source_hash, discovered.file_hash)
                status.dirty_reason = "dependency_changed" if same_file else "definition_changed"
                continue
            status.loaded = loaded is not None
            status.dirty_reason = None
            if status.state in {"DISCOVERED", "DIRTY", "PENDING_REMOVE", "RELOADING"}:
                status.state = _settled_state(status)

    def _compute_untracked_files(self, changed_files: list[str]) -> list[str]:
        tracked_files: set[str] = set()
        for metadata in list(self._loaded_metadata.values()) + list(self._discovered_metadata.values()):
            if metadata.definition_file:
                tracked_files.add(_file_key(metadata.definition_file))
            tracked_files.update(metadata.dependency_files)  # resolved already
        return sorted(path for path in changed_files if _file_key(path) not in tracked_files)

    def _select_plugin_ids(self, plugin_type: str, *, target: str, pattern: str | None) -> set[str]:
        if self.engine is None:
            return set()
        if target == "dirty":
            return {
                status.plugin_id
                for status in self.engine.plugin_states.values()
                if status.plugin_type == plugin_type and status.state in {"DISCOVERED", "DIRTY", "PENDING_REMOVE"}
            }
        union_ids = {plugin_id for kind, plugin_id in set(self._loaded_metadata) | set(self._discovered_metadata) if kind == plugin_type}
        if target == "all":
            return union_ids
        if target != plugin_type or not pattern:
            return set()
        return {plugin_id for plugin_id in union_ids if fnmatch.fnmatch(plugin_id, pattern)}

    def _plugin_class_from_snapshot(self, plugin_type: str, plugin_id: str) -> type[Any] | None:
        if self._discovered_snapshot is None:
            return None
        registry = {
            "source": self._discovered_snapshot.sources,
            "rule": self._discovered_snapshot.rules,
            "output": self._discovered_snapshot.outputs,
        }[plugin_type]
        return registry.get(plugin_id)

    def _count_state(self, plugin_type: str, state_name: str) -> int:
        if self.engine is None:
            return 0
        return sum(
            1
            for status in self.engine.plugin_states.values()
            if status.plugin_type == plugin_type and status.state == state_name
        )

    def _plugin_ids_for_type(self, states: dict[str, PluginStatus], plugin_type: str) -> set[str]:
        return {status.plugin_id for status in states.values() if status.plugin_type == plugin_type and status.loaded}


def _settled_state(status: PluginStatus) -> str:
    """READY or FAILED for a plugin that is no longer being reloaded.

    A source or an output is READY once its init() worked; a rule has no init(), so
    it is READY unless its last evaluation (or its resolution) failed.
    """
    if status.plugin_type == "rule":
        return "FAILED" if status.last_error else "READY"
    return "READY" if status.init_ok else "FAILED"


def _reload_summary(target: str, pattern: str | None, plans: dict[str, _ReloadPlan]) -> dict[str, Any]:
    """The answer to a targeted reload; "partial" or "reload_failed" says something was not applied."""
    summary: dict[str, Any] = {
        "status": "reloaded",
        "target": target,
        "pattern": pattern,
        **{f"{plugin_type}s": plans[plugin_type].summary() for plugin_type in PLUGIN_TYPES},
    }
    problems = [
        f"{plugin_type} '{plugin_id}': {message}"
        for plugin_type in PLUGIN_TYPES
        for plugin_id, message in summary[f"{plugin_type}s"]["errors"].items()
    ]
    if problems:
        applied = any(plan.instances or plan.removed for plan in plans.values())
        summary["status"] = "partial" if applied else "reload_failed"
        summary["error"] = "; ".join(problems)
    return summary


def _initial_schedule_run_at(schedule, now: datetime) -> datetime:
    candidate = schedule.next_after(now - timedelta(minutes=1))
    if candidate <= now:
        return now
    return candidate


def _parse_reload_request(request: dict[str, Any]) -> tuple[str, str | None]:
    if not request:
        return ("all", None)
    if request.get("target"):
        target = str(request["target"])
        pattern = request.get("pattern")
        if target not in {"dirty", "all", "full", "rule", "source", "output"}:
            raise ValueError(f"unknown reload target '{target}'")
        if target in {"rule", "source", "output"}:
            if not pattern:
                raise ValueError(f"reload target '{target}' requires a pattern")
            return (target, str(pattern))
        return (target, None)
    selected = [
        name
        for name in ("dirty", "all", "full", "rule", "source", "output")
        if request.get(name)
    ]
    if len(selected) > 1:
        raise ValueError("reload request must select only one of rule/source/output/dirty/all/full")
    if request.get("dirty"):
        return ("dirty", None)
    if request.get("all"):
        return ("all", None)
    if request.get("full"):
        return ("full", None)
    for target in ("rule", "source", "output"):
        pattern = request.get(target)
        if pattern:
            return (target, str(pattern))
    raise ValueError("reload request requires one of rule/source/output/dirty/all/full")


def _changed_files(
    previous_signature: tuple[tuple[str, int], ...],
    new_signature: tuple[tuple[str, int], ...],
) -> list[str]:
    previous = dict(previous_signature)
    current = dict(new_signature)
    paths = set(previous) | set(current)
    return sorted(path for path in paths if previous.get(path) != current.get(path))


def _file_key(path: str | Path) -> str:
    """One name for a file however it was reached (a relative path, a symlink, /tmp)."""
    try:
        return str(Path(path).resolve())
    except OSError:
        return str(path)


def _build_dependency_map(rule_directories: list[Path]) -> dict[str, set[str]]:
    """For each Python file of the plugin directories, the ones it imports, also through
    the ones those import (``from a import LIMIT`` where a.py has ``from b import LIMIT``).

    A module is named from the plugin directory it is in, as the imports find it: ``limits``
    for limits.py, ``pkg.limits`` for pkg/limits.py, ``pkg`` for pkg/__init__.py. Keys and
    values are the files' resolved paths."""
    modules: dict[str, str] = {}  # module name -> file
    names_of: dict[str, tuple[str, bool]] = {}  # file -> (module name, is a package)
    for root in rule_directories:
        if not root.exists() or not root.is_dir():
            continue
        for path in sorted(root.rglob("*.py")):
            if not path.is_file():
                continue
            parts = path.relative_to(root).with_suffix("").parts
            package = parts[-1] == "__init__"
            if package:
                parts = parts[:-1]
            if not parts:
                continue
            key = _file_key(path)
            name = ".".join(parts)
            modules.setdefault(name, key)
            names_of.setdefault(key, (name, package))

    direct: dict[str, set[str]] = {}
    for key, (name, package) in names_of.items():
        try:
            tree = ast.parse(Path(key).read_text(encoding="utf-8"), filename=key)
        except Exception:
            direct[key] = set()
            continue
        direct[key] = {modules[imported] for imported in _imported_names(tree, name, package) if imported in modules} - {key}

    closed: dict[str, set[str]] = {}
    for key in direct:
        seen: set[str] = set()
        pending = list(direct[key])
        while pending:
            dependency = pending.pop()
            if dependency in seen or dependency == key:
                continue
            seen.add(dependency)
            pending.extend(direct.get(dependency, ()))
        closed[key] = seen
    return closed


def _imported_names(tree: ast.AST, module_name: str, is_package: bool) -> set[str]:
    """The modules a file imports, with the packages they are in (their __init__ runs too)."""
    names: set[str] = set()

    def with_packages(name: str) -> None:
        parts = name.split(".")
        names.update(".".join(parts[: index + 1]) for index in range(len(parts)))

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                with_packages(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # from .limits import LIMIT: from the file's own package
                package = module_name.split(".") if is_package else module_name.split(".")[:-1]
                if node.level - 1 >= len(package):
                    continue
                package = package[: len(package) - (node.level - 1)]
                base = ".".join(package + ([node.module] if node.module else []))
            else:
                base = node.module or ""
            if not base:
                continue
            with_packages(base)
            names.update(f"{base}.{alias.name}" for alias in node.names if alias.name != "*")  # from pkg import limits
    return names


def _hash_file_around_plugins(path: str, plugin_class_names: set[str]) -> str:
    """The file without its plugin classes: what they all share. An edit to one plugin class
    changes only that plugin; an edit to anything else in the file changes every plugin in it."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return ""
    try:
        tree = ast.parse(text, filename=path)
    except SyntaxError:
        return _hash_text(text)
    dropped: set[int] = set()
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name in plugin_class_names and node.end_lineno is not None:
            first = min([decorator.lineno for decorator in node.decorator_list] + [node.lineno])
            dropped.update(range(first, node.end_lineno + 1))
    kept = [line for number, line in enumerate(text.splitlines(keepends=True), start=1) if number not in dropped]
    return _hash_text("".join(kept))


def _class_sources(plugin_cls: type[Any]) -> str:
    """The plugin class's code, with that of the classes it extends that are plugin code too
    (a base class in the same file, or in a helper): a change to the base changes the plugin."""
    texts = []
    for cls in plugin_cls.__mro__:
        if cls is plugin_cls or _is_plugin_code(cls):
            texts.append(_safe_getsource(cls))
    return "\0".join(texts)


def _is_plugin_code(cls: type[Any]) -> bool:
    module = getattr(cls, "__module__", "") or ""
    if module.startswith("_kanary_rules_"):
        return True
    if module == "kanary" or module.startswith("kanary.") or module == "builtins":
        return False
    try:
        source_file = inspect.getsourcefile(cls)
    except (OSError, TypeError):
        return False
    return bool(source_file) and not installed_file(source_file)


def _safe_getsource(plugin_cls: type[Any]) -> str:
    try:
        return inspect.getsource(plugin_cls)
    except Exception:
        return ""


def _hash_text(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _hash_dependencies(paths: tuple[str, ...]) -> str:
    digest = hashlib.sha256()
    for path in paths:
        file_path = Path(path)
        try:
            data = file_path.read_bytes()
        except OSError:
            data = b""
        digest.update(path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(data).hexdigest().encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()
