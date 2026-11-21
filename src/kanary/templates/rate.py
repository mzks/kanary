# kanary-template: rate
# kind: rule
# level: advanced
# summary: Fire when a value changes too fast between two snapshots (RateRule).
# summary-ja: 2 つの snapshot の間で値が速く変わりすぎたら発報する (RateRule)。
import kanary


@kanary.rule(
    rule_id="template.rate",
    # The rate compares the current and previous snapshot of each input. When the
    # source returns the same sample again, the last rate is kept. Two-sample rates
    # are noisy; for a smoother trend use slope() of a kanary.Buffer series and a range rule.
    inputs="example.source:pressure",  # TODO
    severity=kanary.WARN,
    tags=["TODO"],
)
class TemplateRate(kanary.RateRule):
    description = "TODO: describe the allowed rate of change."
    # runbook = "TODO: what to check first, or a link"  # also in the notifications

    # Allowed rate range, in value units per second; works like the "range" template.
    # Write it with the time units: 1.0 / kanary.minute is "1 per minute".
    # low = -1.0 / kanary.minute also catches falling too fast.
    low = None
    high = 1.0 / kanary.minute
    lower_inclusive = True
    upper_inclusive = True
    hysteresis = 0.0

    # An input with no value (or no previous sample to compare with) is not
    # judged and leaves the rule OK. True: it fires too. For data that stops coming,
    # a StaleRule on the same input is the usual check (it also sees a dead source).
    missing_fires = False
