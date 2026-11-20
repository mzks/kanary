# kanary-template: ping
# kind: source
# level: basic
# summary: Ping hosts and report the round-trip time; no reply leaves the value empty (PingSource).
import kanary


@kanary.source(source_id="template.ping", interval=1 * kanary.minute)
class TemplatePing(kanary.PingSource):
    description = "TODO: describe the hosts that must answer."

    # Input name -> host. "gateway" below produces "gateway.rtt_ms", the round-trip time of one
    # ICMP echo (system ping command). A host that does not answer within timeout gets
    # an input with no value and the error text in its metadata.
    hosts = {
        "gateway": "192.0.2.1",  # TODO
        "daq01": "daq01.example.org",
    }
    timeout = 5.0  # seconds per host; hosts are pinged in parallel


# Alert when a host does not answer, or answers slowly:
#
#   @kanary.rule(rule_id="template.ping.slow_or_down", inputs="template.ping:*.rtt_ms",
#                severity=kanary.ERROR, tags=["network"])
#   class PingSlowOrDown(kanary.RangeRule):
#       high = 200.0          # ms
#       missing_fires = True  # no reply fires too
