# kanary-template: peer
# kind: source
# level: basic
# summary: Watch another KANARY: alert when it does not answer or its plugins fail (source and rules in one file); put one on each node to watch each other.
# summary-ja: 別の KANARY を見張る: 応答しない、plugin が失敗している、で発報する（source と rule が 1 file）。両方の node に置けば相互監視になる。
import kanary


@kanary.source(source_id="template.peer", interval=30 * kanary.second)
class TemplatePeer(kanary.HTTPJSONSource):
    description = "TODO: which KANARY this watches."

    url = "http://peer-kanary.example:8000/peer-status"  # TODO: the other node's API
    timeout = 5.0

    def poll(self):
        # No answer is what this source reports (up = 0), not a failure of its own.
        try:
            status = self.fetch_json()
        except Exception as exc:
            return kanary.inputs(("up", 0, None, {"error": f"{type(exc).__name__}: {exc}"}))
        counts = status.get("counts") or {}
        firing = (status.get("alert_states") or {}).get("FIRING")
        return kanary.inputs(("up", 1), ("failed_plugins", counts.get("failed_plugins")), ("firing", firing))


@kanary.rule(
    rule_id="template.peer.down",
    inputs="template.peer:up",
    severity=kanary.ERROR,
    tags=["kanary"],
)
class PeerDown(kanary.ExpectedValueRule):
    description = "The other KANARY does not answer: it, its host, or the network between is down."
    runbook = "TODO: how to reach the other host; kanaryctl --base-url http://peer-kanary.example:8000 plugins"
    expected = 1


@kanary.rule(
    rule_id="template.peer.plugins_failed",
    inputs="template.peer:failed_plugins",
    severity=kanary.WARN,
    tags=["kanary"],
    suppressed_by="template.peer.down",  # no answer says it all
)
class PeerPluginsFailed(kanary.RangeRule):
    description = "Plugins of the other KANARY fail: it runs, but does not watch everything."
    high = 0


# Put the same file on the other node with this node's URL, and each watches the
# other. Both may stop together (one power line, one network): for that, add an
# outside watch as well (the "heartbeat" template).
