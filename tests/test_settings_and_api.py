"""Settings and requests written loosely (one name as a string, any case), cron schedules,
request bodies that are not JSON objects, and the order of a rule's history."""

import contextlib
from datetime import datetime, timedelta, timezone
import http.client
import io
import json
import logging
from pathlib import Path
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from unittest import mock
from urllib.request import Request, urlopen

import kanary
from kanary import ctl as kanaryctl
from kanary.runtime import EngineRuntime, RuntimeConfig
from kanary.schedule import parse_schedule
from kanary.store import build_store
from kanary.validation import validate_registries

T0 = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)
JST = timezone(timedelta(hours=9))


def source(source_id="daq"):
    return type(source_id, (kanary.Source,), {"source_id": source_id, "interval": 60.0, "poll": lambda self: kanary.no_update()})


def range_rule(rule_id, inputs="daq:hv", **attrs):
    return type("R", (kanary.RangeRule,), {"rule_id": rule_id, "inputs": inputs, "high": 1, "tags": ["t"], **attrs})


def evaluating(rule_id, result):
    return type("E", (kanary.Rule,), {"rule_id": rule_id, "inputs": "daq:hv", "tags": ["t"], "evaluate": lambda self, ctx: result})


class Recorder(kanary.Output):
    def init(self):
        self.sent = []

    def emit(self, event):
        self.sent.append(event.current_state.value)


def start(case, rules, outputs=None, store=None):
    engine = kanary.Engine(
        source_registry={"daq": source()},
        rule_registry={rule.rule_id: rule for rule in rules},
        output_registry=outputs or {},
        store=store,
        now_fn=lambda: T0,
    )
    engine.start()
    case.addCleanup(engine.shutdown)
    return engine


class SilenceTargetTest(unittest.TestCase):
    def test_one_pattern_or_tag_may_be_a_string(self):
        engine = start(self, [range_rule("sqlite.a"), range_rule("other.b", tags=["db"])])
        silence = engine.create_silence(operator="op", start_at=T0, end_at=T0 + timedelta(hours=1), rule_patterns="sqlite.*")
        self.assertEqual(silence.rule_patterns, ("sqlite.*",))
        self.assertTrue(engine._silence_matches_rule(silence, engine.rules["sqlite.a"], T0))
        self.assertFalse(engine._silence_matches_rule(silence, engine.rules["other.b"], T0))
        by_tag = engine.create_silence(operator="op", start_at=T0, end_at=T0 + timedelta(hours=1), tags="db")
        self.assertEqual(by_tag.tags, ("db",))
        self.assertEqual(engine.silence_target_warnings(rule_patterns="sqlite.*"), [])

    def test_targets_that_are_not_names_are_refused(self):
        engine = start(self, [range_rule("sqlite.a")])
        with self.assertRaisesRegex(ValueError, "rule_patterns must be a name or a list of names"):
            engine.create_silence(operator="op", start_at=T0, end_at=T0 + timedelta(hours=1), rule_patterns=[1])
        with self.assertRaisesRegex(ValueError, "at least one rule pattern or tag"):
            engine.create_silence(operator="op", start_at=T0, end_at=T0 + timedelta(hours=1), rule_patterns="  ")


