# kanary-template: stale
# kind: rule
# level: basic
# summary: Fire when an input stops updating for longer than a timeout (StaleRule).
# summary-ja: input が timeout より長く更新されなければ発報する (StaleRule)。
import kanary


@kanary.rule(
    rule_id="template.stale",
    # The input's timestamp decides its age. With a glob, any stale input fires the rule.
    inputs="example.source:temperature",  # TODO
    severity=kanary.ERROR,
    tags=["TODO"],
    # Without a timer, a source that stops polling also stops this rule from running,
    # so stale detection needs evaluation_interval.
    evaluation_interval=30 * kanary.second,
)
class TemplateStale(kanary.StaleRule):
    description = "TODO: describe what stopped when this fires."
    # runbook = "TODO: e.g. check that the DAQ writer is running."

    # Maximum age in seconds of the newest value.
    timeout = 5 * kanary.minute
