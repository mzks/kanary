import json
import os
from datetime import datetime, timezone
from urllib.request import urlopen

import kanary


@kanary.source(source_id="kanary.plugins", interval=30.0)
class KanaryPluginSource:

    def init(self, ctx):
        base_url = os.environ.get("KANARY_API_URL", "http://127.0.0.1:8000").rstrip("/")
        self.plugins_url = f"{base_url}/plugins"
        # Timeout for reading this Kanary node's own /plugins API.
        self.timeout_seconds = float(os.environ.get("KANARY_PLUGIN_SOURCE_TIMEOUT_SECONDS", "5.0"))

    def poll(self, ctx):
        try:
            with urlopen(self.plugins_url, timeout=self.timeout_seconds) as response:
                payload = json.loads(response.read().decode())
        except Exception as exc:
            return kanary.SourceResult(status="error", error=str(exc))

        now = datetime.now(timezone.utc)
        plugins = payload.get("plugins", [])
        measurements = [kanary.Measurement(name="heartbeat", value=1, timestamp=now)]

        for plugin_type in ("source", "rule", "output"):
            typed_plugins = [plugin for plugin in plugins if plugin.get("type") == plugin_type]
            failed_plugins = [plugin for plugin in typed_plugins if plugin.get("state") == "failed"]
            latest_failure_at = max(
                (
                    _parse_plugin_timestamp(plugin.get("last_failure_at"))
                    or _parse_plugin_timestamp(plugin.get("last_updated_at"))
                    or now
                )
                for plugin in failed_plugins
            ) if failed_plugins else now
            measurements.append(
                kanary.Measurement(
                    name=f"{plugin_type}.failed_count",
                    value=len(failed_plugins),
                    timestamp=latest_failure_at,
                    metadata={
                        "plugin_type": plugin_type,
                        "failed_plugin_ids": [plugin.get("plugin_id") or "unknown" for plugin in failed_plugins],
                        "failed_plugins": [
                            {
                                "plugin_id": plugin.get("plugin_id") or "unknown",
                                "last_error": plugin.get("last_error"),
                                "last_failure_at": plugin.get("last_failure_at"),
                            }
                            for plugin in failed_plugins
                        ],
                        "total_plugins": len(typed_plugins),
                    },
                )
            )

        return kanary.SourceResult(
            measurements=measurements,
            status="ok",
            metadata={"plugins_url": self.plugins_url},
        )


@kanary.rule(
    rule_id="kanary.plugins.connection.failed",
    source="kanary.plugins",
    severity=kanary.ERROR,
    tags=["kanary", "internal", "plugins"],
    owner="expert_kanary",
)
class KanaryPluginSourceConnectionFailed:

    def evaluate(self, payload, ctx):
        if payload.get("status") == "ok":
            return kanary.Evaluation(
                state=kanary.AlertState.OK,
                payload=payload,
                message=f"plugin API ok: {payload.get('metadata', {}).get('plugins_url', 'unknown')}",
            )
        return kanary.Evaluation(
            state=kanary.AlertState.FIRING,
            payload=payload,
            message=payload.get("error") or f"plugin source status={payload.get('status')}",
        )


@kanary.rule(
    rule_id="kanary.plugins.heartbeat.stale",
    source="kanary.plugins",
    severity=kanary.ERROR,
    tags=["kanary", "internal", "plugins"],
    owner="expert_kanary",
)
class KanaryPluginSourceHeartbeatStale(kanary.StaleRule):
    measurement = "heartbeat"
    timeout = 2 * kanary.minute
    suppressed_by = ["kanary.plugins.connection.failed"]


def make_plugin_type_failure_rule(
    *,
    plugin_type: str,
    severity: int = kanary.ERROR,
    owner: str | None = "expert_kanary",
):
    measurement = f"{plugin_type}.failed_count"
    rule_id = f"kanary.{plugin_type}.failure"
    cls_name = f"Kanary{plugin_type.title()}Failure"

    def evaluate(self, payload, ctx):
        count = ctx.value(measurement)
        result_payload = dict(payload)
        metadata = ctx.metadata(measurement, default={}) or {}
        failed_plugins = metadata.get("failed_plugins", [])
        if count is None:
            return kanary.Evaluation(
                state=kanary.AlertState.OK,
                payload=result_payload,
                message=f"{measurement} is missing",
            )
        if count <= 0:
            return kanary.Evaluation(
                state=kanary.AlertState.OK,
                payload=result_payload,
                message=f"no failed {plugin_type} plugins",
            )
        summaries = []
        for plugin in failed_plugins[:3]:
            plugin_id = plugin.get("plugin_id") or "unknown"
            last_error = plugin.get("last_error") or "runtime error"
            summaries.append(f"{plugin_id}: {last_error}")
        summary_text = "; ".join(summaries) if summaries else "details are in metadata"
        if len(failed_plugins) > 3:
            summary_text += f"; ... (+{len(failed_plugins) - 3} more)"
        return kanary.Evaluation(
            state=kanary.AlertState.FIRING,
            payload=result_payload,
            message=f"{count} failed {plugin_type} plugin(s); {summary_text}",
            severity=severity,
        )

    cls = type(
        cls_name,
        (),
        {
            "evaluate": evaluate,
        },
    )
    return kanary.rule(
        rule_id=rule_id,
        source="kanary.plugins",
        severity=severity,
        tags=["kanary", "internal", plugin_type, "failure"],
        owner=owner,
        suppressed_by=["kanary.plugins.connection.failed"],
    )(cls)


SourcePluginFailure = make_plugin_type_failure_rule(plugin_type="source")
RulePluginFailure = make_plugin_type_failure_rule(plugin_type="rule")
OutputPluginFailure = make_plugin_type_failure_rule(plugin_type="output")


def _parse_plugin_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = str(value).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
