# kanary-template: plugin-health
# kind: source
# level: basic
# summary: Watch this KANARY's own plugins: alert when a source, rule, or output fails, or an output drops events (PluginHealthSource).
# summary-ja: オススメ! この KANARY 自身の plugin を見張る: source、rule、output の失敗や、output が event を捨てたときに発報する (PluginHealthSource)。
import kanary


@kanary.source(source_id="template.plugin_health", interval=30 * kanary.second)
class TemplatePluginHealth(kanary.PluginHealthSource):
    description = "The plugins of this KANARY: failures, slow rules, and output backlog."
    # It reads the engine it runs in, so it needs no settings. For every other plugin:
    #   source.daq.failed / rule.oil.high.failed / output.slack.failed
    #                           1 while FAILED (the error is in the metadata), else 0
    #   output.slack.queued     events waiting to be sent
    #   output.slack.dropped    events dropped since the previous poll (queue full)
    #   rule.oil.high.slow      1 while evaluate() is slow


@kanary.rule(
    rule_id="template.plugin_health.failed",
    inputs="template.plugin_health:*.failed",
    severity=kanary.ERROR,
    tags=["kanary"],
)
class PluginFailed:
    description = "A plugin of this KANARY failed; KANARY retries it or waits for a fix."
    runbook = "kanaryctl plugins PLUGIN_ID --traceback shows the whole error. Fix the plugin or its settings, then reload it."

    def evaluate(self, ctx):
        failed = [item for item in ctx.inputs() if item.value]
        if not failed:
            return kanary.ok(f"all {len(ctx.inputs())} plugins are working")
        names = [item.input_name.removesuffix(".failed") for item in failed]
        details = [f"{name}: {item.metadata.get('error') or 'failed'}" for name, item in zip(names, failed)]
        return kanary.firing(f"{len(failed)} plugin(s) failed - " + "; ".join(details), extra={"failed": names})


@kanary.rule(
    rule_id="template.plugin_health.dropped",
    inputs="template.plugin_health:output.*.dropped",
    severity=kanary.WARN,
    tags=["kanary"],
)
class OutputDropping(kanary.RangeRule):
    description = "An output dropped alert events: its destination is down or too slow."
    high = 0


# An alert about a failed output goes out through the other outputs; with a single
# output, only the viewer shows it. KANARY itself stopping cannot be seen from inside:
# for that, watch it from another KANARY (the "peer" template) or from outside (the
# "heartbeat" template).
