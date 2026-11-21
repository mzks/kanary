# kanary-template: webhook
# kind: output
# level: basic
# summary: POST each alert event as JSON to an HTTP endpoint; reshape the body in payload() (WebhookOutput).
# summary-ja: 各 alert event を JSON で HTTP の endpoint に POST する。body の形は payload() で変える (WebhookOutput)。
import os

import kanary


@kanary.output(
    output_id="template.webhook",
    # include_tags=["detector"],
    minimum_severity=kanary.WARN,
)
class TemplateWebhook(kanary.WebhookOutput):
    description = "TODO: describe the receiving service."

    # headers = {"X-Source": "kanary"}  # extra request headers (a secret one: see http-json)
    timeout = 5.0  # a hung server holds this output's queue for up to ~3x this (with retries)
    max_retry = 2

    # The URL is often a secret: keep it out of this file. Here it comes from an
    # environment variable (set it in the systemd unit, an EnvironmentFile, or the shell
    # that starts kanary); without it url stays None and this output fails its init()
    # with "url is not set".
    url = os.environ.get("WEBHOOK_URL")  # TODO
    # Or from a JSON file next to this one (chmod 600, not in git), read when this
    # file is loaded (a URL that is not secret can be a plain string): {"webhook_url": "https://hooks.example/..."}
    # url = kanary.load_json("webhook_url", filename="secrets.json")

    def payload(self, event):
        # The JSON body. The default (kept here) sends rule_id, state, previous_state,
        # transition, severity, message, owner, tags, occurred_at, active_since, acked_by,
        # and runbook.
        body = super().payload(event)
        body["source"] = "kanary"
        # self.text(event) gives a one-line summary such as
        #   "[ERROR] FIRING oil.temperature: value=95 out of range [10, 80]"
        body["summary"] = self.text(event)
        return body
