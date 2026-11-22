"""Times without a time zone, and input values that are not plain Python numbers."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from urllib.request import Request, urlopen

import kanary
from kanary.values import json_default, plain_value

T0 = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)


class FakeSource(kanary.Source):
    source_id = "fake"
    interval = 60.0

    def poll(self):
        return kanary.no_update()


class Recorder(kanary.Output):
    output_id = "rec"

    def __init__(self):
        self.events = []

    def emit(self, event):
        self.events.append(event)


def make_rule(base, rule_id="test.rule", **attrs):
    attrs.setdefault("inputs", "fake:*")
    attrs.setdefault("severity", kanary.ERROR)
    attrs.setdefault("tags", ["test"])
    return type("TestRule", (base,), {"rule_id": rule_id, **attrs})


class EngineCase(unittest.TestCase):
    def start_engine(self, *rules, store=None):
        engine = kanary.Engine(
            source_registry={"fake": FakeSource},
            rule_registry={rule.rule_id: rule for rule in rules},
            output_registry={"rec": Recorder},
            store=store,
            now_fn=lambda: T0,
        )
        engine.start()
        self.addCleanup(engine.shutdown)
        return engine

    def feed(self, engine, *items, at=T0):
        return engine.evaluate_source("fake", kanary.inputs(*items), now=at)

    def assert_ready(self, engine, rule_id="test.rule"):
        status = engine.plugin_states[f"rule:{rule_id}"]
        self.assertEqual(status.state, "READY", status.last_error)


class NaiveTimeTest(EngineCase):
    def test_a_time_without_a_zone_is_local_time(self):
        naive = datetime(2026, 11, 18, 21, 0)
        self.assertEqual(kanary.to_datetime(naive), naive.astimezone())
        self.assertEqual(kanary.to_datetime("2026-11-18T21:00:00"), naive.astimezone())
        self.assertEqual(kanary.to_datetime(naive, naive_timezone="UTC"), naive.replace(tzinfo=timezone.utc))
        self.assertEqual(kanary.to_datetime("2026-11-18T12:00:00Z"), T0)

    def test_input_timestamps_are_read_the_same_way(self):
        naive = datetime(2026, 11, 18, 21, 0)
        result = kanary.inputs(("a", 1.0, naive), ("b", 1.0, "2026-11-18T12:00:00+00:00"), ("c", 1.0, T0.timestamp()))
        stamps = [m.timestamp for m in result.measurements]
        self.assertEqual(stamps, [naive.astimezone(), T0, T0])
        self.assertTrue(all(stamp.tzinfo is not None for stamp in stamps))

    def test_a_silence_written_without_a_zone_does_not_stop_evaluation(self):
        # kanaryctl silence-until --start-at 2026-11-18T21:00 ... used to break every rule.
        engine = self.start_engine(make_rule(kanary.RangeRule, high=5.0))
        start = T0.astimezone().replace(tzinfo=None) - timedelta(minutes=1)
        silence = engine.create_silence(
            operator="op", start_at=start, end_at=start + timedelta(hours=1), rule_patterns=["other.*"]
        )
        self.assertEqual(silence.start_at, start.astimezone())
        self.feed(engine, ("t", 9.0))
        self.assert_ready(engine)
        self.assertEqual(engine.alerts["test.rule"].state, kanary.FIRING)

    def test_an_old_database_with_naive_silence_times_still_starts(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.db"
            store = kanary.SQLiteStore(path)
            store.initialize()
            store.close()
            start = T0.astimezone().replace(tzinfo=None) - timedelta(minutes=1)
            conn = sqlite3.connect(path)
            conn.execute(
                "INSERT INTO silences (silence_id, created_by, created_at, start_at, end_at, rule_patterns_json)"
                " VALUES ('s1', 'op', ?, ?, ?, '[\"test.*\"]')",
                (start.isoformat(), start.isoformat(), (start + timedelta(hours=1)).isoformat()),
            )
            conn.commit()
            conn.close()
            engine = self.start_engine(make_rule(kanary.RangeRule, high=5.0), store=kanary.SQLiteStore(path))
            self.assertEqual(engine.silences["s1"].start_at, start.astimezone())
            self.feed(engine, ("t", 9.0))
            self.assert_ready(engine)
            self.assertEqual(engine.alerts["test.rule"].state, kanary.SILENCED)


class StaleFutureTest(EngineCase):
    def stale(self, **attrs):
        return make_rule(kanary.StaleRule, timeout=600.0, **attrs)

    def test_a_timestamp_far_ahead_fires_a_warning(self):
        engine = self.start_engine(self.stale())
        alert = self.feed(engine, ("t", 1.0, T0 + timedelta(hours=9)))["test.rule"]
        self.assertEqual((alert.state, alert.severity), (kanary.FIRING, kanary.WARN))
        self.assertIn("9 h in the future (time zone?)", alert.message)

    def test_a_little_ahead_is_fine_and_the_check_can_be_turned_off(self):
        engine = self.start_engine(self.stale())
        alert = self.feed(engine, ("t", 1.0, T0 + timedelta(minutes=10)))["test.rule"]
        self.assertEqual(alert.state, kanary.OK)
        self.assertIn("10 min in the future", alert.message)
        self.assertNotIn("time zone", alert.message)
        engine = self.start_engine(self.stale(future_tolerance=None))
        self.assertEqual(self.feed(engine, ("t", 1.0, T0 + timedelta(hours=9)))["test.rule"].state, kanary.OK)

    def test_stale_keeps_the_rule_severity(self):
        engine = self.start_engine(self.stale())
        alert = self.feed(engine, ("t", 1.0, T0 - timedelta(hours=1)))["test.rule"]
        self.assertEqual((alert.state, alert.severity), (kanary.FIRING, kanary.ERROR))


class OddValueTest(EngineCase):
    def test_database_and_numpy_numbers_become_python_numbers(self):
        self.assertEqual((plain_value(Decimal("99.5")), type(plain_value(Decimal("99.5")))), (99.5, float))
        self.assertEqual(plain_value("RUN"), "RUN")
        self.assertIsNone(plain_value(None))
        text = json.dumps({"d": Decimal("2.5"), "t": T0, "s": {1}, "o": object()}, default=json_default)
        self.assertEqual(json.loads(text)["d"], 2.5)

    def test_a_decimal_input_is_judged_and_stored(self):
        # psycopg returns NUMERIC as Decimal: the rule used to see "no value", and the
        # state database could not store the alert, so its notification was lost.
        with tempfile.TemporaryDirectory() as tmp:
            engine = self.start_engine(
                make_rule(kanary.RangeRule, high=25.0), store=kanary.SQLiteStore(Path(tmp) / "state.db")
            )
            alert = self.feed(engine, ("t", Decimal("99.5")))["test.rule"]
            self.assert_ready(engine)
            self.assertEqual(alert.state, kanary.FIRING)
            self.assertIn("99.5", alert.message)
            self.assertTrue(engine.flush_outputs(5))
            self.assertEqual(len(engine.outputs["rec"].events), 1)
            self.assertEqual(engine.get_rule_history("test.rule")["alert_events"][0]["current_state"], "FIRING")

    def test_an_odd_value_a_rule_adds_does_not_stop_the_store(self):
        class Odd(kanary.Rule):
            rule_id = "test.rule"
            inputs = "fake:*"
            tags = ["test"]

            def evaluate(self, ctx):
                return kanary.warn("odd", extra={"amount": Decimal("1.25"), "thing": object()})

        with tempfile.TemporaryDirectory() as tmp:
            engine = self.start_engine(Odd, store=kanary.SQLiteStore(Path(tmp) / "state.db"))
            self.feed(engine, ("t", 1.0))
            self.assert_ready(engine)
            events = engine.get_rule_history("test.rule")["alert_events"]
            self.assertEqual(events[0]["payload"]["amount"], 1.25)


class APITimeTest(EngineCase):
    def start_api(self, engine):
        api = kanary.ControlAPI(engine_getter=lambda: engine, reload_callback=lambda request: True, host="127.0.0.1", port=0)
        thread = threading.Thread(target=api.start, daemon=True)
        thread.start()

        def stop():
            api.shutdown()
            thread.join(timeout=2.0)

        self.addCleanup(stop)
        return f"http://127.0.0.1:{api._server.server_address[1]}"

    def request(self, url, body=None):
        data = None if body is None else json.dumps(body).encode()
        request = Request(url, method="GET" if body is None else "POST", data=data)
        request.add_header("Content-Type", "application/json")
        with urlopen(request) as response:
            return json.loads(response.read().decode())

    def test_health_tells_the_time_zone_and_a_silence_window_is_echoed(self):
        engine = self.start_engine(make_rule(kanary.RangeRule, high=5.0))
        base = self.start_api(engine)
        health = self.request(f"{base}/health")
        local = datetime.now().astimezone()
        self.assertEqual(health["timezone"], local.tzname())
        self.assertRegex(health["utc_offset"], r"^[+-]\d\d:\d\d$")
        self.assertIsNotNone(datetime.fromisoformat(health["time"]).tzinfo)
        created = self.request(
            f"{base}/silences/window",
            {"operator": "op", "start_at": "2026-11-18T21:00", "end_at": "2026-11-18T22:00", "rule_patterns": ["test.*"]},
        )
        expected = datetime(2026, 11, 18, 21, 0).astimezone()
        self.assertEqual(datetime.fromisoformat(created["start_at"]), expected)
        self.assertEqual(engine.silences[created["silence_id"]].start_at, expected)


if __name__ == "__main__":
    unittest.main()
