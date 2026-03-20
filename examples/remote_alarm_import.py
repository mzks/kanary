import kanary


@kanary.source(source_id="remote", interval=60.0)
class UpstreamKanarySource(kanary.RemoteKanarySource):
    base_url = "http://127.0.0.1:8000"


@kanary.rule(
    rule_id="upstream.sqlite.connection.failed",
    source="remote",
    severity=kanary.ERROR,
    tags=["remote", "upstream"],
    owner="operator_name",
)
class ImportedUpstreamConnectionFailed(kanary.RemoteAlarm):
    remote_alarm_id = "sqlite.connection.failed"
    propagate_ack = True
    propagate_silence = True


kanary.import_remote_alarms(
    source="remote",
    remote_alarm_ids=[
        "sqlite.value1.range",
        "sqlite.value2.range",
        "sqlite.values.balance",
    ],
    prefix="upstream",
    add_tags=["remote", "upstream"],
    propagate_ack=True,
    propagate_silence=False,
)
