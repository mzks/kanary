# kanary-template: slack
# kind: output
# level: basic
# summary: Post alerts to a Slack channel through an incoming webhook (SlackOutput).
import os

import kanary


@kanary.output(
    output_id="template.slack",
    minimum_severity="WARN",
    # include_tags=["detector"],
)
class TemplateSlack(kanary.SlackOutput):
    description = "TODO: name the channel."

    timeout = 5.0
    max_retry = 2

    # The webhook URL (Slack app > Incoming Webhooks) is a secret: keep it out of this
    # file. Here it comes from an environment variable (set it in the systemd unit, an
    # EnvironmentFile, or the shell that starts kanary); without it url stays None and
    # this output fails its init() with "url is not set".
    url = os.environ.get("SLACK_WEBHOOK_URL")  # TODO
    # Or from a JSON file next to this one (chmod 600, not in git), read when this
    # file is loaded: {"slack_webhook_url": "https://hooks.slack.com/services/..."}
    # url = kanary.load_json("slack_webhook_url", filename="secrets.json")

    # Each event is a message with a colored bar (green OK, grey silenced, else by
    # severity), "🔴 FIRING ERROR oil.temperature", the rule's message, and fields
    # (state, severity, since, ack, owner, tags, the judged values). Notifications show
    # the one line self.text(event). Link the title to the viewer:
    # viewer_url = "http://kanary.example.org:8000/"

    # To change it, override payload(); kanary.describe(event) gives the parts. For
    # example, mention the channel for ERROR and above:
    # def payload(self, event):
    #     body = super().payload(event)
    #     if event.current_state == kanary.FIRING and event.effective_severity >= kanary.ERROR:
    #         body["text"] = "<!channel> " + body["text"]
    #     return body
