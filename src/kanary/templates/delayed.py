# kanary-template: delayed
# kind: output
# level: advanced
# summary: Notify only when an alert is still active after a delay, like a "for" / fire_after (OutputFollowups).
import kanary


@kanary.output(
    output_id="template.delayed",
    minimum_severity="WARN",
    # Receive SILENCED / SUPPRESSED too, so a pending notification can be cancelled.
    exclude_states=[],
)
class TemplateDelayed:
    description = "TODO: describe who is notified and after how long."

    # The alert is visible in the UI and other outputs right away; only this output waits.
    # To decide that a short spike is not an alert at all, do it in the source instead
    # (see the "buffered" template: a range rule on the window minimum).
    delay = 5 * kanary.minute

    def init(self):
        self.followups = kanary.OutputFollowups()
        # Rules whose FIRING was sent, so their recovery is sent too. No lock is needed:
        # emit() and the followup callbacks all run on this output's thread, one at a time.
        self.notified = set()

    def terminate(self):
        self.followups.close()

    def emit(self, event):
        followups = self.followups.for_event(event)

        if event.current_state == kanary.FIRING and event.previous_state != kanary.FIRING:
            # Runs after delay (between emit() calls), with the latest event of the rule.
            followups.after(self.delay, self.send_firing)
            return

        if event.current_state in {kanary.ACKED, kanary.SILENCED, kanary.SUPPRESSED, kanary.OK}:
            followups.cancel()  # still pending: nothing was sent, so nothing to take back
            was_notified = event.rule_id in self.notified
            self.notified.discard(event.rule_id)
            if was_notified and event.current_state == kanary.OK:
                self.send(event)

    def send_firing(self, event):
        self.notified.add(event.rule_id)
        self.send(event)

    def send(self, event):
        # TODO: replace with mail, chat, or pager delivery. Use a timeout.
        print(f"{event.rule_id} {event.current_state.value}: {event.message}")
