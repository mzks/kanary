# kanary-template: discord
# kind: output
# level: basic
# summary: Post alerts to a Discord channel through a webhook (DiscordOutput).
import os

import kanary


@kanary.output(
    output_id="template.discord",
    minimum_severity="WARN",
)
class TemplateDiscord(kanary.DiscordOutput):
    description = "TODO: name the channel."

    username = "kanary"
    timeout = 5.0
    max_retry = 2

    # The webhook URL (Channel settings > Integrations > Webhooks) is a secret: keep it
    # out of this file. Here it comes from an environment variable (set it in the
    # systemd unit, an EnvironmentFile, or the shell that starts kanary); without it url
    # stays None and this output fails its init() with "url is not set".
    url = os.environ.get("DISCORD_WEBHOOK_URL")  # TODO
    # Or from a JSON file next to this one (chmod 600, not in git), read when this
    # file is loaded: {"discord_webhook_url": "https://discord.com/api/webhooks/..."}
    # url = kanary.load_json("discord_webhook_url", filename="secrets.json")

    # Each event is an embed with a colored bar, "🔴 FIRING ERROR oil.temperature",
    # the rule's message, fields, and the time (in each reader's own time zone);
    # link the title to the viewer:
    # viewer_url = "http://kanary.example.org:8000/"
    # To change it, override payload(); kanary.describe(event) gives the parts.
