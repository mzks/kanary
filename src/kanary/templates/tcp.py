# kanary-template: tcp
# kind: source
# level: basic
# summary: Connect to host:port and report the connect time; refused or timed out leaves it empty (TCPSource).
# summary-ja: host:port に接続し、接続にかかった時間を出す。拒否や timeout なら値なし (TCPSource)。
import kanary


@kanary.source(source_id="template.tcp", interval=1 * kanary.minute)
class TemplateTcp(kanary.TCPSource):
    description = "TODO: describe the services that must accept connections."

    # Input name -> "host:port". "daq" below produces "daq.connect_ms", how long the TCP
    # connect took. A refused or timed-out connect gets an input with no value and
    # the error text in its metadata. Nothing is sent after connecting.
    targets = {
        "daq": "daq01.example.org:5000",  # TODO
        "db": "db.example.org:5432",
    }
    timeout = 5.0  # seconds per target; targets are checked in parallel


# Alert when a port does not accept connections:
#
#   @kanary.rule(rule_id="template.tcp.down", inputs="template.tcp:*.connect_ms",
#                severity=kanary.ERROR, tags=["network"])
#   class PortDown(kanary.RangeRule):
#       high = 1000.0         # ms; a very slow connect also fires
#       missing_fires = True  # refused or timed out fires
