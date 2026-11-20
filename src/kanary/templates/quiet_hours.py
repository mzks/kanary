# kanary-template: quiet-hours
# kind: output
# level: advanced
# summary: Hold non-urgent alerts at night and send them in the morning, in order (QuietHours).
import os

import kanary


@kanary.output(
    output_id="template.quiet_hours",
    minimum_severity="WARN",
)
class TemplateQuietHours(kanary.SlackOutput):
    description = "TODO: describe the channel and the quiet hours."

    timeout = 5.0

    url = os.environ.get("SLACK_WEBHOOK_URL")  # TODO: see the "slack" template

    def init(self):
        super().init()  # checks that url is set
        # Inside these windows only events at minimum_severity or above go out at once;
        # the rest are held until the quiet period ends (back-to-back windows count as
        # one period; windows may wrap past midnight). timezone is an IANA name
        # ("Asia/Tokyo", "Europe/Berlin"), an offset ("+09:00", "+9", "UTC+9"), or "UTC";
        # without it, the host's local time.
        self.quiet = kanary.QuietHours(
            [("22:00", "07:00"), ("12:00", "13:00")],  # TODO
            timezone="Asia/Tokyo",
            minimum_severity=kanary.CRITICAL,
            # What goes out when the quiet period ends:
            #   "all"      every held event, in order (one message each)
            #   "latest"   only the newest event of each rule
            #   "summary"  one message listing each rule's newest state and how many
            #              events it had; summary= sends it (emit_summary of the output)
            release="summary",
            summary=self.emit_summary,
            # defer=False drops the held events instead
        )
        # Held events are kept in memory: they are lost on reload, stop, and restart.

    def terminate(self):
        self.quiet.close()

    def emit(self, event):
        # super().emit posts to Slack: now (a failure propagates, so the engine retries as
        # usual), or later for a held event, on this output's thread either way; a failure
        # for a held event is logged and not retried. When an urgent event of a rule goes
        # out at night, that rule's held events are dropped as older ("all": sent first).
        self.quiet.send(event, super().emit)

    # Other calls:
    #   self.quiet.applies(event)  True when event would be held (or dropped) now
    #   self.quiet.held()          [(release_at, event), ...] still waiting
