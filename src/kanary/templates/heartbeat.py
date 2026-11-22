# kanary-template: heartbeat
# kind: source
# level: basic
# summary: Tell an outside service that this KANARY is alive (healthchecks.io, Uptime Kuma push, ...); it alerts you when the pings stop.
# summary-ja: この KANARY が生きていることを外部の service に伝える（healthchecks.io、Uptime Kuma の push など）。ping が止まると、その service が知らせてくれる。
import kanary


@kanary.source(source_id="template.heartbeat", interval=1 * kanary.minute)
class TemplateHeartbeat(kanary.HTTPStatusSource):
    description = "Pings an outside dead man's switch: when the pings stop, it alerts (KANARY, its host, or its network is down)."

    # The ping URL of the check; set the check's period a little longer than the
    # interval above (and a grace time), so one late ping does not alert.
    urls = {"ping": "https://hc-ping.com/TODO-check-uuid"}  # TODO
    timeout = 10.0


@kanary.rule(
    rule_id="template.heartbeat.not_delivered",
    inputs="template.heartbeat:ping.status",
    severity=kanary.WARN,
    tags=["kanary"],
)
class HeartbeatNotDelivered(kanary.ExpectedValueRule):
    description = "The heartbeat did not reach the outside service, which will soon alert the people it notifies."
    runbook = "TODO: check this host's network to the outside, and the service's status page."
    expected = 200
    missing_fires = True  # no answer at all fires too


# This is the watch KANARY cannot do for itself: from inside, it cannot report that
# it stopped. To ping only while its own plugins work, read them in poll() first
# (the "plugin-health" template) and skip the request while one fails.
