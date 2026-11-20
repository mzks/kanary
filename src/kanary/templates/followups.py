# kanary-template: followups
# kind: output
# level: advanced
# summary: Escalate over time - notify now, again after a delay, cancel on ack (OutputFollowups).
import kanary


@kanary.output(
    output_id="template.followups",
    minimum_severity="WARN",
    # Receive SILENCED / SUPPRESSED too, so pending followups can be cancelled.
    exclude_states=[],
)
class TemplateFollowups:
    description = "TODO: describe the escalation steps."

    def init(self):
        # In memory: pending followups are lost on reload and restart.
        self.followups = kanary.OutputFollowups()

    def terminate(self):
        self.followups.close()

    def emit(self, event):
        followups = self.followups.for_event(event)

        if event.current_state == kanary.FIRING and event.previous_state != kanary.FIRING:
            followups.now(self.notify_owner)  # runs right away, inside emit()
            # Delays count from the first registration of this alert episode. The callback
            # receives the latest event of the rule at the time it runs.
            followups.after(30 * kanary.minute, self.notify_group)
            followups.after(2 * kanary.hour, self.notify_mailing_list)
            return

        if event.current_state in {kanary.ACKED, kanary.SILENCED, kanary.SUPPRESSED, kanary.OK}:
            followups.cancel()  # cancel everything pending for this rule
            # followups.cancel(self.notify_group)  # or only one callback
            return

        if event.transition == kanary.ESCALATED:
            followups.cancel()
            followups.now(self.notify_mailing_list)

    # Callbacks run on this output's thread, between emit() calls (now() runs inside
    # emit()), so they never overlap with emit() and need no locks. Use timeouts: a slow
    # callback delays this output's next event. An exception from now() propagates to
    # emit() and triggers retries; one from a later callback is logged and shown on the
    # Outputs page.
    def notify_owner(self, event):
        print(f"owner: {event.rule_id} {event.message}")

    def notify_group(self, event):
        print(f"group: {event.rule_id} still {event.current_state.value}")

    def notify_mailing_list(self, event):
        print(f"mailing list: {event.rule_id} {kanary.severity_label(event.effective_severity)}")
