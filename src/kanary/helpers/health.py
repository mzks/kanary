"""PluginHealthSource: this KANARY's own plugins, as ordinary inputs."""

from typing import Any

from ..models import SourceResult
from ..source import Source, inputs


class PluginHealthSource(Source):
    """Publish the state of every other plugin of the KANARY it runs in.

    Read in the same process (no HTTP, no settings). For each loaded plugin:

    - ``<type>.<plugin_id>.failed``: 1 while it is FAILED, else 0; metadata has
      its state, the error, and when it failed
    - outputs also ``output.<plugin_id>.queued`` (events waiting to be sent) and
      ``output.<plugin_id>.dropped`` (events dropped since the previous poll,
      because its queue was full or it was stopping)
    - rules also ``rule.<plugin_id>.slow``: 1 while evaluate() is slow

    Put ordinary rules on them, as the "plugin-health" template does. An alert
    about a failed output goes out through the other outputs.
    """

    def init(self) -> None:
        self._dropped: dict[str, int] = {}

    def poll(self) -> SourceResult:
        engine = getattr(self, "_kanary_engine", None)
        if engine is None:
            raise RuntimeError("PluginHealthSource reads the engine it runs in; it cannot run on its own")
        with engine._lock:
            statuses = [status for status in engine.plugin_states.values() if status.loaded]
        items: list[tuple[str, Any, Any, dict[str, Any]]] = []
        for status in statuses:
            if status.plugin_type == "source" and status.plugin_id == self.source_id:
                continue
            name = f"{status.plugin_type}.{status.plugin_id}"
            failed_at = status.last_failure_at.isoformat() if status.last_failure_at else None
            items.append((f"{name}.failed", int(status.state == "FAILED"), None,
                          {"state": status.state, "error": status.last_error, "failed_at": failed_at}))
            if status.plugin_type == "rule":
                items.append((f"{name}.slow", int(bool(status.warning)), None, {"warning": status.warning}))
            if status.plugin_type == "output":
                backlog = engine.output_backlog(status.plugin_id)
                dropped = backlog["dropped"]
                before = self._dropped.get(status.plugin_id, dropped)
                self._dropped[status.plugin_id] = dropped
                # A reload starts a new count; then everything counted so far is new.
                items.append((f"{name}.queued", backlog["queued"], None, {}))
                items.append((f"{name}.dropped", dropped - before if dropped >= before else dropped, None, {}))
        return inputs(items)
