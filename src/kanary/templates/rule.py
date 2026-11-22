# kanary-template: rule
# kind: rule
# level: basic
# summary: Write your own evaluate(ctx) with full access to inputs, history, and severities.
# summary-ja: evaluate(ctx) を自分で書く。input、履歴、severity をすべて使える。
import kanary


@kanary.rule(
    rule_id="template.rule",
    # One selector, a list, or globs: "src:name", ["a:x", "b:y"], "src:*", "kernel_*:temp".
    inputs="example.source:temperature",  # TODO
    severity=kanary.WARN,  # default severity for kanary.firing(); INFO / WARN / ERROR / CRITICAL
    tags=["TODO"],  # Outputs route on tags
    # owner="detector-team",
    # While any of these fire, this rule is SUPPRESSED (not notified):
    # suppressed_by=["daq.link"],  # still evaluated: the viewer shows what it would say
    # depends_on=["daq.link"],     # not evaluated at all (it keeps history, or cannot work without them)
    # evaluation_interval=30 * kanary.second,  # also evaluate on a timer, not only on source updates
)
class TemplateRule:
    description = "TODO: describe when this rule fires."
    # runbook = "TODO: what to check first, or a link"  # also in the notifications

    # Your own setting: any class attribute works. KANARY does not read this name;
    # only evaluate() below uses it.
    high_temperature = 80.0

    def evaluate(self, ctx):
        # Keep evaluate() fast and free of I/O: rules are evaluated one at a time, so
        # every other evaluation waits for this one (over 100 ms shows as SLOW).
        # Fetch everything you need in a Source instead.
        value = ctx.value()  # the single selected input; ctx.value("src:name") picks one
        if value is None:
            # No value to judge. OK here is "not judged", as the helper rules do by
            # default (missing_fires = False): data that stops coming is the job of a
            # StaleRule on the same input, so one cause does not alert twice. Return
            # kanary.firing(...) instead when a missing value is itself the problem.
            return kanary.ok("temperature has no value")

        previous = ctx.prev_value()  # value from the previous snapshot, or None
        # Also: ctx.timestamp(), ctx.metadata(), ctx.inputs(), ctx.values(),
        #       ctx.age()     seconds since the input's timestamp
        #       ctx.delta()   value - previous value (None unless both are numbers)
        #       ctx.changed() True when the value differs from the previous one
        #       ctx.missing() declared inputs that are absent or have no value
        #       ctx.previous_alert (last state of this rule), ctx.now.
        high = self.high_temperature
        extra = {"previous": previous, "high_temperature": high}

        # Return the first helper that matches; *_if() returns None when the condition is false.
        return (
            kanary.critical_if(value > high + 20, f"temperature={value} is far over {high}", extra=extra)
            or kanary.error_if(value > high + 10, f"temperature={value} is over {high + 10}", extra=extra)
            or kanary.warn_if(value > high, f"temperature={value} is over {high}", extra=extra)
            or kanary.ok(f"temperature={value} is normal", extra=extra)
        )
        # kanary.firing(message) fires with the decorator severity;
        # kanary.firing(message, severity=kanary.ERROR) overrides it for this evaluation.
