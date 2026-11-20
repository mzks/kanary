# kanary-template: multi-source
# kind: rule
# level: advanced
# summary: Combine inputs from several sources in one rule (e.g. compare two sensors).
import kanary


@kanary.rule(
    rule_id="template.multi_source",
    # Any source update re-evaluates the rule with the latest snapshot of every source.
    inputs=[
        "example.primary:temperature",  # TODO
        "example.secondary:temperature",  # TODO
    ],
    severity=kanary.WARN,
    tags=["TODO"],
    # Re-check even when neither source updates (uses saved snapshots).
    # evaluation_interval=1 * kanary.minute,
)
class TemplateMultiSource:
    description = "TODO: describe what disagreement between the sources means."
    # runbook = "TODO: what the operator should check first."

    max_difference = 2.0
    max_age = 5 * kanary.minute

    def evaluate(self, ctx):
        # With several sources, always name the input; ctx.value() alone is ambiguous.
        primary = ctx.value("example.primary:temperature")
        secondary = ctx.value("example.secondary:temperature")

        # Declared inputs that matched nothing or have no value (or pass selectors explicitly).
        missing = ctx.missing()
        if missing:
            return kanary.warn(f"missing input: {', '.join(missing)}")

        # Every selected input, across sources: InputView(name, source_id, input_name, value, timestamp, metadata).
        # ctx.age(name) is the seconds since that input's timestamp.
        too_old = [item.name for item in ctx.inputs() if (ctx.age(item.name) or 0) > self.max_age]
        if too_old:
            return kanary.warn(f"inputs too old to compare: {', '.join(too_old)}")

        # Whole snapshot of one source (status, metadata, channels): ctx.source_payload("example.primary")
        difference = abs(primary - secondary)
        return kanary.warn_if(
            difference > self.max_difference,
            f"primary={primary} and secondary={secondary} differ by {difference:.2f}",
            extra={"difference": difference},
        ) or kanary.ok(f"sensors agree within {difference:.2f}")
