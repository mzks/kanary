# kanary-template: command-output
# kind: output
# level: advanced
# summary: Run a script for each event, e.g. a phone call system or a warning light (CommandOutput).
# summary-ja: event ごとに script を実行する。例: 電話の自動発信や警告灯 (CommandOutput)。
import kanary


@kanary.output(
    output_id="template.command_output",
    minimum_severity=kanary.ERROR,
)
class TemplateCommandOutput(kanary.CommandOutput):
    description = "TODO: describe what the script does."

    # A list runs as-is (no shell). The script receives:
    #   stdin: the event as JSON (see record())
    #   env:   KANARY_RULE_ID, KANARY_STATE, KANARY_PREVIOUS_STATE, KANARY_TRANSITION,
    #          KANARY_SEVERITY, KANARY_MESSAGE, KANARY_OWNER, KANARY_TAGS
    # A non-zero exit raises, so the event is retried (max_retry).
    command = ["/usr/local/bin/notify-oncall"]  # TODO
    timeout = 10.0  # this output's next event waits while the script runs; keep it short
    # env = {"NOTIFY_PROFILE": "detector"}

    # def args(self, event):
    #     # Build the command per event, e.g. pass the rule id as an argument.
    #     return [*self.command, event.rule_id]
