from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import logging
from pathlib import Path
import threading
import time

from .api import ControlAPI
from .engine import Engine
from .filtering import apply_excludes
from .loader import RuleDirectoryLoader
from .store import build_store

logger = logging.getLogger("kanary.runtime")

DEFAULT_LOG_LEVEL = "INFO"
LOG_LEVEL_CHOICES = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


@dataclass(slots=True)
class RuntimeConfig:
    rule_directories: list[Path]
    reload_interval: float = 1.0
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    print_alerts: bool = False
    exclude_plugins: list[str] | None = None
    log_level: str = DEFAULT_LOG_LEVEL
    state_db_path: Path | None = None
    node_id: str | None = None


class EngineRuntime:
    def __init__(self, config: RuntimeConfig) -> None:
        self.config = config
        self.loader = RuleDirectoryLoader(config.rule_directories)
        self.store = build_store(config.state_db_path)
        self._stop_event = threading.Event()
        self._reload_thread: threading.Thread | None = None
        self._api_thread: threading.Thread | None = None
        self._source_threads: dict[str, threading.Thread] = {}
        self._source_stop_events: dict[str, threading.Event] = {}
        self.engine: Engine | None = None
        self._signature: tuple[tuple[str, int], ...] = ()
        self.api = ControlAPI(
            engine_getter=lambda: self.engine,
            reload_callback=self.reload_now,
            host=config.api_host,
            port=config.api_port,
        )

    def start(self) -> None:
        snapshot = self.loader.load(exclude_patterns=self.config.exclude_plugins)
        self._signature = self.loader.snapshot_signature()
        self.engine = Engine(
            source_registry=snapshot.sources,
            rule_registry=snapshot.rules,
            output_registry=snapshot.outputs,
            store=self.store,
            node_id=self.config.node_id,
        )
        self.engine.start()
        logger.info("engine started with %d sources, %d rules, %d outputs", len(self.engine.sources), len(self.engine.rules), len(self.engine.outputs))
        self._sync_source_threads()
        self._api_thread = threading.Thread(target=self.api.start, daemon=True)
        self._api_thread.start()
        logger.info("control API listening on %s:%d", self.config.api_host, self.config.api_port)
        self._reload_thread = threading.Thread(target=self._watch_reload_loop, daemon=True)
        self._reload_thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self.api.shutdown()
        if self._api_thread is not None:
            self._api_thread.join(timeout=2.0)
        if self._reload_thread is not None:
            self._reload_thread.join(timeout=2.0)
        for stop_event in self._source_stop_events.values():
            stop_event.set()
        for thread in self._source_threads.values():
            thread.join(timeout=2.0)
        if self.engine is not None:
            self.engine.shutdown()
        logger.info("engine stopped")

    def run_forever(self) -> None:
        self.start()
        try:
            while not self._stop_event.is_set():
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()

    def _watch_reload_loop(self) -> None:
        while not self._stop_event.wait(self.config.reload_interval):
            self.reload_now_if_changed()

    def reload_now_if_changed(self) -> bool:
        new_signature = self.loader.snapshot_signature()
        if new_signature == self._signature:
            return True
        return self.reload_now(expected_signature=new_signature)

    def reload_now(self, expected_signature: tuple[tuple[str, int], ...] | None = None) -> bool:
        new_signature = expected_signature or self.loader.snapshot_signature()
        try:
            snapshot = self.loader.load(exclude_patterns=self.config.exclude_plugins)
        except Exception:
            logger.exception("reload failed while loading rule directory")
            return False
        self._signature = new_signature
        if self.engine is None:
            return False
        try:
            self.engine.reload(
                source_registry=snapshot.sources,
                rule_registry=snapshot.rules,
                output_registry=snapshot.outputs,
            )
            self._sync_source_threads()
            logger.info("reload succeeded")
            return True
        except Exception as exc:
            logger.exception("reload failed")
            return False

    def _sync_source_threads(self) -> None:
        if self.engine is None:
            return

        current_source_ids = set(self.engine.sources)
        existing_source_ids = set(self._source_threads)

        for source_id in existing_source_ids - current_source_ids:
            self._source_stop_events[source_id].set()
            self._source_threads[source_id].join(timeout=2.0)
            del self._source_stop_events[source_id]
            del self._source_threads[source_id]

        for source_id in current_source_ids - existing_source_ids:
            stop_event = threading.Event()
            thread = threading.Thread(
                target=self._source_loop,
                args=(source_id, stop_event),
                daemon=True,
            )
            self._source_stop_events[source_id] = stop_event
            self._source_threads[source_id] = thread
            thread.start()

    def _source_loop(self, source_id: str, stop_event: threading.Event) -> None:
        assert self.engine is not None
        while not stop_event.is_set() and not self._stop_event.is_set():
            source = self.engine.sources.get(source_id)
            if source is None:
                return
            now = datetime.now().astimezone()
            try:
                payload = source.poll({"engine": self.engine, "now": now})
                alerts = self.engine.evaluate_source(source_id, payload, now=now)
                if self.config.print_alerts:
                    self._print_alerts(alerts)
            except Exception as exc:
                self.engine.record_source_failure(source_id, str(exc), now=now)
                logger.exception("source '%s' failed", source_id)
            stop_event.wait(source.interval)

    def _print_alerts(self, alerts: dict) -> None:
        rows = []
        for alert in alerts.values():
            rows.append(
                {
                    "rule_id": alert.rule_id,
                    "state": alert.state.value,
                    "severity": int(alert.severity),
                    "message": alert.message,
                    "payload": alert.payload,
                    "last_evaluated_at": (
                        alert.last_evaluated_at.isoformat() if alert.last_evaluated_at else None
                    ),
                }
            )
        if rows:
            import json

            print(json.dumps(rows, ensure_ascii=False, indent=2, default=_json_default))

    def _apply_excludes(self, snapshot):
        return apply_excludes(snapshot, self.config.exclude_plugins)


def _json_default(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")
