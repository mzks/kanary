# kanary-template: spike
# kind: source
# level: advanced
# summary: Publish how unusual the newest value is against recent history, to catch spikes and dips (kanary.Buffer).
import kanary


@kanary.source(source_id="template.spike", interval=10 * kanary.second)
class TemplateSpike(kanary.Source):
    description = "TODO: describe the value and what a spike means."

    def init(self):
        # Per input: the samples of the last hour (see the "buffered" template).
        self.buffer = kanary.Buffer(seconds=1 * kanary.hour)

    def poll(self):
        reading = 12.0  # TODO: read the value, e.g. a current or a trigger rate
        self.buffer.add(("current", reading))
        current = self.buffer["current"]
        if len(current) < 30:  # a spread from a few samples means little
            return kanary.skip(reason="filling the buffer")
        return kanary.inputs(
            ("current", reading),
            # Standard deviations of the newest value from the mean of the hour: a spike
            # (or a dip) against the usual noise, without a fixed limit.
            ("current_zscore", current.zscore()),
            # The jump from the previous sample, in the value's own unit.
            ("current_jump", current.tail(2).delta()),
        )


# Fire on more than 5 standard deviations either way:
#
#   @kanary.rule(rule_id="template.spike.current", inputs="template.spike:current_zscore",
#                severity=kanary.WARN, tags=["hv"])
#   class CurrentSpike(kanary.RangeRule):
#       low = -5.0
#       high = 5.0
