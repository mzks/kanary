"""Operator actions on the API, --exclude, severities in any spelling, and matched outputs."""

from datetime import datetime, timedelta, timezone
import json
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import kanary
from kanary.constants import coerce_severity
from kanary.filtering import apply_excludes
from kanary.loader import RegistrySnapshot

T0 = datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc)


def source(source_id):
    return type(source_id, (kanary.Source,), {"source_id": source_id, "interval": 60.0, "poll": lambda self: kanary.no_update()})


def range_rule(rule_id="hv1", inputs="daq:hv1", **attrs):
    attrs.setdefault("tags", ["t"])
    return type("R", (kanary.RangeRule,), {"rule_id": rule_id, "inputs": inputs, "high": 2000.0, **attrs})


class Quiet(kanary.Output):
    output_id = "chat"

    def emit(self, event):
        return None


def start_engine(case, rules, outputs=None):
    engine = kanary.Engine(
        source_registry={"daq": source("daq")},
        rule_registry={rule.rule_id: rule for rule in rules},
        output_registry=outputs if outputs is not None else {"chat": Quiet},
        now_fn=lambda: T0,
    )
    engine.start()
    case.addCleanup(engine.shutdown)
    return engine


class OperatorActionTest(unittest.TestCase):
    def setUp(self):
        self.engine = start_engine(self, [range_rule("hv1"), range_rule("hv2", "daq:hv2"), range_rule("never", "daq:nothing")])
        self.engine.evaluate_source("daq", kanary.inputs(("hv1", 2500, T0), ("hv2", 1500, T0)), now=T0)
        self.reloads = []
        api = kanary.ControlAPI(engine_getter=lambda: self.engine, reload_callback=self.reload, host="127.0.0.1", port=0)
        thread = threading.Thread(target=api.start, daemon=True)
        thread.start()
        self.addCleanup(lambda: (api.shutdown(), thread.join(timeout=2.0)))
        self.base = f"http://127.0.0.1:{api._server.server_address[1]}"

    def reload(self, request):
        self.reloads.append(request)
        raise TypeError("a bug in a plugin")

    def post(self, path, body):
        request = Request(self.base + path, method="POST", data=json.dumps(body).encode())
        request.add_header("Content-Type", "application/json")
        try:
            with urlopen(request) as response:
                return response.status, json.loads(response.read().decode())
        except HTTPError as exc:
            return exc.code, json.loads(exc.read().decode())

    def test_only_a_firing_alert_can_be_acknowledged(self):
        self.assertEqual(self.post("/alerts/hv1/ack", {"operator": "op"})[0], 200)
        status, body = self.post("/alerts/hv1/ack", {"operator": "op"})
        self.assertEqual((status, body["error"]), (409, "rule 'hv1' is ACKED: only a FIRING alert can be acknowledged"))
        status, body = self.post("/alerts/hv2/ack", {"operator": "op"})
        self.assertEqual((status, body["error"]), (409, "rule 'hv2' is OK: only a FIRING alert can be acknowledged"))
        self.assertEqual(self.post("/alerts/never/ack", {"operator": "op"})[0], 409)  # WAITING
        status, body = self.post("/alerts/nosuch/ack", {"operator": "op"})
        self.assertEqual((status, body["error"]), (404, "unknown rule 'nosuch'"))
        self.assertEqual(self.post("/alerts/hv1/ack", {}), (400, {"error": "missing field: operator"}))

    def test_unack_and_unsilence_say_what_is_wrong(self):
        self.assertEqual(self.post("/alerts/hv2/unack", {"operator": "op"})[0], 409)
        self.assertEqual(self.post("/alerts/nosuch/unack", {"operator": "op"})[0], 404)
        self.assertEqual(self.post("/silences/nosuch/cancel", {"operator": "op"})[0], 404)
        silence = self.engine.create_silence(operator="op", start_at=T0, end_at=T0 + timedelta(hours=1), rule_patterns=["hv*"])
        self.assertEqual(self.post(f"/silences/{silence.silence_id}/cancel", {"operator": "op"})[0], 200)
        self.assertEqual(self.post(f"/silences/{silence.silence_id}/cancel", {"operator": "op"})[0], 409)

    def test_a_type_error_in_a_reload_is_reported_not_retried_as_a_full_reload(self):
        status, body = self.post("/reload", {"rule": "hv*"})
        self.assertEqual((status, body["error"]), (400, "a bug in a plugin"))
        self.assertEqual(self.reloads, [{"rule": "hv*"}])


