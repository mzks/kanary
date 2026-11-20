# kanary-template: frozen
# kind: source
# level: advanced
# summary: Publish how long a value has not changed (frozen sensor) and how often it changes (flapping), from recent history (kanary.Buffer).
import kanary


@kanary.source(source_id="template.frozen", interval=10 * kanary.second)
class TemplateFrozen(kanary.Source):
    description = "TODO: describe the value and how often it normally changes."

    def init(self):
        # Per input: the samples of the last hour (see the "buffered" template).
        self.buffer = kanary.Buffer(seconds=1 * kanary.hour)

    def poll(self):
        flow = 3.2   # TODO: read a value that always moves a little, e.g. a flow
        valve = 1    # TODO: read a state that changes now and then, e.g. a valve (0/1)
        self.buffer.add(("flow", flow), ("valve", valve))
        return kanary.inputs(
            ("flow", flow),
            ("valve", valve),
            # Seconds the value has stayed within 0.001: a frozen sensor or a stuck
            # reader keeps sending new timestamps with the same value, which a stale
            # rule cannot see.
            ("flow_unchanged_seconds", self.buffer["flow"].unchanged_seconds(tolerance=0.001)),
            # How often the state changed in the last 10 minutes (toggling, flapping).
            # Only sampled values are compared: changes between two polls are not seen.
            ("valve_changes_10m", self.buffer["valve"].window(10 * kanary.minute).changes()),
        )


# Rules on the results, as in the "range" template:
#
#   @kanary.rule(rule_id="template.frozen.flow", inputs="template.frozen:flow_unchanged_seconds",
#                severity=kanary.WARN, tags=["cooling"])
#   class FlowFrozen(kanary.RangeRule):
#       high = 10 * kanary.minute
#
#   @kanary.rule(rule_id="template.frozen.valve", inputs="template.frozen:valve_changes_10m",
#                severity=kanary.WARN, tags=["cooling"])
#   class ValveFlapping(kanary.RangeRule):
#       high = 6
