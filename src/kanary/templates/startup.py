# kanary-template: startup
# kind: output
# level: basic
# summary: Post one message when KANARY starts, e.g. to know that monitoring is back after a power cut (Slack here; any chat output works).
# summary-ja: KANARY の起動時にメッセージを 1 つ送る。例: 停電の後に監視が戻ったことを知る（ここでは Slack。どの chat output でもよい）。
from datetime import datetime
import os
import socket

import kanary


@kanary.output(
    output_id="template.startup",
    # It sends no alerts, only the message below.
    exclude_states=["OK", "FIRING", "ACKED", "SILENCED", "SUPPRESSED"],
)
class TemplateStartup(kanary.SlackOutput):
    description = "Says once that KANARY started (or this output was reloaded)."

    url = os.environ.get("SLACK_WEBHOOK_URL")  # TODO: or the webhook of your channel

    def init(self):
        super().init()
        # init() runs again when the output recovers from a failed send: say it once.
        # If the destination cannot be reached yet, init() fails and is tried again.
        if not getattr(self, "announced", False):
            started = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
            self.post_json({"text": f"KANARY started on {socket.gethostname()} at {started}"})
            self.announced = True


# For Discord, subclass kanary.DiscordOutput and post {"content": ...}; for
# Mattermost, kanary.MattermostOutput and {"text": ...}; for mail, kanary.MailOutput.
