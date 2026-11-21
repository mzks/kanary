# kanary-template: file-log
# kind: output
# level: basic
# summary: Append every alert event to a JSON Lines file (FileOutput).
# summary-ja: すべての alert event を JSON Lines の file に追記する (FileOutput)。
import kanary


@kanary.output(
    output_id="template.file_log",
    # Record everything, including silenced and suppressed alerts.
    exclude_states=[],
)
class TemplateFileLog(kanary.FileOutput):
    description = "TODO: say who reads this file."

    # Relative paths are next to this plugin file. Rotate with logrotate (copytruncate).
    path = "alerts.jsonl"

    # One JSON object per line: rule_id, state, previous_state, transition, severity,
    # message, owner, tags, occurred_at, active_since, acked_by. True adds the rule's
    # payload (the judged values and extra) as "payload", for later analysis.
    include_payload = True

    # To reshape a line, override record(); kanary.event_record(event) is the default:
    # def record(self, event):
    #     return {**kanary.event_record(event), "host": "daq01"}
