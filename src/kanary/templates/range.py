# kanary-template: range
# kind: rule
# level: basic
# summary: Fire when a value leaves [low, high], or enters a forbidden band (RangeRule).
# summary-ja: 値が [low, high] を外れたとき、または禁止帯に入ったときに発報する (RangeRule)。
import kanary


@kanary.rule(
    rule_id="template.range",
    # With a glob such as "example.source:*", each input is checked independently
    # and any out-of-range input fires the rule.
    inputs="example.source:temperature",  # TODO
    severity=kanary.WARN,
    tags=["TODO"],
    # owner="detector-team",
    # depends_on=[...], suppressed_by=[...], evaluation_interval=...  (see the "rule" template)
)
class TemplateRange(kanary.RangeRule):
    description = "TODO: describe the normal range."
    # runbook = "TODO: what to check first, or a link"  # also in the notifications

    # The range [low, high]. Set either one to None for a one-sided check; never both.
    low = 10.0
    high = 30.0

    # "outside" (default): [low, high] is normal; leaving it fires.
    # "inside":            [low, high] is forbidden; entering it fires
    #                      (e.g. a resonance band 48-52 Hz that must be passed quickly).
    fire_when = "outside"

    # Whether the bound itself belongs to [low, high].
    #   True  (default): value == low / high is inside   -> [low, high]
    #   False:           value == low / high is outside  -> (low, high)
    # With fire_when="outside" an inside value is normal; with "inside" it fires.
    lower_inclusive = True
    upper_inclusive = True

    # After firing, the value must move this far back before the alert clears:
    #   outside: 0.5 with high = 30.0 clears only at 29.5 or below.
    #   inside:  0.5 with [48, 52] clears only below 47.5 or above 52.5.
    hysteresis = 0.0

    # An input with no value (a probe without an answer, an empty column) is not
    # judged and leaves the rule OK. True: it fires too. For data that stops coming,
    # a StaleRule on the same input is the usual check (it also sees a dead source).
    missing_fires = False
