# kanary-template: threshold
# kind: rule
# level: basic
# summary: Raise severity step by step as a value crosses several thresholds (ThresholdRule).
import kanary


@kanary.rule(
    rule_id="template.threshold",
    # With several inputs, the highest matched severity among them wins.
    inputs="example.source:temperature",  # TODO
    severity=kanary.WARN,  # rule default; each evaluation uses the matched threshold's severity
    tags=["TODO"],
    # owner="detector-team",
)
class TemplateThreshold(kanary.ThresholdRule):
    description = "TODO: describe what each level means."
    # runbook = "TODO: what the operator should check first."

    # "high": fire when value >= threshold.  "low": fire when value <= threshold.
    direction = "high"

    # (threshold, severity) pairs; the most severe matched pair wins.
    thresholds = [
        (40.0, kanary.WARN),
        (50.0, kanary.ERROR),
        (60.0, kanary.CRITICAL),
    ]

    # Margin needed before severity drops or the rule clears (e.g. back below 38.0 for WARN).
    hysteresis = 2.0

    # An input with no value (a probe without an answer, an empty column) is not
    # judged and leaves the rule OK. True: it fires too. For data that stops coming,
    # a StaleRule on the same input is the usual check (it also sees a dead source).
    missing_fires = False
