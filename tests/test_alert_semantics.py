"""Acks that end with their alert, diagnostics that leave the live state alone, input freshness
for held rules, NaN inputs, and the severity an OK closes with."""

from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import tempfile
import threading
import time
import unittest

import kanary
from kanary.helpers.remote import RemoteKanarySource, _compose_local_rule_id
from kanary.store import build_store
from kanary.values import is_number, json_ready

T0 = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)


def minutes(n):
    return T0 + timedelta(minutes=n)


def source(source_id):
    return type(source_id, (kanary.Source,), {"source_id": source_id, "interval": 60.0, "poll": lambda self: kanary.no_update()})


def range_rule(rule_id="hv", inputs="daq:hv", **attrs):
    return type("HV", (kanary.RangeRule,), {"rule_id": rule_id, "inputs": inputs, "high": 25, "tags": ["t"], **attrs})


class Recorder(kanary.Output):
    output_id = "chat"

    def __init__(self):
        self.sent = []

    def emit(self, event):
        self.sent.append(event)


class EngineCase(unittest.TestCase):
    def setUp(self):
        self.now = T0
        self.db = Path(tempfile.mkdtemp()) / "state.db"

    def start(self, rules, *, sources=("daq",), outputs=None, store=True):
        engine = kanary.Engine(
            source_registry={source_id: source(source_id) for source_id in sources},
            rule_registry={rule.rule_id: rule for rule in rules},
            output_registry=outputs if outputs is not None else {"chat": Recorder},
            store=build_store(self.db) if store else None,
            now_fn=lambda: self.now,
        )
        engine.start()
        self.addCleanup(engine.shutdown)
        return engine

    def value(self, engine, value, at, source_id="daq", name="hv"):
        self.now = at
        engine.evaluate_source(source_id, kanary.inputs((name, value, at)), now=at)
        return engine.alerts.get(name if source_id == "daq" and name == "hv" else None)

    def sent(self, engine):
        engine.flush_outputs(5)
        return [(event.current_state.value, event.acked_by) for event in engine.outputs["chat"].sent]


