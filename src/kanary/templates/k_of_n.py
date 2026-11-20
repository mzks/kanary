# kanary-template: k-of-n
# kind: rule
# level: advanced
# summary: Fire when at least k of many inputs are out of range, e.g. "3 channels dead" (KOfNRule).
import kanary


@kanary.rule(
    rule_id="template.k_of_n",
    inputs="example.source:ch*.current",  # TODO: one input per channel
    severity=kanary.ERROR,
    tags=["TODO"],
)
class TemplateKOfN(kanary.KOfNRule):
    description = "TODO: describe when this many bad channels matters."

    # Fire when k or more inputs break the range.
    k = 3
    # The range per input, exactly as in RangeRule (fire_when, *_inclusive also work).
    low = 0.5
    high = 10.0
    # Count inputs that are present but have no value as bad too.
    count_missing = False
    # The bad input names are listed in the alert message and payload["bad_inputs"].
