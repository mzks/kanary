import json
from datetime import datetime, timezone
from urllib.request import urlopen

import kanary

# Self-monitoring example that turns failed source/rule/output plugins into
# ordinary alerts. Transport failures while reading /plugins are treated as
# source plugin failures and handled by the runtime recovery policy.

@kanary.source(source_id="kanary.plugins", interval=30.0)
class KanaryPluginSource:

    def init(self):
        config = kanary.load_toml(filename="self_plugin_monitoring_config.toml")
        base_url = str(config.get("api_base_url", "http://127.0.0.1:8000")).rstrip("/")
        self.plugins_url = f"{base_url}/plugins"
        self.timeout_seconds = float(config.get("timeout_seconds", 5.0))

    def poll(self):
        with urlopen(self.plugins_url, timeout=self.timeout_seconds) as response:
            payload = json.loads(response.read().decode())

        now = datetime.now(timezone.utc)
        plugins = payload.get("plugins", [])
        inputs = [("heartbeat", 1, now)]

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
            inputs.append(
                (
                    f"{plugin_type}.failed_count",
                    len(failed_plugins),
                    latest_failure_at,
                    {
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

        return kanary.inputs(inputs, metadata={"plugins_url": self.plugins_url})

@kanary.rule(
    rule_id="kanary.plugins.heartbeat.stale",
    inputs="kanary.plugins:heartbeat",
    severity=kanary.ERROR,
    tags=["kanary", "internal", "plugins"],
    owner="expert_kanary",
)
class KanaryPluginSourceHeartbeatStale(kanary.StaleRule):
    timeout = 2 * kanary.minute


def make_plugin_type_failure_rule(
    *,
    plugin_type: str,
    severity: int = kanary.ERROR,
    owner: str | None = "expert_kanary",
):
    measurement = f"{plugin_type}.failed_count"
    rule_id = f"kanary.{plugin_type}.failure"
    cls_name = f"Kanary{plugin_type.title()}Failure"

    def evaluate(self, ctx):
        count = ctx.value()
        metadata = ctx.metadata(default={}) or {}
        failed_plugins = metadata.get("failed_plugins", [])
        if count is None:
            return kanary.ok(f"{measurement} is missing")
        if count <= 0:
            return kanary.ok(f"no failed {plugin_type} plugins")
        summaries = []
        for plugin in failed_plugins[:3]:
            plugin_id = plugin.get("plugin_id") or "unknown"
            last_error = plugin.get("last_error") or "runtime error"
            summaries.append(f"{plugin_id}: {last_error}")
        summary_text = "; ".join(summaries) if summaries else "details are in metadata"
        if len(failed_plugins) > 3:
            summary_text += f"; ... (+{len(failed_plugins) - 3} more)"
        return kanary.firing(
            message=f"{count} failed {plugin_type} plugin(s); {summary_text}",
            severity=severity,
            extra={"failed_plugins": failed_plugins},
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
        inputs=f"kanary.plugins:{measurement}",
        severity=severity,
        tags=["kanary", "internal", plugin_type, "failure"],
        owner=owner,
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