class AcknowledgementLifetimeTest(EngineCase):
    """An ack belongs to one alert: it ends when the rule judges that alert over."""

    def test_an_ack_ends_when_the_alert_recovers_during_a_silence(self):
        engine = self.start([range_rule()])
        self.value(engine, 50, minutes(1))
        engine.acknowledge("hv", operator="alice")
        self.now = minutes(2)
        silence = engine.create_silence(operator="bob", start_at=minutes(2), end_at=minutes(60), rule_patterns=["hv"])
        self.value(engine, 0, minutes(3))  # recovers while silenced
        self.value(engine, 50, minutes(4))  # a new alert, still silenced
        self.now = minutes(5)
        engine.cancel_silence(silence.silence_id, operator="bob")

        alert = engine.alerts["hv"]
        self.assertEqual((alert.state, alert.acked_by), (kanary.FIRING, None))
        actions = engine.get_rule_history("hv")["operator_actions"]
        closing = next(action for action in actions if action["action_type"] == "unack")
        self.assertEqual(closing["operator"], "system")
        self.assertIn("recovered while SILENCED", closing["reason"])

    def test_an_ack_goes_on_while_the_alert_stays_firing_under_a_silence(self):
        engine = self.start([range_rule()])
        self.value(engine, 50, minutes(1))
        engine.acknowledge("hv", operator="alice")
        self.now = minutes(2)
        silence = engine.create_silence(operator="bob", start_at=minutes(2), end_at=minutes(60), rule_patterns=["hv"])
        self.value(engine, 60, minutes(3))
        self.now = minutes(4)
        engine.cancel_silence(silence.silence_id, operator="bob")
        alert = engine.alerts["hv"]
        self.assertEqual((alert.state, alert.acked_by), (kanary.ACKED, "alice"))

    def test_an_ack_ends_when_the_rule_recovers_while_suppressed(self):
        link = type("Link", (kanary.ExpectedValueRule,), {"rule_id": "link", "inputs": "daq:link", "expected": 1, "tags": ["t"]})
        engine = self.start([link, range_rule(suppressed_by="link")])
        self.now = minutes(1)
        engine.evaluate_source("daq", kanary.inputs(("link", 1, minutes(1)), ("hv", 50, minutes(1))), now=minutes(1))
        engine.acknowledge("hv", operator="alice")
        self.now = minutes(2)
        engine.evaluate_source("daq", kanary.inputs(("link", 0, minutes(2)), ("hv", 0, minutes(2))), now=minutes(2))
        self.assertEqual(engine.alerts["hv"].state, kanary.SUPPRESSED)
        self.assertNotIn("hv", engine.acknowledgements)

    def test_a_remote_alarm_acknowledged_at_its_peer_keeps_the_local_ack(self):
        # A RemoteAlarm judges the remote state: ACKED there is not the end of the alert here.
        from kanary.helpers.remote import RemoteAlarm

        alarm = type("A", (RemoteAlarm,), {"rule_id": "remote.link", "source": "peer", "remote_alarm_id": "link", "tags": ["t"]})
        engine = self.start([alarm], sources=("peer",))

        def remote(state, at):
            self.now = at
            metadata = {"state": state, "severity": "ERROR", "message": "link down"}
            engine.evaluate_source("peer", kanary.inputs(("link", 1, at, metadata)), now=at)

        remote("FIRING", minutes(1))
        engine.acknowledge("remote.link", operator="alice")
        remote("ACKED", minutes(2))
        alert = engine.alerts["remote.link"]
        self.assertEqual((alert.state, alert.acked_by), (kanary.ACKED, "alice"))
        self.assertIn("remote.link", engine.acknowledgements)
        actions = engine.get_rule_history("remote.link")["operator_actions"]
        self.assertEqual([action["action_type"] for action in actions], ["ack"])
        remote("OK", minutes(3))
        self.assertNotIn("remote.link", engine.acknowledgements)

    def restart(self, engine, at):
        engine.shutdown()
        self.now = at
        return self.start(list(engine._rule_registry.values()))

    def test_an_unack_during_a_silence_is_kept_over_a_restart(self):
        engine = self.start([range_rule()])
        self.value(engine, 50, minutes(1))
        engine.acknowledge("hv", operator="alice")
        self.now = minutes(2)
        engine.create_silence(operator="bob", start_at=minutes(2), end_at=minutes(60), rule_patterns=["hv"])
        self.now = minutes(3)
        engine.unacknowledge("hv", operator="alice")

        engine = self.restart(engine, minutes(4))
        self.assertEqual(engine.acknowledgements, {})

    def test_an_ack_of_an_earlier_alert_is_not_restored(self):
        engine = self.start([range_rule()])
        self.value(engine, 50, minutes(1))
        engine.acknowledge("hv", operator="alice")
        self.value(engine, 0, minutes(2))
        self.value(engine, 50, minutes(3))
        self.now = minutes(4)
        engine.create_silence(operator="bob", start_at=minutes(4), end_at=minutes(60), rule_patterns=["hv"])

        engine = self.restart(engine, minutes(5))
        self.assertEqual(engine.acknowledgements, {})

    def test_an_ack_of_the_alert_still_going_is_restored(self):
        engine = self.start([range_rule()])
        self.value(engine, 50, minutes(1))
        engine.acknowledge("hv", operator="alice")
        self.now = minutes(2)
        engine.create_silence(operator="bob", start_at=minutes(2), end_at=minutes(60), rule_patterns=["hv"])

        engine = self.restart(engine, minutes(3))
        self.assertEqual(engine.acknowledgements["hv"].operator, "alice")
        engine = self.restart(engine, minutes(4))
        self.assertEqual(engine.acknowledgements["hv"].operator, "alice")

    def test_an_ack_of_a_removed_rule_is_not_restored(self):
        engine = self.start([range_rule()])
        self.value(engine, 50, minutes(1))
        engine.acknowledge("hv", operator="alice")
        self.now = minutes(2)
        engine.reload_rule_plugins(replacements={}, removed_rule_ids={"hv"})
        engine = self.restart(engine, minutes(3))
        engine.reload_rule_plugins(replacements={"hv": range_rule()})
        engine = self.restart(engine, minutes(4))
        self.assertEqual(engine.acknowledgements, {})


class Counting(kanary.Rule):
    """Fires on the third high value in a row: keeps its count on self."""

    rule_id = "count"
    inputs = "daq:hv"
    tags = ["t"]

    def __init__(self):
        self.breaches = 0

    def evaluate(self, ctx):
        self.breaches = self.breaches + 1 if ctx.value("daq:hv") > 10 else 0
        return kanary.fire_if(self.breaches >= 3, f"{self.breaches} in a row")


class Locked(Counting):
    rule_id = "locked"

    def __init__(self):
        super().__init__()
        self.lock = threading.Lock()  # cannot be copied


class FollowupOutput(kanary.Output):
    output_id = "chat"
    exclude_states = []

    def init(self):
        self.followups = kanary.OutputFollowups()
        self.ran = []

    def terminate(self):
        self.followups.close()

    def emit(self, event):
        followups = self.followups.for_event(event)
        if event.current_state == kanary.FIRING:
            followups.after(0.2, lambda ev: self.ran.append((ev.rule_id, ev.synthetic)))
        elif event.current_state == kanary.OK:
            followups.cancel()