class EvaluationTest(unittest.TestCase):
    def test_state_and_severity_may_be_text_in_any_case(self):
        engine = start(self, [
            evaluating("upper", kanary.Evaluation(state="FIRING")),
            evaluating("lower", kanary.Evaluation(state="firing", severity="warn")),
        ])
        engine.evaluate_source("daq", {"hv": 1}, now=T0)
        self.assertEqual(engine.alerts["upper"].state, kanary.FIRING)
        self.assertIs(type(engine.alerts["upper"].state), kanary.AlertState)
        self.assertEqual((engine.alerts["lower"].state, engine.alerts["lower"].severity), (kanary.FIRING, kanary.WARN))

    def test_an_unknown_state_is_the_rule_failing(self):
        with self.assertRaisesRegex(ValueError, "evaluation state must be one of"):
            kanary.Evaluation(state="broken")
        rule = type("B", (kanary.Rule,), {
            "rule_id": "b", "inputs": "daq:hv", "tags": ["t"], "evaluate": lambda self, ctx: kanary.Evaluation(state="broken"),
        })
        engine = start(self, [rule])
        with self.assertLogs("kanary.engine", "ERROR"):
            engine.evaluate_source("daq", {"hv": 1}, now=T0)
        self.assertNotIn("b", engine.alerts)
        self.assertEqual(engine._plugin_status("rule", "b").state, "FAILED")

    def test_an_evaluation_returned_every_time_gets_each_new_payload(self):
        alarm = kanary.firing("pump stopped")
        engine = start(self, [evaluating("pump", alarm)])
        engine.evaluate_source("daq", {"hv": 1}, now=T0)
        engine.evaluate_source("daq", {"hv": 2}, now=T0 + timedelta(minutes=1))
        self.assertEqual(engine.alerts["pump"].payload["inputs"]["hv"]["value"], 2)
        self.assertIsNone(alarm.payload)  # the rule's own object is left as it was


