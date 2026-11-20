# kanary-template: buffered
# kind: source
# level: advanced
# summary: Keep recent history in a kanary.Buffer and publish window averages and minimums.
import kanary


@kanary.source(source_id="template.buffered", interval=10 * kanary.second)
class TemplateBuffered(kanary.Source):
    description = "TODO: describe the raw value and the derived inputs."

    def init(self):
        # Per input: at most size samples, none older than seconds (counted back from
        # the newest sample). Keep enough for the longest window below.
        self.buffer = kanary.Buffer(size=1024, seconds=1 * kanary.hour)

    def poll(self):
        reading = 1.0  # TODO: read the raw value (a device, a file, an API)
        # Added the same way as for kanary.inputs(). When the reading carries its own
        # time, give it - ("pressure", reading, observed_at) - so the windows are right.
        self.buffer.add(("pressure", reading))

        # buffer["pressure"] is a kanary.Series: the samples of one input, oldest first.
        pressure = self.buffer["pressure"]
        # Right after start the window is only partly filled. This source waits until
        # 10 minutes are buffered; skip() means "no new snapshot, do not evaluate".
        # Decide as fits: by count (len(pressure) < 60), or publish partial results.
        if pressure.span < 10 * kanary.minute:
            return kanary.skip(reason="filling the buffer")
        return kanary.inputs(
            ("pressure", reading),
            ("pressure_avg_10m", pressure.window(10 * kanary.minute).mean()),
            # The lowest value of the last 5 minutes is over a limit only when every
            # sample was: a range rule on it fires after 5 minutes over (a fire_after).
            ("pressure_min_5m", pressure.window(5 * kanary.minute).min()),
        )

    # A Series also has first, last (.value, .timestamp), values, timestamps, len(),
    # span, tail(n), median(), max(), sum(), stddev(); a statistic is None without data.
    # The "trend", "spike", and "frozen" templates show the others.
    # Several inputs: self.buffer.select("temp_*") -> {name: Series}. print(self.buffer)
    # shows what is kept. kanaryctl test-poll runs poll() too; what it adds is dropped.
    # To keep what a helper class reads, subclass it: self.buffer.add(super().poll()).


# A rule on the result, as in the "range" template:
#
#   @kanary.rule(rule_id="template.buffered.high", inputs="template.buffered:pressure_min_5m",
#                severity=kanary.WARN, tags=["vacuum"])
#   class PressureHigh(kanary.RangeRule):
#       high = 2.0
