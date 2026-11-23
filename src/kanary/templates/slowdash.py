# kanary-template: slowdash
# kind: source
# level: basic
# summary: Read the newest value of SlowDash channels at their own times; alert when SlowDash does not answer, or a channel stops updating or is missing (SlowDashSource).
# summary-ja: SlowDash の channel の最新値をその点の時刻付きで読む。SlowDash が応答しない、channel の更新が止まった、channel が見つからない、で発報する (SlowDashSource)。
import kanary


@kanary.source(source_id="template.slowdash", interval=1 * kanary.minute)
class TemplateSlowdash(kanary.SlowDashSource):
    description = "TODO: describe what these channels measure."

    # The SlowDash address, as opened in the browser (18881: SlowDash's default port).
    # A page address such as .../slowplot.html?config=... works too.
    url = "http://localhost:18881"  # TODO
    timeout = 5.0

    # Input name -> SlowDash channel; or a list of channels, each named after itself:
    # channels = ["oil.temperature", "oil.level"]
    # A pattern takes every matching channel. Listed ("oil.*"), each is named after
    # itself; in an input name, * is what * matched: hv.pmt.17 below becomes pmt_hv.17.
    channels = {
        "oil_temperature": "oil.temperature",  # TODO
        "run_state": "daq.run_state",  # text such as "RUN" stays text
        "pmt_hv.*": "hv.pmt.*",
    }

    # Each poll reads the last 10 minutes of every channel in one request and takes
    # each channel's newest point. A channel with no point in that time still gets its
    # last point before, from SlowDash's SQL data sources (SlowDash 2026-02 or later);
    # otherwise it has no input. Every point in the window is sent, so shorten it when
    # there are many fast channels.
    lookback = 10 * kanary.minute

    # Two inputs of its own:
    # - slowdash.up is 1 while SlowDash answers /api/ping. When it does not,
    #   slowdash.up is 0 and the channels have no inputs.
    # - slowdash.missing counts the channels without a point (a typo, a channel gone,
    #   or none in lookback without prior_data); their names are in its metadata.
    #   A rule on * cannot see an input that is not there: this one can.


@kanary.rule(
    rule_id="template.slowdash.down",
    inputs="template.slowdash:slowdash.up",
    severity=kanary.ERROR,
    tags=["slowdash"],
)
class SlowdashDown(kanary.ExpectedValueRule):
    description = "SlowDash does not answer: it, its host, or the network between is down."
    runbook = "TODO: where SlowDash runs and how to restart it."
    expected = 1


@kanary.rule(
    rule_id="template.slowdash.stale",
    inputs="template.slowdash:*",
    severity=kanary.WARN,
    tags=["slowdash"],
    suppressed_by="template.slowdash.down",  # no answer says it all
)
class SlowdashStale(kanary.StaleRule):
    description = "A channel stopped updating: its writer, or the device behind it."
    timeout = 15 * kanary.minute  # TODO: longer than the slowest channel's update interval


@kanary.rule(
    rule_id="template.slowdash.missing",
    inputs="template.slowdash:slowdash.missing",
    severity=kanary.WARN,
    tags=["slowdash"],
    suppressed_by="template.slowdash.down",
)
class SlowdashMissing(kanary.RangeRule):
    description = "Channels without a point: a typo, a channel gone, or no point in lookback. Which ones: the source's metadata[\"missing\"]."
    high = 0


# Values and states, one rule each:
#
#   @kanary.rule(rule_id="template.slowdash.oil", inputs="template.slowdash:oil_temperature",
#                severity=kanary.ERROR, tags=["slowdash"], suppressed_by="template.slowdash.down")
#   class OilTemperature(kanary.RangeRule):
#       low, high = 10.0, 30.0
#
#   @kanary.rule(rule_id="template.slowdash.run", inputs="template.slowdash:run_state",
#                severity=kanary.WARN, tags=["slowdash"], suppressed_by="template.slowdash.down")
#   class NotRunning(kanary.ExpectedValueRule):
#       expected = "RUN"