class ExcludeTest(unittest.TestCase):
    def test_excluding_a_source_drops_only_the_rules_that_read_nothing_else(self):
        snapshot = RegistrySnapshot(
            sources={"daq": source("daq"), "net": source("net")},
            rules={
                "hv1": range_rule("hv1", "daq:hv1"),
                "both": range_rule("both", ["daq:hv1", "net:link"]),
                "link": range_rule("link", source="net", inputs=None),
            },
            outputs={},
        )
        kept = apply_excludes(snapshot, ["daq"])
        self.assertEqual((sorted(kept.sources), sorted(kept.rules)), (["net"], ["both", "link"]))
        self.assertEqual(sorted(apply_excludes(snapshot, ["hv*"]).rules), ["both", "link"])

    def test_an_engine_given_no_rules_has_none(self):
        engine = start_engine(self, [])
        self.assertEqual(engine.rules, {})


class SeverityTest(unittest.TestCase):
    def test_every_spelling_is_accepted(self):
        for spelling in (kanary.WARN, "WARN", "Warn", "warn", " warn ", 20):
            self.assertIs(coerce_severity(spelling), kanary.WARN, spelling)
        for wrong in ("loud", 25, True, None):
            with self.assertRaises((TypeError, ValueError)):
                coerce_severity(wrong)

    def test_rules_take_a_name_and_default_to_error(self):
        class Plain:  # not a subclass of kanary.Rule, no severity
            rule_id = "plain"
            inputs = "daq:hv1"
            tags = ["t"]

            def evaluate(self, ctx):
                return ("warn", "tuple form")

        engine = start_engine(self, [range_rule("named", severity="critical"), Plain])
        self.assertIs(engine.rules["named"].severity, kanary.CRITICAL)
        self.assertIs(engine.rules["plain"].severity, kanary.ERROR)
        alerts = engine.evaluate_source("daq", kanary.inputs(("hv1", 2500, T0)), now=T0)
        self.assertEqual((alerts["named"].severity, alerts["plain"].severity), (kanary.CRITICAL, kanary.WARN))

    def test_thresholds_take_names(self):
        rule = type("T", (kanary.ThresholdRule,), {
            "rule_id": "level", "inputs": "daq:hv1", "tags": ["t"],
            "thresholds": [(1800, "warn"), (2200, "Critical")],
        })
        engine = start_engine(self, [rule])
        alert = engine.evaluate_source("daq", kanary.inputs(("hv1", 2500, T0)), now=T0)["level"]
        self.assertEqual((alert.state, alert.severity), (kanary.FIRING, kanary.CRITICAL))

    def test_json_carries_the_name(self):
        engine = start_engine(self, [range_rule()])
        result = engine.test_evaluate("hv1", {"inputs": {"daq:hv1": {"value": 2500}}})
        self.assertEqual(result["severity"], "ERROR")


class EvaluateLintTest(unittest.TestCase):
    def lint(self, code):
        import tempfile
        import textwrap
        from pathlib import Path

        from kanary.validation import validate_plugin_files

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rules.py"
            path.write_text(textwrap.dedent(code))
            return validate_plugin_files([path]).warnings

    def test_a_dropped_result_and_a_firing_without_message(self):
        warnings = self.lint("""
            import kanary

            class Hot:
                def evaluate(self, ctx):
                    if ctx.value() > 30:
                        kanary.firing("too hot")
                    if ctx.value() > 40:
                        return kanary.error()
                    if ctx.value() > 50:
                        return True
                    return kanary.warn_if(ctx.value() > 25)
        """)
        self.assertEqual(warnings, [
            "file 'rules.py' line 7: Hot.evaluate() calls kanary.firing(...) without returning it, "
            "so the rule stays OK; write return kanary.firing(...)",
            "file 'rules.py' line 9: Hot.evaluate() fires with kanary.error() without a message; "
            "the notification shows only the rule id",
            "file 'rules.py' line 11: Hot.evaluate() returns True, a FIRING without a message; "
            "the notification shows only the rule id",
            "file 'rules.py' line 12: Hot.evaluate() fires with kanary.warn_if() without a message; "
            "the notification shows only the rule id",
        ])

    def test_falling_through_to_ok_is_fine(self):
        self.assertEqual(self.lint("""
            import kanary

            class Lazy:
                def evaluate(self, ctx):
                    if ctx.value() > 30:
                        return kanary.fire(f"too hot: {ctx.value()}")
                    return kanary.error_if(ctx.value() < 0, message="below zero")
        """), [])


class MatchedOutputsTest(unittest.TestCase):
    def test_follow_an_output_reload(self):
        engine = start_engine(self, [range_rule()], outputs={})
        self.assertEqual(engine.rules["hv1"].matched_outputs, [])
        engine.reload_output_plugins(replacements={"chat": Quiet})
        self.assertEqual(engine.rules["hv1"].matched_outputs, ["chat"])
        engine.reload_output_plugins(replacements={}, removed_output_ids={"chat"})
        self.assertEqual(engine.rules["hv1"].matched_outputs, [])


if __name__ == "__main__":
    unittest.main()
