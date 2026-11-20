# kanary-template: oncall
# kind: output
# level: advanced
# summary: Mail whoever is on shift, from a schedule file, and hold mail at night (MailOutput + OnCallSchedule + QuietHours).
import kanary


@kanary.output(
    output_id="template.oncall",
    minimum_severity="WARN",
)
class TemplateOncall(kanary.MailOutput):
    description = "TODO: describe the shift arrangement."

    # SMTP settings as in the "mail" template; recipients come from the schedule.
    smtp_host = "smtp.example.org"  # TODO
    sender = "kanary@example.org"   # TODO
    subject_prefix = "[KANARY]"

    def init(self):
        super().init()
        # oncall.toml next to this file, for example:
        #
        #   timezone = "Asia/Tokyo"                  # or "+09:00"; default: host time
        #   default = ["control-room@example.org"]   # when no shift matches
        #
        #   [[shift]]                                # weekly
        #   weekdays = ["mon", "tue", "wed", "thu", "fri"]
        #   from = "09:00"
        #   to = "18:00"
        #   recipients = ["day-shift@example.org"]
        #
        #   [[shift]]                                # overnight; wraps past midnight
        #   weekdays = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
        #   from = "18:00"
        #   to = "09:00"
        #   recipients = ["night-shift@example.org"]
        #
        #   [[shift]]                                # dated; wins over weekly shifts
        #   start = 2026-12-28T09:00:00
        #   end = 2027-01-04T09:00:00
        #   recipients = ["holiday@example.org"]
        #
        # The file is read in init(), so reload the plugin after editing it.
        config = kanary.load_toml(filename="oncall.toml")
        self.schedule = kanary.OnCallSchedule(
            config.get("shift", []),
            timezone=config.get("timezone"),
            default=config.get("default", []),
        )
        # At night only CRITICAL is mailed at once; the rest is held and mailed at 07:00,
        # in order (see the "quiet-hours" template for the details).
        self.quiet = kanary.QuietHours(
            [("23:00", "07:00")],
            timezone=config.get("timezone"),
            minimum_severity=kanary.CRITICAL,
        )

    def terminate(self):
        self.quiet.close()  # mail still held is dropped (and logged)
        super().terminate()

    def emit(self, event):
        self.quiet.send(event, super().emit)

    def recipients_for(self, event):
        # Held mail goes to whoever is on shift when the event happened.
        return self.schedule.recipients(event.occurred_at)
