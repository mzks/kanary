# kanary-template: count
# kind: rule
# level: advanced
# summary: Fire when fewer (or more) inputs than expected report a value (CountRule).
import kanary


@kanary.rule(
    rule_id="template.count",
    inputs="example.source:ch*.current",  # TODO
    severity=kanary.ERROR,
    tags=["TODO"],
)
class TemplateCount(kanary.CountRule):
    description = "TODO: describe how many inputs should report."

    # Fire when the number of inputs with a value is below min_count or above max_count.
    # Either may be None.
    min_count = 64
    max_count = None
    # Missing names are listed in payload["missing_inputs"].
