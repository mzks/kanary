"""suppressed_by / depends_on, and which outputs an OK goes to."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

import kanary
from kanary.validation import validate_registries

T0 = datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc)


def source(source_id):
    return type(source_id, (kanary.Source,), {"source_id": source_id, "interval": 60.0, "poll": lambda self: kanary.no_update()})


def link_rule(inputs="daq:link"):
    return type("Link", (kanary.ExpectedValueRule,), {"rule_id": "link", "inputs": inputs, "expected": 1, "tags": ["t"]})


def hv_rule(rule_id="hv1", inputs="daq:hv1", **attrs):
    return type("HV", (kanary.RangeRule,), {
        "rule_id": rule_id, "inputs": inputs, "low": 1000.0, "high": 2000.0, "tags": ["t"], **attrs,
    })


class Recorder(kanary.Output):
    output_id = "chat"

    def __init__(self):
        self.sent = []

    def emit(self, event):
        previous = event.previous_state.value if event.previous_state else None
        self.sent.append((event.rule_id, previous, event.current_state.value))


class DependencyCase(unittest.TestCase):
    def setUp(self):
        self.now = T0

    def start(self, sources, rules, store=None):
        engine = kanary.Engine(
            source_registry={s.source_id: s for s in sources},
            rule_registry={r.rule_id: r for r in rules},
            output_registry={"chat": Recorder},
            store=store,
            now_fn=lambda: self.now,
        )
        engine.start()
        self.addCleanup(engine.shutdown)
        return engine

    def poll(self, engine, *polls):
        """polls: (source_id, {input: value}) in the order the sources are read; returns what was sent."""
        self.now += timedelta(minutes=1)
        output = engine.outputs["chat"]
        output.sent.clear()
        for source_id, values in polls:
            items = [(name, value, self.now) for name, value in values.items()]
            engine.evaluate_source(source_id, kanary.inputs(*items), now=self.now)
        self.assertTrue(engine.flush_outputs(5))
        return list(output.sent)

    def states(self, engine):
        return {rule_id: alert.state.value for rule_id, alert in engine.alerts.items()}


class SuppressedByTest(DependencyCase):
    def test_a_parent_is_judged_first_and_its_children_are_not_sent(self):
        # The HV rules are defined before the link rule they depend on.
        rules = [hv_rule("hv1", "daq:hv1", suppressed_by=["link"]), hv_rule("hv2", "daq:hv2", suppressed_by="link"), link_rule()]
        engine = self.start([source("daq")], rules)
        self.poll(engine, ("daq", {"link": 1, "hv1": 1500, "hv2": 1500}))
        sent = self.poll(engine, ("daq", {"link": 0, "hv1": 0, "hv2": 0}))
        self.assertEqual(sent, [("link", "OK", "FIRING")])
        self.assertEqual(self.states(engine), {"link": "FIRING", "hv1": "SUPPRESSED", "hv2": "SUPPRESSED"})
        self.assertEqual(
            engine.alerts["hv1"].message, "suppressed by link; would fire: daq:hv1=0 out of range [1000.0, 2000.0]"
        )
        # Back in the same poll as the link; their OK closes nothing that was sent.
        sent = self.poll(engine, ("daq", {"link": 1, "hv1": 1500, "hv2": 1500}))
        self.assertEqual(sent, [("link", "FIRING", "OK")])
        self.assertEqual(self.states(engine), {"link": "OK", "hv1": "OK", "hv2": "OK"})

    def test_a_rule_already_sent_as_firing_gets_its_ok(self):
        engine = self.start([source("daq")], [link_rule(), hv_rule(suppressed_by=["link"])])
        self.assertEqual(self.poll(engine, ("daq", {"link": 1, "hv1": 2500})), [("hv1", None, "FIRING")])
        self.assertEqual(self.poll(engine, ("daq", {"link": 0, "hv1": 0})), [("link", "OK", "FIRING")])
        sent = self.poll(engine, ("daq", {"link": 1, "hv1": 1500}))
        self.assertEqual(sent, [("link", "FIRING", "OK"), ("hv1", "SUPPRESSED", "OK")])

    def test_a_child_on_another_source_waits_for_a_value_read_after_the_recovery(self):
        engine = self.start([source("net"), source("crate")], [link_rule("net:link"), hv_rule("hv1", "crate:hv1", suppressed_by=["link"])])
        self.poll(engine, ("net", {"link": 1}), ("crate", {"hv1": 1500}))
        self.poll(engine, ("net", {"link": 0}))
        self.assertEqual(self.states(engine)["hv1"], "SUPPRESSED")  # at once, before crate is read again
        self.poll(engine, ("crate", {"hv1": 0}))
        sent = self.poll(engine, ("net", {"link": 1}))
        self.assertEqual(sent, [("link", "FIRING", "OK")])  # not hv1 FIRING from the 0 read while down
        self.assertEqual(self.states(engine)["hv1"], "SUPPRESSED")
        self.assertIn("recovered; waiting for a new value", engine.alerts["hv1"].message)
        self.assertEqual(self.poll(engine, ("crate", {"hv1": 1500})), [])
        self.assertEqual(self.states(engine)["hv1"], "OK")

    def test_silencing_or_acking_the_parent_does_not_hold_its_children(self):
        engine = self.start([source("daq")], [link_rule(), hv_rule(suppressed_by=["link"])])
        self.poll(engine, ("daq", {"link": 1, "hv1": 1500}))
        engine.create_silence(operator="op", start_at=self.now, end_at=self.now + timedelta(hours=1), rule_patterns=["link"])
        self.assertEqual(self.poll(engine, ("daq", {"link": 1, "hv1": 2500})), [("hv1", "OK", "FIRING")])
        self.assertEqual(self.states(engine), {"link": "SILENCED", "hv1": "FIRING"})

    def test_a_failing_evaluate_still_shows_suppressed(self):
        class Strict(kanary.Rule):
            rule_id = "hv1"
            inputs = "daq:hv1"
            tags = ["t"]
            suppressed_by = ["link"]

            def evaluate(self, ctx):
                return kanary.fire_if(ctx.value() > 2000, "too high")  # no value: None > 2000 raises

        engine = self.start([source("daq")], [link_rule(), Strict])
        self.poll(engine, ("daq", {"link": 1, "hv1": 1500}))
        with self.assertLogs("kanary.engine", level="ERROR"):
            self.poll(engine, ("daq", {"link": 0, "hv1": None}))
        self.assertEqual(self.states(engine)["hv1"], "SUPPRESSED")
        self.assertEqual(engine.plugin_states["rule:hv1"].state, "FAILED")


class DependsOnTest(DependencyCase):
    def counting_rule(self):
        calls = []

        class Mean(kanary.Rule):
            rule_id = "hv1"
            inputs = "daq:hv1"
            tags = ["t"]
            depends_on = ["link"]

            def evaluate(self, ctx):
                calls.append(ctx.value())
                return kanary.fire_if(ctx.value() < 1000, f"hv1={ctx.value()}")

        return Mean, calls

    def test_not_evaluated_while_the_parent_fires(self):
        rule, calls = self.counting_rule()
        engine = self.start([source("daq")], [rule, link_rule()])
        self.poll(engine, ("daq", {"link": 1, "hv1": 1500}))
        self.assertEqual(self.poll(engine, ("daq", {"link": 0, "hv1": 0})), [("link", "OK", "FIRING")])
        self.assertEqual(calls, [1500])
        self.assertEqual(self.states(engine)["hv1"], "SUPPRESSED")
        self.assertEqual(engine.alerts["hv1"].message, "blocked by link (not evaluated)")
        self.assertEqual(self.poll(engine, ("daq", {"link": 1, "hv1": 1500})), [("link", "FIRING", "OK")])
        self.assertEqual(calls, [1500, 1500])  # evaluated in the poll the link came back
        self.assertEqual(self.states(engine)["hv1"], "OK")

    def test_a_firing_child_is_not_told_ok_while_blocked(self):
        # It used to turn OK "blocked by link": a recovery that had not happened.
        rule, _calls = self.counting_rule()
        engine = self.start([source("daq")], [link_rule(), rule])
        self.assertEqual(self.poll(engine, ("daq", {"link": 1, "hv1": 500})), [("hv1", None, "FIRING")])
        self.assertEqual(self.poll(engine, ("daq", {"link": 0, "hv1": 0})), [("link", "OK", "FIRING")])
        sent = self.poll(engine, ("daq", {"link": 1, "hv1": 500}))
        self.assertEqual(sent, [("link", "FIRING", "OK"), ("hv1", "SUPPRESSED", "FIRING")])


class HysteresisJudgmentTest(DependencyCase):
    def test_a_silenced_ok_rule_keeps_its_normal_range(self):
        # was_alerting() follows the rule's own judgment, not the SILENCED shown.
        engine = self.start([source("daq")], [hv_rule(hysteresis=200.0)])
        self.poll(engine, ("daq", {"hv1": 1900}))
        engine.create_silence(operator="op", start_at=self.now, end_at=self.now + timedelta(hours=1), rule_patterns=["hv1"])
        self.poll(engine, ("daq", {"hv1": 1900}))
        self.assertEqual(engine._rule_evaluations["hv1"].state, kanary.OK)


class OkOnlyWhereSentTest(DependencyCase):
    def test_silence_ok_goes_where_firing_went(self):
        engine = self.start([source("daq")], [hv_rule("hv1", "daq:hv1"), hv_rule("hv2", "daq:hv2")])
        self.poll(engine, ("daq", {"hv1": 2500, "hv2": 1500}))
        silence = engine.create_silence(operator="op", start_at=self.now, end_at=self.now + timedelta(hours=1), rule_patterns=["hv*"])
        self.poll(engine, ("daq", {"hv1": 1500, "hv2": 1500}))
        engine.cancel_silence(silence.silence_id, operator="op")
        self.assertTrue(engine.flush_outputs(5))
        self.assertEqual(engine.outputs["chat"].sent, [("hv1", "SILENCED", "OK")])

    def test_after_a_restart_a_restored_alert_gets_its_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "state.db"
            engine = self.start([source("daq")], [hv_rule()], store=kanary.SQLiteStore(db))
            self.poll(engine, ("daq", {"hv1": 2500}))
            engine.shutdown()
            engine = self.start([source("daq")], [hv_rule()], store=kanary.SQLiteStore(db))
            self.assertEqual(self.poll(engine, ("daq", {"hv1": 1500})), [("hv1", "FIRING", "OK")])

    def test_a_held_ok_is_in_the_dispatch_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = self.start([source("daq")], [link_rule(), hv_rule(suppressed_by=["link"])], store=kanary.SQLiteStore(Path(tmp) / "s.db"))
            self.poll(engine, ("daq", {"link": 1, "hv1": 1500}))
            self.poll(engine, ("daq", {"link": 0, "hv1": 0}))
            self.poll(engine, ("daq", {"link": 1, "hv1": 1500}))
            dispatches = engine.get_rule_history("hv1")["output_dispatches"]
            self.assertEqual((dispatches[0]["current_state"], dispatches[0]["filtered_outputs"]), ("OK", ["chat"]))


class DependencyLintTest(unittest.TestCase):
    def lint(self, *rules):
        return validate_registries(sources={"daq": source("daq")}, rules={r.rule_id: r for r in rules}, outputs={"chat": Recorder})

    def test_cycles_and_self_references_are_errors(self):
        a = hv_rule("a", suppressed_by=["b"])
        b = hv_rule("b", depends_on=["c"])
        c = hv_rule("c", suppressed_by="a")
        report = self.lint(a, b, c, hv_rule("d", suppressed_by=["d"]))
        self.assertIn("rules 'a' -> 'b' -> 'c' -> 'a' hold each other back (suppressed_by / depends_on form a cycle)", report.errors)
        self.assertIn("rule 'd' lists itself in suppressed_by", report.errors)

    def test_an_unknown_parent_is_a_warning(self):
        report = self.lint(link_rule(), hv_rule(suppressed_by=["lnk"]))
        self.assertTrue(report.ok, report.errors)
        self.assertTrue(any("suppressed_by 'lnk' is not a loaded rule" in w and "did you mean 'link'" in w for w in report.warnings))


if __name__ == "__main__":
    unittest.main()
