# kanary-template: aggregate
# kind: rule
# level: advanced
# summary: Check the total or average of many inputs at once against a range, e.g. the summed current of all channels (AggregateRule).
import kanary


@kanary.rule(
    rule_id="template.aggregate",
    inputs="example.source:ch*.current",  # TODO
    severity=kanary.WARN,
    tags=["TODO"],
)
class TemplateAggregate(kanary.AggregateRule):
    description = "TODO: describe the total that must stay in range."

    # The inputs are combined at one moment (the current snapshot), across inputs;
    # for one input over time (an average of the last 10 minutes), use kanary.Buffer.

    # "sum", "mean", "median", "min", or "max" of the inputs that have numeric values.
    aggregate = "sum"
    # The range for the aggregate, exactly as in RangeRule.
    low = None
    high = 120.0
    hysteresis = 2.0
