# kanary-template: mail
# kind: output
# level: basic
# summary: Send alert events by e-mail over SMTP (MailOutput).
# summary-ja: SMTP で alert event を mail で送る (MailOutput)。
import kanary


@kanary.output(
    output_id="template.mail",
    # include_tags=["detector"],
    minimum_severity=kanary.ERROR,
)
class TemplateMail(kanary.MailOutput):
    description = "TODO: describe who receives these mails."

    smtp_host = "smtp.example.org"  # TODO
    smtp_port = 587
    use_starttls = True
    smtp_timeout = 10.0
    smtp_username = None  # None: send without logging in

    sender = "kanary@example.org"  # TODO
    recipients = ["operator@example.org"]  # TODO
    subject_prefix = "[KANARY]"

    # Gmail: send through Google's SMTP server with an app password.
    #   smtp_host = "smtp.gmail.com"
    #   smtp_port = 587               # STARTTLS (use_starttls = True)
    #   smtp_username = "you@gmail.com"
    #   sender = "you@gmail.com"      # Gmail rewrites other From addresses to the login
    #   smtp_password: a 16-character app password, not the account password
    #     (Google Account > Security > 2-Step Verification must be on > App passwords).
    #     Workspace admins can disable app passwords; ask them or use your own SMTP relay.
    # Gmail limits sending (about 500 recipients a day for a personal account), so keep
    # minimum_severity high or route frequent events to a chat output instead.

    # With a login, the password is a secret: keep it out of this file, for example
    # in an environment variable (import os), or in secrets.json next to this file
    # (chmod 600, not in git) as {"smtp_password": "..."}:
    # smtp_password = os.environ.get("SMTP_PASSWORD")
    # smtp_password = kanary.load_json("smtp_password", filename="secrets.json")

    # Each mail has a plain-text part and an HTML part: a colored header
    # ("🔴 FIRING ERROR oil.temperature"), the rule's message, and a table of state,
    # severity, since, ack, owner, tags, and the judged values. Link it to the viewer:
    # viewer_url = "http://kanary.example.org:8000/"

    # To change it, override subject(), body() (plain text), or html() (None: text only);
    # kanary.describe(event) gives the parts:
    # def subject(self, event):
    #     return f"{self.subject_prefix} {kanary.describe(event).title}"
