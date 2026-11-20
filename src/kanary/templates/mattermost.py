# kanary-template: mattermost
# kind: output
# level: basic
# summary: Post alerts to Mattermost through an incoming webhook (MattermostOutput).
import os

import kanary


@kanary.output(
    output_id="template.mattermost",
    minimum_severity="WARN",
)
class TemplateMattermost(kanary.MattermostOutput):
    description = "TODO: name the channel."

    username = "kanary"  # shown as the sender when the server allows overriding it
    timeout = 5.0
    max_retry = 2

    # The webhook URL (Integrations > Incoming Webhooks) is a secret: keep it out of
    # this file. Here it comes from an environment variable (set it in the systemd unit,
    # an EnvironmentFile, or the shell that starts kanary); without it url stays None
    # and this output fails its init() with "url is not set".
    url = os.environ.get("MATTERMOST_WEBHOOK_URL")  # TODO
    # Or from a JSON file next to this one (chmod 600, not in git), read when this
    # file is loaded: {"mattermost_webhook_url": "https://mattermost.example.org/hooks/..."}
    # url = kanary.load_json("mattermost_webhook_url", filename="secrets.json")

    # icon_url = "https://example.org/kanary.png"
    # Each event is an attachment with a colored bar, "🔴 FIRING ERROR oil.temperature",
    # the rule's message, and fields; link the title to the viewer:
    # viewer_url = "http://kanary.example.org:8000/"
    # To change it, override payload(); kanary.describe(event) gives the parts.
