# kanary-template: output
# kind: output
# level: basic
# summary: Deliver alert events somewhere, with every routing filter shown.
import kanary


@kanary.output(
    output_id="template.output",
    # Routing: an event reaches emit() only when it passes every filter below.
    # include_tags=["detector"],           # alert must have one of these tags (globs allowed)
    # exclude_tags=["debug", "test-*"],    # alert must have none of these tags
    # minimum_severity="WARN",             # INFO / WARN / ERROR / CRITICAL
    # exclude_states=[],                   # default ["SUPPRESSED", "SILENCED"]; [] receives every state
    # exclude_states=kanary.Output.exclude_states + ["OK"],  # also skip recoveries (no "back to OK" message)
    # exclude_transitions=["UNACK"],       # also "ESCALATED", "DEESCALATED"
)
class TemplateOutput:
    description = "TODO: describe where this output sends alerts."

    # Retries after an emit() failure, then terminate() + init() attempts.
    max_retry = 1
    max_reinit = 1

    def init(self):
        # Create clients here, not at module top level.
        #   config = kanary.load_toml("my_output", filename="config.toml")
        pass

    def emit(self, event):
        # emit() runs on this output's own thread, one event at a time; a slow call
        # delays only this output's queue. Use finite timeouts for external I/O.
        # Raise to trigger retries.
        transition = f" ({event.transition.value})" if event.transition else ""
        previous = event.previous_state.value if event.previous_state else "-"
        print(
            f"{event.occurred_at.isoformat()} {event.rule_id} "
            f"{previous} -> {event.current_state.value}{transition} "
            f"{kanary.severity_label(event.effective_severity)} {event.message or ''}"
        )
        # Other fields: event.owner, event.tags, event.payload, event.active_since,
        # event.acked_by, event.ack_reason, event.active_silence_ids,
        # event.previous_severity, event.current_severity.
        # effective_severity keeps the firing severity on the event that returns to OK.

    def terminate(self):
        pass
