# kanary-template: remote
# kind: source
# level: advanced
# summary: Mirror alerts from another Kanary node (RemoteKanarySource + import_remote_alarms).
# summary-ja: 別の Kanary node の alert を映す (RemoteKanarySource + import_remote_alarms)。
import kanary


@kanary.source(source_id="template.remote", interval=30 * kanary.second)
class TemplateRemote(kanary.RemoteKanarySource):
    description = "TODO: describe the remote Kanary node."

    base_url = "http://remote-kanary.example:8000"  # TODO
    timeout = 5.0
    # url = "http://remote-kanary.example:8000/export-alerts"  # full URL instead of base_url


# Create one local RemoteAlarm rule per remote alert. Discovery contacts the remote node
# when this file is loaded; pass remote_alarm_ids=[...] to avoid that.
# kanary.import_remote_alarms(
#     source="template.remote",
#     prefix="remote.",              # local rule_id = prefix + remote rule_id + suffix
#     add_tags=["remote"],
#     include_tags=["detector"],     # only import matching remote alerts
#     exclude_rule_ids=["*.debug"],
#     severity=kanary.ERROR,         # used when the remote alert has no severity
#     propagate_ack=True,            # local ack/unack is forwarded to the remote node
#     propagate_silence=True,        # local silences are forwarded too
# )