class DiagnosticsTest(EngineCase):
    """test-evaluate and test-fire leave the live rule and the live notifications alone."""

    def test_test_evaluate_does_not_change_the_live_rule(self):
        engine = self.start([Counting], store=False)
        self.now = minutes(1)
        engine.evaluate_source("daq", kanary.inputs(("hv", 50, minutes(1))), now=minutes(1))
        judged = [engine.test_evaluate("count", {"value": 50}, now=minutes(2))["state"] for _ in range(3)]
        self.assertEqual(judged, ["OK"] * 3)  # each counts on from the live count (1), none changes it
        self.assertEqual(engine.rules["count"].breaches, 1)
        engine.evaluate_source("daq", kanary.inputs(("hv", 50, minutes(3))), now=minutes(3))
        self.assertEqual(engine.alerts["count"].state, kanary.OK)
        engine.evaluate_source("daq", kanary.inputs(("hv", 50, minutes(4))), now=minutes(4))
        self.assertEqual(engine.alerts["count"].state, kanary.FIRING)
        self.assertEqual(engine.alerts["count"].message, "3 in a row")

    def test_a_rule_that_cannot_be_copied_is_judged_by_a_new_instance(self):
        engine = self.start([Locked], store=False)
        engine.evaluate_source("daq", kanary.inputs(("hv", 50, minutes(1))), now=minutes(1))
        with self.assertLogs("kanary.engine", "INFO"):
            result = engine.test_evaluate("locked", {"value": 50}, now=minutes(2))
        self.assertEqual(result["state"], "OK")
        self.assertEqual(engine.rules["locked"].breaches, 1)

    def test_a_test_fire_ok_does_not_cancel_the_live_followups(self):
        engine = self.start([range_rule()], outputs={"chat": FollowupOutput}, store=False)
        self.value(engine, 50, minutes(1))
        engine.flush_outputs(5)
        engine.test_fire("hv", state=kanary.OK, reason="output check")
        output = engine.outputs["chat"]
        deadline = time.monotonic() + 3
        while not output.ran and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(output.ran, [("hv", False)])

    def test_a_test_fire_firing_has_its_own_followups(self):
        engine = self.start([range_rule()], outputs={"chat": FollowupOutput}, store=False)
        engine.test_fire("hv", state=kanary.FIRING, reason="output check")
        self.value(engine, 50, minutes(1))
        self.value(engine, 0, minutes(2))  # the live alert ends: only its followups are cancelled
        output = engine.outputs["chat"]
        deadline = time.monotonic() + 3
        while not output.ran and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(output.ran, [("hv", True)])

    def test_quiet_hours_keep_a_test_fire_apart_from_held_events(self):
        released = []
        quiet = kanary.QuietHours([("00:00", "23:59")], timezone="UTC", release="latest", now_fn=lambda: minutes(0))
        self.addCleanup(quiet.close)
        alert = kanary.Alert(rule_id="hv", state=kanary.FIRING, severity=kanary.WARN)

        def event(synthetic, severity):
            return kanary.AlertEvent(
                rule_id="hv", previous_state=None, current_state=kanary.FIRING, previous_severity=None,
                current_severity=severity, transition=None, alert=alert, occurred_at=minutes(1), synthetic=synthetic,
            )

        self.assertFalse(quiet.send(event(False, kanary.WARN), released.append))  # held
        self.assertTrue(quiet.send(event(True, kanary.CRITICAL), released.append))  # sent now
        self.assertEqual([held.synthetic for _, held in quiet.held()], [False])  # the real one is still held


class HeldRuleFreshnessTest(EngineCase):
    def test_a_rule_on_two_sources_waits_for_both_after_its_parent_recovers(self):
        link = type("Link", (kanary.ExpectedValueRule,), {"rule_id": "link", "inputs": "net:up", "expected": 1, "tags": ["t"]})
        both = range_rule("sum", inputs=["a:v", "b:v"], suppressed_by="link")
        engine = self.start([link, both], sources=("net", "a", "b"), store=False)

        def give(source_id, name, value, at):
            self.now = at
            engine.evaluate_source(source_id, kanary.inputs((name, value, at)), now=at)

        give("net", "up", 1, minutes(0))
        give("a", "v", 0, minutes(0))
        give("b", "v", 0, minutes(0))
        give("net", "up", 0, minutes(1))  # link down
        give("b", "v", 99, minutes(2))  # read during the trouble
        give("net", "up", 1, minutes(3))  # link back
        give("a", "v", 0, minutes(4))  # only A is new
        self.assertEqual(engine.alerts["sum"].state, kanary.SUPPRESSED)
        self.assertIn("waiting for a new value", engine.alerts["sum"].message)
        give("b", "v", 0, minutes(5))  # B is new too
        self.assertEqual(engine.alerts["sum"].state, kanary.OK)


