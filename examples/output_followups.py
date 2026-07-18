import kanary
from kanary import hour as h


@kanary.output(output_id="operations-followups",
               minimum_severity="WARN",
               exclude_states=[]) # remove "SILENCED" and "SUPPRESSED" from default `exclude_states`.
class OperationsFollowupOutput:
    def init(self):
        self.followups = kanary.OutputFollowups()

    def terminate(self):
        self.followups.close()

    def emit(self, event):
        followups = self.followups.for_event(event)

        if event.current_state == kanary.FIRING and event.previous_state != kanary.FIRING :

            followups.now(self.report_to_expert)
            followups.after(1.*h, self.post_group_discord)
            followups.after(2.*h, self.post_mailing_list)
            return

        if event.current_state in { kanary.ACKED, kanary.SILENCED, kanary.OK, kanary.SUPPRESSED }:
            followups.cancel()

        if event.transition == kanary.ESCALATED:
            followups.cancel()
            followups.now(self.post_mailing_list)

    def report_to_expert(self, event):
        pass

    def post_group_discord(self, event):
        pass

    def post_mailing_list(self, event):
        pass
