# kanary-template: trend
# kind: source
# level: advanced
# summary: Publish the trend of a value and the time until it reaches a limit, from recent history (kanary.Buffer).
# summary-ja: 値の傾向と、上限に届くまでの時間を、最近の履歴から出す (kanary.Buffer)。
import kanary


@kanary.source(source_id="template.trend", interval=30 * kanary.second)
class TemplateTrend(kanary.Source):
    description = "TODO: describe the value and the limit it must not reach."

    def init(self):
        # Per input: the samples of the last 30 minutes (see the "buffered" template).
        self.buffer = kanary.Buffer(max_age=30 * kanary.minute)

    def poll(self):
        reading = 50.0  # TODO: read the value, e.g. a tank level or a disk usage in %
        self.buffer.add(("level", reading))
        level = self.buffer["level"]
        if level.span < 10 * kanary.minute:
            return kanary.skip(reason="filling the buffer")
        last_10m = level.window(10 * kanary.minute)
        return kanary.inputs(
            ("level", reading),
            # Least-squares trend over every sample of the window, per second; times
            # kanary.minute gives % per minute. Steadier than rate(), which uses only
            # the first and the last sample.
            ("level_slope_per_min", last_10m.slope() * kanary.minute),
            # Seconds until the trend reaches 95; None when flat or moving away (and
            # once the value is past 95, so check the value itself as well).
            ("level_eta_seconds", last_10m.eta(95.0)),
        )


# Warn an hour before the limit is reached, and raise it 10 minutes before:
#
#   @kanary.rule(rule_id="template.trend.soon_full", inputs="template.trend:level_eta_seconds",
#                severity=kanary.WARN, tags=["tank"])
#   class SoonFull(kanary.ThresholdRule):
#       direction = "low"
#       thresholds = [(1 * kanary.hour, kanary.WARN), (10 * kanary.minute, kanary.ERROR)]