class NonFiniteInputTest(EngineCase):
    def test_nan_is_a_missing_value(self):
        engine = self.start([range_rule(missing_fires=True)], store=False)
        engine.evaluate_source("daq", kanary.inputs(("hv", float("nan"), minutes(1))), now=minutes(1))
        alert = engine.alerts["hv"]
        self.assertEqual(alert.state, kanary.FIRING)
        self.assertEqual(alert.message, "no value: daq:hv (value is nan)")
        current = engine.source_states["daq"].current.payload["inputs"]["hv"]
        self.assertEqual((current["value"], current["metadata"]["error"]), (None, "value is nan"))

    def test_infinity_in_a_dict_result_is_a_missing_value(self):
        engine = self.start([range_rule()], store=False)
        engine.evaluate_source("daq", {"hv": float("inf")}, now=minutes(1))
        self.assertEqual(engine.alerts["hv"].message, "no value: daq:hv (value is inf)")

    def test_test_evaluate_reads_nan_like_a_poll_does(self):
        engine = self.start([range_rule(missing_fires=True)], store=False)
        result = engine.test_evaluate("hv", {"value": float("nan")}, now=minutes(1))
        self.assertEqual((result["state"], result["message"]), ("FIRING", "no value: daq:hv (value is nan)"))

    def test_payloads_are_written_as_standard_json(self):
        self.assertEqual(json_ready({"a": [1.5, float("nan"), (float("-inf"),)], "b": {"c": float("inf")}}),
                         {"a": [1.5, None, [None]], "b": {"c": None}})
        rule = type("Raw", (kanary.Rule,), {
            "rule_id": "raw", "inputs": "daq:hv", "tags": ["t"],
            "evaluate": lambda self, ctx: kanary.firing("odd", extra={"ratio": float("nan")}),
        })
        engine = self.start([rule])
        engine.evaluate_source("daq", kanary.inputs(("hv", 1, minutes(1))), now=minutes(1))
        events = engine.get_rule_history("raw")["alert_events"]
        self.assertIsNone(events[-1]["payload"]["ratio"])

    def test_is_number_is_false_for_nan_and_infinity(self):
        self.assertEqual([is_number(v) for v in (1, 1.5, float("nan"), float("inf"), True, 10**400)],
                         [True, True, False, False, False, True])
        self.assertTrue(math.isnan(float("nan")))  # json_ready leaves the caller's data alone


class RecoverySeverityTest(EngineCase):
    def test_an_ok_reaches_the_output_that_got_the_alert_after_the_severity_went_down(self):
        rule = type("T", (kanary.ThresholdRule,), {
            "rule_id": "hv", "inputs": "daq:hv", "tags": ["t"],
            "thresholds": [(50, kanary.WARN), (90, kanary.CRITICAL)],
        })
        pager = type("Pager", (Recorder,), {"output_id": "chat", "minimum_severity": kanary.ERROR})
        engine = self.start([rule], outputs={"chat": pager}, store=False)
        for at, value in ((1, 95), (2, 60), (3, 0)):
            self.value(engine, value, minutes(at))
        engine.flush_outputs(5)
        sent = [(event.current_state.value, event.effective_severity.name) for event in engine.outputs["chat"].sent]
        self.assertEqual(sent, [("FIRING", "CRITICAL"), ("OK", "CRITICAL")])

    def test_an_ok_still_skips_an_output_that_never_got_the_alert(self):
        pager = type("Pager", (Recorder,), {"output_id": "chat", "minimum_severity": kanary.CRITICAL})
        engine = self.start([range_rule(severity=kanary.WARN)], outputs={"chat": pager}, store=False)
        self.value(engine, 50, minutes(1))
        self.value(engine, 0, minutes(2))
        engine.flush_outputs(5)
        self.assertEqual(engine.outputs["chat"].sent, [])


class RemoteTemplateTest(unittest.TestCase):
    def test_a_prefix_with_or_without_its_dot_gives_the_same_id(self):
        self.assertEqual(_compose_local_rule_id("daq.link", prefix="remote", suffix=None), "remote.daq.link")
        self.assertEqual(_compose_local_rule_id("daq.link", prefix="remote.", suffix=".mirror"), "remote.daq.link.mirror")

    def test_a_url_that_ends_with_the_alerts_path_is_taken_without_it(self):
        for url in ("http://peer:8000", "http://peer:8000/", "http://peer:8000/export-alerts", "http://peer:8000/export-alerts/"):
            remote = type("Peer", (RemoteKanarySource,), {"source_id": "peer", "url": url})()
            self.assertEqual(remote._base_url(), "http://peer:8000", url)


if __name__ == "__main__":
    unittest.main()