class CronTest(unittest.TestCase):
    def test_weekday_ranges_may_end_on_7_and_names_are_read(self):
        self.assertEqual(sorted(parse_schedule("0 0 * * 5-7").weekdays), [0, 5, 6])
        self.assertEqual(sorted(parse_schedule("0 9 * * mon-fri").weekdays), [1, 2, 3, 4, 5])
        self.assertEqual(sorted(parse_schedule("0 0 1 JAN,jul *").months), [1, 7])

    def test_day_of_month_or_weekday_when_both_are_given(self):
        schedule = parse_schedule("0 0 1 * 1")  # the 1st, and every Monday
        self.assertTrue(schedule.matches(datetime(2026, 11, 2, 0, 0)))  # a Monday
        self.assertTrue(schedule.matches(datetime(2026, 12, 1, 0, 0)))  # a Tuesday, the 1st
        self.assertFalse(schedule.matches(datetime(2026, 11, 3, 0, 0)))
        only_day = parse_schedule("0 0 1 * *")
        self.assertFalse(only_day.matches(datetime(2026, 11, 2, 0, 0)))
        stepped_weekday = parse_schedule("0 0 1 * */2")  # "*..." counts as any: both must match
        self.assertFalse(stepped_weekday.matches(datetime(2026, 11, 3, 0, 0)))

    def test_a_step_after_a_value_runs_to_the_end_of_the_field(self):
        self.assertEqual(sorted(parse_schedule("5/15 * * * *").minutes), [5, 20, 35, 50])
        self.assertEqual(sorted(parse_schedule("5 * * * *").minutes), [5])

    def test_a_schedule_that_never_runs_is_read_as_not_polled(self):
        # "0 0 31 2 *" is cron's way to turn a job off.
        self.assertTrue(parse_schedule("0 0 31 2 *").never)
        self.assertFalse(parse_schedule("0 0 29 2 *").never)
        with self.assertRaisesRegex(ValueError, "never runs"):
            parse_schedule("0 0 30 2 *").next_after(datetime(2026, 11, 22))
        report = validate_registries(
            sources={"off": kanary.source(source_id="off", schedule="0 0 31 2 *")(type("S", (), {"poll": lambda self: {}}))},
            rules={}, outputs={},
        )
        self.assertTrue(any("never runs (no such day): it is not polled" in warning for warning in report.warnings))

    def test_both_day_fields_are_a_lint_warning(self):
        report = validate_registries(
            sources={"both": kanary.source(source_id="both", schedule="0 0 1 * MON")(type("S", (), {"poll": lambda self: {}}))},
            rules={}, outputs={},
        )
        self.assertTrue(any("a day matches when either does" in warning and "MON#1" in warning for warning in report.warnings))

    def test_forms_of_other_cron_implementations(self):
        def runs(expression, start=datetime(2026, 11, 22, 12, 0), count=3):
            schedule, found = parse_schedule(expression), []
            for _ in range(count):
                start = schedule.next_after(start)
                found.append(start)
            return found

        self.assertEqual(runs("0 9 * * MON#1"), [datetime(2026, 12, 7, 9), datetime(2027, 1, 4, 9), datetime(2027, 2, 1, 9)])
        self.assertEqual(runs("0 0 L * *"), [datetime(2026, 11, 30), datetime(2026, 12, 31), datetime(2027, 1, 31)])
        self.assertEqual(runs("0 0 L-2 * *", count=1), [datetime(2026, 11, 28)])
        self.assertEqual(runs("0 18 * * 5L"), runs("0 18 * * FRIL"))
        self.assertEqual(runs("0 18 * * 5L", count=2), [datetime(2026, 11, 27, 18), datetime(2026, 12, 25, 18)])
        self.assertEqual(runs("0 0 ? * mon"), runs("0 0 * * 1"))
        self.assertEqual(runs("0 9 * * monday,Tues"), runs("0 9 * * 1,2"))
        self.assertEqual(sorted(parse_schedule("0 0 1 sept,DECEMBER *").months), [9, 12])
        self.assertEqual(sorted(parse_schedule("0 22-2 * * *").hours), [0, 1, 2, 22, 23])
        self.assertEqual(sorted(parse_schedule("0 22-2/2 * * *").hours), [0, 2, 22])
        self.assertEqual(sorted(parse_schedule("0 0 * * FRI-MON").weekdays), [0, 1, 5, 6])
        self.assertEqual(sorted(parse_schedule("0 0 * * SAT-SUN").weekdays), [0, 6])
        self.assertEqual(sorted(parse_schedule("0 0 * NOV-FEB *").months), [1, 2, 11, 12])

    def test_a_time_zone_in_front(self):
        schedule = parse_schedule("CRON_TZ=Asia/Tokyo 0 9 * * *")
        after = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)  # 21:00 in Tokyo
        self.assertEqual(schedule.next_after(after), datetime(2026, 11, 23, 0, 0, tzinfo=timezone.utc))
        self.assertEqual(parse_schedule("TZ=UTC @daily").next_after(after), datetime(2026, 11, 23, tzinfo=timezone.utc))
        self.assertEqual(parse_schedule("tz=+09:00 0 9 * * *").next_after(after), schedule.next_after(after))
        with self.assertRaisesRegex(ValueError, "schedule time zone"):
            parse_schedule("CRON_TZ=Mars/Base 0 9 * * *")

    def test_every_is_an_interval(self):
        every = kanary.source(source_id="every", schedule="@every 1h30m")(type("S", (), {"poll": lambda self: {}}))
        self.assertEqual((every.interval, every.schedule), (5400.0, None))
        from kanary.schedule import every_seconds

        self.assertEqual([every_seconds(text) for text in ("@every 5m", "@every 90s", "@EVERY 1.5h", "@every 2 days", "@every 300")],
                         [300.0, 90.0, 5400.0, 172800.0, 300.0])
        with self.assertRaisesRegex(ValueError, "write the interval as"):
            kanary.source(source_id="bad", schedule="@every soon")(type("S", (), {"poll": lambda self: {}}))

    def test_what_stays_an_error(self):
        for expression, reason in (
            ("0 0 * * L", "ambiguous"),
            ("0 0 * * MON#6", "MON#1 to MON#5"),
            ("0 0 L-31 * *", "at most 30"),
            ("60 * * * *", "outside 0-59"),
            ("0 0 0 * * *", "5 cron fields"),
            ("0 0 * * mo", "invalid cron value"),
        ):
            with self.subTest(expression), self.assertRaisesRegex(ValueError, reason):
                parse_schedule(expression)

    def test_the_next_run_of_a_rare_schedule_is_found_at_once(self):
        started = time.monotonic()
        leap_day = parse_schedule("0 0 29 2 *").next_after(datetime(2026, 11, 22, 12, 0))
        self.assertEqual(leap_day, datetime(2028, 2, 29, 0, 0))
        self.assertLess(time.monotonic() - started, 0.2)
        self.assertEqual(  # read in its own zone, whatever the host's is
            parse_schedule("CRON_TZ=Asia/Tokyo 30 9 * * mon").next_after(datetime(2026, 11, 22, 12, 0, tzinfo=JST)),
            datetime(2026, 11, 23, 9, 30, tzinfo=JST),
        )


