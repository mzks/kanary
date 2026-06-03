from __future__ import annotations

import ast
from dataclasses import dataclass
from datetime import datetime, timedelta
import fnmatch
import hashlib
import inspect
import logging
from pathlib import Path
import threading
import time
from typing import Any

from .api import ControlAPI
from .engine import Engine
from .filtering import apply_excludes
from .loader import RuleDirectoryLoader
from .models import PluginStatus
from .source import compiled_schedule
from .store import build_store

logger = logging.getLogger("kanary.runtime")

DEFAULT_LOG_LEVEL = "INFO"
LOG_LEVEL_CHOICES = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
AUTO_RELOAD_CHOICES = ("off", "dirty", "all")


@dataclass(slots=True)
class RuntimeConfig:
    rule_directories: list[Path]
    reload_interval: float = 1.0
    auto_reload: str = "off"
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    enable_default_viewer: bool = True
    print_alerts: bool = False
    exclude_plugins: list[str] | None = None
    log_level: str = DEFAULT_LOG_LEVEL
    state_db_path: Path | None = None
    node_id: str | None = None


@dataclass(slots=True)
class PluginMetadata:
    plugin_type: str
    plugin_id: str
    definition_file: str | None
    source_hash: str
    dependency_files: tuple[str, ...]
    dependency_hash: str

    @property
    def signature(self) -> tuple[str, str]:
        return (self.source_hash, self.dependency_hash)


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
        self._discovered_snapshot = None
        self._discovered_metadata: dict[tuple[str, str], PluginMetadata] = {}
        self._loaded_metadata: dict[tuple[str, str], PluginMetadata] = {}
        self._untracked_files: list[str] = []
        self.api = ControlAPI(
            engine_getter=lambda: self.engine,
            reload_callback=self.reload_now,
            host=config.api_host,
            port=config.api_port,
            enable_default_viewer=config.enable_default_viewer,
        )

    def start(self) -> None:
        snapshot = self.loader.load(exclude_patterns=self.config.exclude_plugins)
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
        )
        self.engine.start()
        self._publish_runtime_plugin_overlay()
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
        if expected_signature is not None or self.loader.snapshot_signature() != self._signature:
            refreshed = self._refresh_discovery(expected_signature=expected_signature)
            if refreshed is False:
                return {"status": "reload_failed"} if request is not None else False
        if request is None:
            return self._apply_reload_request({"target": "all"})
        return self._apply_reload_request(request)

    def _refresh_discovery(self, expected_signature: tuple[tuple[str, int], ...] | None = None) -> bool:
        new_signature = expected_signature or self.loader.snapshot_signature()
        try:
            snapshot = self.loader.load(exclude_patterns=self.config.exclude_plugins)
        except Exception:
            logger.exception("reload failed while loading rule directory")
            return False
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

    def _apply_reload_request(self, request: dict[str, Any]) -> dict[str, Any]:
        if self.engine is None or self._discovered_snapshot is None:
            return {"status": "reload_failed"}
        target, pattern = _parse_reload_request(request)
        summary = {
            "status": "reloaded",
            "target": target,
            "pattern": pattern,
            "rules": self._apply_plugin_reload("rule", target=target, pattern=pattern),
            "sources": self._apply_plugin_reload("source", target=target, pattern=pattern),
            "outputs": self._apply_plugin_reload("output", target=target, pattern=pattern),
        }
        self._publish_runtime_plugin_overlay()
        return summary

    def _apply_plugin_reload(self, plugin_type: str, *, target: str, pattern: str | None) -> dict[str, Any]:
        selected_ids = self._select_plugin_ids(plugin_type, target=target, pattern=pattern)
        if not selected_ids:
            return {"matched": [], "reloaded": [], "removed": [], "failed": []}

        discovered = {
            plugin_id: self._plugin_class_from_snapshot(plugin_type, plugin_id)
            for plugin_id in selected_ids
            if self._plugin_class_from_snapshot(plugin_type, plugin_id) is not None
        }
        loaded_ids = {plugin_id for kind, plugin_id in self._loaded_metadata if kind == plugin_type}
        removed_ids = {plugin_id for plugin_id in selected_ids if plugin_id in loaded_ids and plugin_id not in discovered}
        reloaded_ids: list[str] = []
        failed_ids: list[str] = []
        for plugin_id in selected_ids:
            status = self.engine._plugin_status(plugin_type, plugin_id)
            status.state = "RELOADING"

        if plugin_type == "source":
            self._stop_source_threads(selected_ids)
        try:
            if plugin_type == "rule":
                self.engine.reload_rule_plugins(replacements=discovered, removed_rule_ids=removed_ids)
            elif plugin_type == "source":
                self.engine.reload_source_plugins(replacements=discovered, removed_source_ids=removed_ids)
            elif plugin_type == "output":
                self.engine.reload_output_plugins(replacements=discovered, removed_output_ids=removed_ids)
            else:
                raise ValueError(f"unknown plugin type '{plugin_type}'")
            reloaded_ids = sorted(discovered)
        except Exception:
            failed_ids = sorted(selected_ids)
            logger.exception("targeted reload failed for %s pattern=%s", plugin_type, pattern or target)
        finally:
            if plugin_type == "source":
                self._start_source_threads({source_id for source_id in selected_ids if source_id in self.engine.sources})

        if not failed_ids:
            for plugin_id in removed_ids:
                self._loaded_metadata.pop((plugin_type, plugin_id), None)
                self.engine.plugin_states.pop(self.engine._plugin_key(plugin_type, plugin_id), None)
            for plugin_id in reloaded_ids:
                metadata = self._discovered_metadata.get((plugin_type, plugin_id))
                if metadata is not None:
                    self._loaded_metadata[(plugin_type, plugin_id)] = metadata
            for plugin_id in selected_ids:
                status = self.engine._plugin_status(plugin_type, plugin_id)
                if plugin_id in removed_ids:
                    continue
                status.loaded = plugin_id in self._plugin_ids_for_type(self.engine.plugin_states, plugin_type)
                status.dirty_reason = None
                status.definition_file = self._discovered_metadata.get((plugin_type, plugin_id), self._loaded_metadata.get((plugin_type, plugin_id), None)).definition_file if self._discovered_metadata.get((plugin_type, plugin_id), self._loaded_metadata.get((plugin_type, plugin_id), None)) is not None else status.definition_file
                if status.state == "RELOADING":
                    status.state = "READY" if status.init_ok else "FAILED"
        else:
            for plugin_id in selected_ids:
                status = self.engine._plugin_status(plugin_type, plugin_id)
                status.state = "FAILED"

        logger.info(
            "reload applied: target=%s pattern=%s type=%s matched=%s reloaded=%s removed=%s failed=%s",
            target,
            pattern or "-",
            plugin_type,
            ",".join(sorted(selected_ids)) or "-",
            ",".join(reloaded_ids) or "-",
            ",".join(sorted(removed_ids)) or "-",
            ",".join(failed_ids) or "-",
        )
        return {
            "matched": sorted(selected_ids),
            "reloaded": reloaded_ids,
            "removed": sorted(removed_ids),
            "failed": failed_ids,
        }

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

    def _stop_source_threads(self, source_ids: set[str]) -> None:
        for source_id in set(source_ids):
            stop_event = self._source_stop_events.get(source_id)
            thread = self._source_threads.get(source_id)
            if stop_event is None or thread is None:
                continue
            stop_event.set()
            thread.join(timeout=2.0)
            self._source_stop_events.pop(source_id, None)
            self._source_threads.pop(source_id, None)

    def _start_source_threads(self, source_ids: set[str]) -> None:
        if self.engine is None:
            return
        for source_id in set(source_ids):
            if source_id not in self.engine.sources or source_id in self._source_threads:
                continue
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
        next_run_at: datetime | None = None
        while not stop_event.is_set() and not self._stop_event.is_set():
            source = self.engine.sources.get(source_id)
            if source is None:
                return
            now = datetime.now().astimezone()
            schedule = compiled_schedule(source)
            if schedule is not None:
                if next_run_at is None:
                    next_run_at = _initial_schedule_run_at(schedule, now)
                if now < next_run_at:
                    stop_event.wait((next_run_at - now).total_seconds())
                    continue
            try:
                payload = self._poll_source_with_recovery(source_id, source, now, stop_event)
                alerts = self.engine.evaluate_source(source_id, payload, now=now)
                if self.config.print_alerts:
                    self._print_alerts(alerts)
            except Exception as exc:
                logger.exception("source '%s' failed", source_id)
            if schedule is not None:
                next_run_at = schedule.next_after(now)
                continue
            stop_event.wait(float(source.interval))

    def _poll_source_with_recovery(
        self,
        source_id: str,
        source,
        now: datetime,
        stop_event: threading.Event,
    ):
        assert self.engine is not None
        last_exc: Exception | None = None
        attempt = 0

        try:
            return source.poll({"engine": self.engine, "now": now})
        except Exception as exc:
            last_exc = exc

        for _ in range(getattr(source, "max_retry", 1)):
            attempt += 1
            if stop_event.wait(attempt ** 2) or self._stop_event.is_set():
                raise RuntimeError("source polling interrupted during recovery")
            try:
                return source.poll({"engine": self.engine, "now": now})
            except Exception as exc:
                last_exc = exc

        for _ in range(getattr(source, "max_reinit", 1)):
            attempt += 1
            if stop_event.wait(attempt ** 2) or self._stop_event.is_set():
                raise RuntimeError("source polling interrupted during recovery")
            try:
                self.engine._terminate_source(source)
            except Exception:
                pass
            try:
                self.engine._initialize_source(source)
            except Exception as exc:
                last_exc = exc
                continue
            try:
                return source.poll({"engine": self.engine, "now": now})
            except Exception as exc:
                last_exc = exc

        assert last_exc is not None
        self.engine.record_source_failure(source_id, str(last_exc), now=now)
        raise last_exc

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

    def _collect_plugin_metadata(self, snapshot) -> dict[tuple[str, str], PluginMetadata]:
        dependency_map = _build_dependency_map(self.loader.rule_directories)
        metadata: dict[tuple[str, str], PluginMetadata] = {}
        for plugin_type, registry in (
            ("source", snapshot.sources),
            ("rule", snapshot.rules),
            ("output", snapshot.outputs),
        ):
            for plugin_id, plugin_cls in registry.items():
                definition_file = getattr(plugin_cls, "__kanary_definition_file__", None)
                dependencies = tuple(sorted(dependency_map.get(str(definition_file), set()))) if definition_file else ()
                dependency_hash = _hash_dependencies(dependencies)
                metadata[(plugin_type, plugin_id)] = PluginMetadata(
                    plugin_type=plugin_type,
                    plugin_id=plugin_id,
                    definition_file=str(definition_file) if definition_file else None,
                    source_hash=_hash_text(_safe_getsource(plugin_cls)),
                    dependency_files=dependencies,
                    dependency_hash=dependency_hash,
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
                status.dirty_reason = "dependency_changed" if loaded.source_hash == discovered.source_hash else "definition_changed"
                continue
            status.loaded = loaded is not None
            status.dirty_reason = None
            if status.state in {"DISCOVERED", "DIRTY", "PENDING_REMOVE", "RELOADING"}:
                status.state = "READY" if status.init_ok else "FAILED"

    def _compute_untracked_files(self, changed_files: list[str]) -> list[str]:
        tracked_files: set[str] = set()
        for metadata in list(self._loaded_metadata.values()) + list(self._discovered_metadata.values()):
            if metadata.definition_file:
                tracked_files.add(metadata.definition_file)
            tracked_files.update(metadata.dependency_files)
        return sorted(path for path in changed_files if path not in tracked_files)

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


def _json_default(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _initial_schedule_run_at(schedule, now: datetime) -> datetime:
    candidate = schedule.next_after(now - timedelta(minutes=1))
    if candidate <= now:
        return now
    return candidate


def _parse_reload_request(request: dict[str, Any]) -> tuple[str, str | None]:
    if request.get("dirty"):
        return ("dirty", None)
    if request.get("all"):
        return ("all", None)
    for target in ("rule", "source", "output"):
        pattern = request.get(target)
        if pattern:
            return (target, str(pattern))
    raise ValueError("reload request requires one of rule/source/output/dirty/all")


def _changed_files(
    previous_signature: tuple[tuple[str, int], ...],
    new_signature: tuple[tuple[str, int], ...],
) -> list[str]:
    previous = dict(previous_signature)
    current = dict(new_signature)
    paths = set(previous) | set(current)
    return sorted(path for path in paths if previous.get(path) != current.get(path))


def _build_dependency_map(rule_directories: list[Path]) -> dict[str, set[str]]:
    python_files: dict[str, Path] = {}
    for root in rule_directories:
        if not root.exists():
            continue
        for path in root.rglob("*.py"):
            if path.is_file():
                python_files[str(path)] = path
    module_index: dict[str, str] = {}
    for path_str, path in python_files.items():
        for root in rule_directories:
            try:
                relative = path.relative_to(root)
            except ValueError:
                continue
            module_name = ".".join(relative.with_suffix("").parts)
            module_index[module_name] = path_str
            break

    dependency_map: dict[str, set[str]] = {path: set() for path in python_files}
    for path_str, path in python_files.items():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except Exception:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            else:
                continue
            for name in names:
                dependency = module_index.get(name)
                if dependency and dependency != path_str:
                    dependency_map[path_str].add(dependency)
    return dependency_map


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
