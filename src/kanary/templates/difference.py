# kanary-template: difference
# kind: rule
# level: advanced
# summary: Fire when redundant inputs disagree by more than a limit (DifferenceRule).
# summary-ja: 冗長な input どうしの差が上限を超えたら発報する (DifferenceRule)。
import kanary


@kanary.rule(
    rule_id="template.difference",
    # Two or more inputs that should read the same; globs and lists both work.
    inputs=["example.source:thermometer_a", "example.source:thermometer_b"],  # TODO
    severity=kanary.WARN,
    tags=["TODO"],
)
class TemplateDifference(kanary.DifferenceRule):
    description = "TODO: describe why these inputs should agree."
    # runbook = "TODO: which one to trust, and how to recalibrate."

    # Fires when highest - lowest > max_difference.
    max_difference = 0.5
    # Below this many inputs with values the rule stays OK (pair it with a StaleRule).
    min_inputs = 2