class ApiRequestTest(unittest.TestCase):
    def setUp(self):
        self.engine = start(self, [range_rule("hv")], outputs={"chat": type("Chat", (Recorder,), {"output_id": "chat"})})
        api = kanary.ControlAPI(engine_getter=lambda: self.engine, reload_callback=lambda body: {"status": "reloaded"},
                                host="127.0.0.1", port=0)
        thread = threading.Thread(target=api.start, daemon=True)
        thread.start()
        self.addCleanup(lambda: (api.shutdown(), thread.join(timeout=2.0)))
        self.port = api._server.server_address[1]

    def post(self, path, body: bytes):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        self.addCleanup(conn.close)
        conn.request("POST", path, body=body, headers={"Content-Type": "application/json"})
        response = conn.getresponse()
        return response.status, json.loads(response.read())

    def test_a_body_that_is_not_a_json_object_is_400(self):
        for path, body in (("/silences/duration", b"{not json"), ("/alerts/hv/ack", b'"alice"'), ("/reload", b"[1]")):
            status, answer = self.post(path, body)
            self.assertEqual(status, 400, path)
            self.assertIn("error", answer)

    def test_a_silence_for_one_pattern_as_a_string(self):
        status, answer = self.post("/silences/duration", json.dumps(
            {"operator": "op", "duration_minutes": 10, "rule_patterns": "hv"}).encode())
        self.assertEqual((status, answer["warnings"]), (200, ["silence target matches all loaded rules"]))
        self.assertEqual(self.engine.silences[answer["silence_id"]].rule_patterns, ("hv",))

    def test_a_test_fire_state_in_any_case(self):
        status, answer = self.post("/test-fire/hv", json.dumps({"state": "firing"}).encode())
        self.assertEqual((status, answer["current_state"]), (200, "FIRING"))


class RuleHistoryOrderTest(unittest.TestCase):
    def test_newest_first_across_time_zones(self):
        store = build_store(Path(tempfile.mkdtemp()) / "state.db")
        engine = start(self, [range_rule("hv")], store=store)
        fired = (T0 - timedelta(minutes=5)).astimezone(JST)  # the runtime stamps polls in local time
        engine.evaluate_source("daq", {"hv": 5}, now=fired)
        engine.acknowledge("hv", operator="alice")  # the engine's clock: UTC, 5 minutes later
        events = engine.get_rule_history("hv")["alert_events"]
        self.assertEqual([event["current_state"] for event in events], ["ACKED", "FIRING"])


class RuleHistoryLimitTest(unittest.TestCase):
    def test_the_newest_entries_of_each_section(self):
        store = build_store(Path(tempfile.mkdtemp()) / "state.db")
        engine = start(self, [range_rule("hv")], store=store)
        for minute, value in enumerate((5, 0, 5, 0), start=1):
            engine.evaluate_source("daq", {"hv": value}, now=T0 + timedelta(minutes=minute))
        history = engine.get_rule_history("hv", limit=2)
        self.assertEqual([event["current_state"] for event in history["alert_events"]], ["OK", "FIRING"])
        self.assertEqual(len(engine.get_rule_history("hv")["alert_events"]), 4)
        api = kanary.ControlAPI(engine_getter=lambda: engine, reload_callback=lambda body: {}, host="127.0.0.1", port=0)
        thread = threading.Thread(target=api.start, daemon=True)
        thread.start()
        self.addCleanup(lambda: (api.shutdown(), thread.join(timeout=2.0)))
        with urlopen(f"http://127.0.0.1:{api._server.server_address[1]}/history/hv?limit=1") as response:
            self.assertEqual(len(json.loads(response.read())["alert_events"]), 1)


