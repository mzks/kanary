# kanary-template: remote
# kind: source
# level: advanced
# summary: Mirror alerts from another Kanary node (RemoteKanarySource + import_remote_alarms).
# summary-ja: 別の Kanary node の alert を映す (RemoteKanarySource + import_remote_alarms)。
import kanary


@kanary.source(source_id="template.remote", interval=30 * kanary.second)
class TemplateRemote(kanary.RemoteKanarySource):
    description = "TODO: describe the remote Kanary node."

    base_url = "http://remote-kanary.example:8000"  # TODO: the node's address, without /export-alerts
    timeout = 5.0


# Create one local RemoteAlarm rule per remote alert. The ids are written out, so this
# file loads even when the remote node is down.
# kanary.import_remote_alarms(
#     source="template.remote",
#     remote_alarm_ids=["daq.link", "hv.crate1.range"],  # TODO: the remote rule ids
#     prefix="remote",               # local rule_id = "remote." + remote rule_id (e.g. remote.daq.link)
#     add_tags=["remote"],
#     severity=kanary.ERROR,         # until the remote alert gives its own
#     propagate_ack=True,            # local ack/unack is forwarded to the remote node
#     propagate_silence=True,        # local silences are forwarded too
# )