class OutputSettingsTest(unittest.TestCase):
    def output(self, **attrs):
        return kanary.output(output_id="o", **attrs)(type("O", (Recorder,), {}))

    def test_one_tag_may_be_a_string(self):
        engine = start(self, [range_rule("hv")], outputs={"o": self.output(include_tags="t")})
        engine.evaluate_source("daq", {"hv": 5}, now=T0)
        engine.flush_outputs(5)
        self.assertEqual(engine.outputs["o"].sent, ["FIRING"])
        self.assertEqual(engine.outputs["o"].include_tags, ["t"])

    def test_state_names_in_any_case(self):
        output = self.output(exclude_states=["ok", "Silenced", "SUPPRESSED"], exclude_transitions="escalated")
        self.assertEqual(output.exclude_states, ["OK", "SILENCED", "SUPPRESSED"])
        self.assertEqual(output.exclude_transitions, ["ESCALATED"])
        engine = start(self, [range_rule("hv")], outputs={"o": output})
        engine.evaluate_source("daq", {"hv": 5}, now=T0)
        engine.evaluate_source("daq", {"hv": 0}, now=T0 + timedelta(minutes=1))
        engine.flush_outputs(5)
        self.assertEqual(engine.outputs["o"].sent, ["FIRING"])

    def test_an_unknown_state_name_is_refused(self):
        with self.assertRaisesRegex(ValueError, "exclude_states has unknown names \\['RESOLVED'\\]"):
            self.output(exclude_states=["RESOLVED"])

    def test_a_filter_set_in_init_is_read_the_same_way(self):
        class Late(Recorder):
            def init(self):
                super().init()
                self.exclude_states = "ok"

        engine = start(self, [range_rule("hv")], outputs={"o": kanary.output(output_id="o")(Late)})
        engine.evaluate_source("daq", {"hv": 5}, now=T0)
        engine.evaluate_source("daq", {"hv": 0}, now=T0 + timedelta(minutes=1))
        engine.flush_outputs(5)
        self.assertEqual(engine.outputs["o"].sent, ["FIRING"])


class RemoteJudgmentTest(unittest.TestCase):
    def test_an_alert_acknowledged_at_the_peer_keeps_its_active_since_and_children(self):
        from kanary.helpers.remote import RemoteAlarm

        alarm = type("A", (RemoteAlarm,), {"rule_id": "remote.link", "source": "peer", "remote_alarm_id": "link", "tags": ["t"]})
        child = range_rule("hv", suppressed_by="remote.link")
        engine = kanary.Engine(
            source_registry={"peer": source("peer"), "daq": source()},
            rule_registry={alarm.rule_id: alarm, child.rule_id: child},
            output_registry={},
        )
        engine.start()
        self.addCleanup(engine.shutdown)

        def remote(state, at):
            metadata = {"state": state, "severity": "ERROR", "message": "link down"}
            engine.evaluate_source("peer", kanary.inputs(("link", 1, at, metadata)), now=at)

        remote("FIRING", T0)
        engine.evaluate_source("daq", {"hv": 5}, now=T0 + timedelta(minutes=1))
        remote("ACKED", T0 + timedelta(minutes=2))
        self.assertEqual(engine.alerts["remote.link"].active_since, T0)
        engine.evaluate_source("daq", {"hv": 5}, now=T0 + timedelta(minutes=3))
        self.assertEqual(engine.alerts["hv"].state, kanary.SUPPRESSED)  # still held: the link is still down


class RuntimeWatchTest(unittest.TestCase):
    def test_a_plugin_file_that_does_not_load_is_reported_once_per_change(self):
        root = Path(tempfile.mkdtemp())
        (root / "ok.py").write_text("import kanary\n")
        runtime = EngineRuntime(RuntimeConfig(rule_directories=[root], api_port=0, reload_interval=3600))
        runtime.start()
        self.addCleanup(runtime.stop)
        (root / "broken.py").write_text("def (:\n")
        with self.assertLogs("kanary.runtime", "ERROR") as logs:
            for _ in range(5):
                runtime.reload_now_if_changed()
            logging.getLogger("kanary.runtime").error("end")
        self.assertEqual(sum("reload failed while loading" in line for line in logs.output), 1)
        self.assertIn("broken.py", runtime.meta_payload()["plugin_load_error"])
        (root / "broken.py").write_text("import kanary\n")
        self.assertTrue(runtime.reload_now_if_changed())
        self.assertIsNone(runtime.meta_payload()["plugin_load_error"])

    def test_a_source_thread_that_cannot_go_on_shows_the_source_failed(self):
        root = Path(tempfile.mkdtemp())
        (root / "p.py").write_text(textwrap.dedent('''
            import kanary

            @kanary.source(source_id="cron", schedule="* * * * *")
            class Cron:
                def poll(self):
                    return {"v": 1}
        '''))
        with mock.patch("kanary.runtime._initial_schedule_run_at", side_effect=RuntimeError("clock broke")), \
                self.assertLogs("kanary.runtime", "ERROR"):
            runtime = EngineRuntime(RuntimeConfig(rule_directories=[root], api_port=0))
            runtime.start()
            self.addCleanup(runtime.stop)
            status = runtime.engine._plugin_status("source", "cron")
            deadline = time.monotonic() + 5
            while status.state != "FAILED" and time.monotonic() < deadline:
                time.sleep(0.02)
        self.assertEqual(status.state, "FAILED")
        self.assertIn("stopped polling: RuntimeError: clock broke", status.last_error)


class NeverScheduledSourceTest(unittest.TestCase):
    def test_a_source_whose_schedule_never_runs_is_not_polled_but_answers_a_test_poll(self):
        root = Path(tempfile.mkdtemp())
        (root / "p.py").write_text(textwrap.dedent('''
            import kanary
            POLLS = []

            @kanary.source(source_id="off", schedule="0 0 31 2 *")
            class Off:
                def poll(self):
                    POLLS.append(1)
                    return {"v": 1}
        '''))
        runtime = EngineRuntime(RuntimeConfig(rule_directories=[root], api_port=0))
        runtime.start()
        self.addCleanup(runtime.stop)
        status = runtime.engine._plugin_status("source", "off")
        deadline = time.monotonic() + 5
        while not status.warning and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertIn("never runs", status.warning)
        self.assertEqual(runtime.test_poll("off", timeout=5)["inputs"], {"off:v": {"value": 1, "timestamp": mock.ANY, "metadata": {}}})
        time.sleep(0.2)
        self.assertEqual(status.run_count, 0)  # the test poll is not a poll
        self.assertEqual(status.state, "READY")


class CtlQuotingTest(unittest.TestCase):
    def test_ids_are_quoted_in_request_paths(self):
        requested = []

        def fake_fetch(url, method="GET", body=None):
            requested.append(url)
            return {"alert_events": [], "output_dispatches": [], "operator_actions": [], "status": "acked"}

        for argv in (["history", "a/b#c"], ["ack", "a/b#c", "--operator", "op"]):
            with mock.patch.object(kanaryctl, "fetch_json", fake_fetch), \
                    mock.patch.object(sys, "argv", ["kanaryctl", "--base-url", "http://kanary", *argv]), \
                    contextlib.redirect_stdout(io.StringIO()):
                kanaryctl.main()
        self.assertEqual(requested, ["http://kanary/history/a%2Fb%23c", "http://kanary/alerts/a%2Fb%23c/ack"])


if __name__ == "__main__":
    unittest.main()
