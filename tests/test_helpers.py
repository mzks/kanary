"""Tests for the plugin helper classes (sources, rules, outputs) and their templates."""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import socket
import sqlite3
import sys
from tempfile import TemporaryDirectory
import threading
import time
import types
import urllib.error
import urllib.request
import unittest
from unittest import mock
from contextlib import redirect_stdout

import kanary
from kanary import __main__ as kanary_main
from kanary.helpers.buffer import discarding_changes
from kanary.validation import validate_registries

T0 = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)


class FakeSource(kanary.Source):
    source_id = "fake"
    interval = 60.0

    def poll(self):
        return kanary.no_update()


def make_rule(base, inputs="fake:*", **attrs):
    attrs.setdefault("rule_id", "test.rule")
    attrs.setdefault("severity", kanary.WARN)
    attrs.setdefault("tags", ["test"])
    return type("TestRule", (base,), {"inputs": inputs, **attrs})


class RuleDriver:
    """Feed snapshots to one rule through a real Engine."""

    def __init__(self, rule_cls):
        self.rule_id = rule_cls.rule_id
        self.engine = kanary.Engine(
            source_registry={"fake": FakeSource},
            rule_registry={self.rule_id: rule_cls},
            output_registry={"null": NullOutput},
            now_fn=lambda: T0,
        )
        self.engine.start()

    def feed(self, values, *, at=T0):
        items = [(name, value, at) for name, value in values.items()]
        return self.engine.evaluate_source("fake", kanary.inputs(items), now=at).get(self.rule_id)

    def close(self):
        self.engine.shutdown()


class NullOutput(kanary.Output):
    output_id = "null"

    def emit(self, event):
        return None


class HelperTestCase(unittest.TestCase):
    def drive(self, rule_cls):
        driver = RuleDriver(rule_cls)
        self.addCleanup(driver.close)
        return driver

    def assert_rule_fails(self, rule_cls, message, values=None):
        """An invalid rule setting raises in evaluate(): the rule is FAILED with the message."""
        driver = self.drive(rule_cls)
        driver.feed(values or {"t": 1.0})
        status = driver.engine.plugin_states[f"rule:{rule_cls.rule_id}"]
        self.assertEqual(status.state, "FAILED")
        self.assertIn(message, status.last_error or "")


class RangeFireWhenTest(HelperTestCase):
    def test_inside_band_fires_with_inclusive_edges_and_hysteresis(self):
        driver = self.drive(make_rule(kanary.RangeRule, "fake:hz", low=48.0, high=52.0, fire_when="inside", hysteresis=0.5))
        self.assertEqual(driver.feed({"hz": 47.0}).state, kanary.OK)
        alert = driver.feed({"hz": 48.0})
        self.assertEqual(alert.state, kanary.FIRING)
        self.assertIn("inside forbidden range [48.0, 52.0]", alert.message)
        self.assertEqual(driver.feed({"hz": 47.7}).state, kanary.FIRING)  # within hysteresis
        self.assertEqual(driver.feed({"hz": 47.4}).state, kanary.OK)
        self.assertIn("outside forbidden range", driver.feed({"hz": 60.0}).message)

    def test_inside_band_exclusive_edges_and_glob_inputs(self):
        driver = self.drive(make_rule(kanary.RangeRule, "fake:*", low=48.0, high=52.0, fire_when="inside", lower_inclusive=False))
        self.assertEqual(driver.feed({"a": 48.0, "b": 30.0}).state, kanary.OK)
        alert = driver.feed({"a": 50.0, "b": 30.0})
        self.assertEqual(alert.state, kanary.FIRING)
        self.assertIn("fake:a=50 inside forbidden range (48.0, 52.0]", alert.message)

    def test_outside_is_unchanged_default(self):
        driver = self.drive(make_rule(kanary.RangeRule, "fake:t", low=10.0, high=30.0))
        self.assertEqual(driver.feed({"t": 31.0}).state, kanary.FIRING)
        self.assertEqual(driver.feed({"t": 20.0}).state, kanary.OK)

    def test_bad_fire_when_raises_in_evaluate(self):
        self.assert_rule_fails(make_rule(kanary.RangeRule, "fake:t", high=10.0, fire_when="between"), "fire_when must be")
        self.assert_rule_fails(make_rule(kanary.RateRule, "fake:t", high=10.0, fire_when="between"), "fire_when must be")


class HysteresisTest(HelperTestCase):
    """While alerting, the boundary moves back by hysteresis; the previous value does not matter."""

    def states(self, driver, name, values):
        return [driver.feed({name: float(value)}).state.value for value in values]

    def test_range_holds_until_clear_boundary(self):
        for inputs in ("fake:t", "fake:*"):
            with self.subTest(inputs=inputs):
                rule = make_rule(kanary.RangeRule, inputs, rule_id=f"h.{inputs}", high=100.0, hysteresis=5.0)
                self.assertEqual(
                    self.states(self.drive(rule), "t", [101, 98, 97, 96, 95, 101]),
                    ["FIRING", "FIRING", "FIRING", "FIRING", "OK", "FIRING"],
                )

    def test_threshold_holds_severity_but_does_not_escalate_early(self):
        rule = make_rule(
            kanary.ThresholdRule,
            "fake:t",
            thresholds=[(80.0, kanary.WARN), (90.0, kanary.CRITICAL)],
            hysteresis=5.0,
        )
        driver = self.drive(rule)
        self.assertEqual(driver.feed({"t": 85.0}).severity, kanary.WARN)
        self.assertEqual(driver.feed({"t": 87.0}).severity, kanary.WARN)  # not CRITICAL at 90 - 5
        self.assertEqual(driver.feed({"t": 91.0}).severity, kanary.CRITICAL)
        self.assertEqual(driver.feed({"t": 87.0}).severity, kanary.CRITICAL)  # held
        self.assertEqual(driver.feed({"t": 84.0}).severity, kanary.WARN)
        self.assertEqual(driver.feed({"t": 76.0}).state, kanary.FIRING)
        self.assertEqual(driver.feed({"t": 74.0}).state, kanary.OK)

    def test_aggregate_uses_the_same_hysteresis(self):
        rule = make_rule(kanary.AggregateRule, "fake:*", aggregate="max", high=100.0, hysteresis=5.0)
        self.assertEqual(self.states(self.drive(rule), "t", [101, 98, 97, 94]), ["FIRING", "FIRING", "FIRING", "OK"])

    def test_rate_keeps_its_judgment_when_the_same_sample_is_polled_again(self):
        driver = self.drive(make_rule(kanary.RateRule, "fake:t", high=1.0))
        later = T0 + timedelta(seconds=60)
        driver.feed({"t": 0.0})
        self.assertEqual(driver.feed({"t": 100.0}, at=later).state, kanary.FIRING)
        repeated = driver.feed({"t": 100.0}, at=later)  # same timestamp: nothing new
        self.assertEqual(repeated.state, kanary.FIRING)
        self.assertAlmostEqual(repeated.payload["values"]["fake:t"], 100.0 / 60)
        self.assertEqual(driver.feed({"t": 100.0}, at=later + timedelta(seconds=60)).state, kanary.OK)

    def test_value_rule_subclass_only_overrides_check(self):
        class Even(kanary.ValueRule):
            def check(self, value, ctx):
                return value % 2 == 0

        driver = self.drive(make_rule(Even, "fake:*"))
        alert = driver.feed({"a": 3.0, "b": 4.0})
        self.assertEqual(alert.state, kanary.FIRING)
        self.assertEqual(alert.message, "fake:b=4 fires")
        self.assertEqual(driver.feed({"a": 3.0, "b": 5.0}).message, "all 2 inputs ok")

    def test_hysteresis_wider_than_the_range_raises(self):
        self.assert_rule_fails(
            make_rule(kanary.RangeRule, "fake:t", low=48.0, high=52.0, hysteresis=3.0), "at most half"
        )


class ExpectedValueTest(HelperTestCase):
    def test_expected_and_equal_modes(self):
        driver = self.drive(make_rule(kanary.ExpectedValueRule, "fake:mode", expected="RUN"))
        self.assertEqual(driver.feed({"mode": "RUN"}).state, kanary.OK)
        self.assertEqual(driver.feed({"mode": "STOP"}).message, "fake:mode='STOP' is not the expected 'RUN'")
        flags = self.drive(make_rule(kanary.ExpectedValueRule, "fake:*", rule_id="flags", expected=1))
        self.assertEqual(flags.feed({"a": 1.0, "b": True}).state, kanary.OK)  # 1 == 1.0 == True
        errors = self.drive(make_rule(kanary.ExpectedValueRule, "fake:code", rule_id="errors", expected=[3, 4], fire_when="equal"))
        self.assertEqual(errors.feed({"code": 0}).state, kanary.OK)
        self.assertEqual(errors.feed({"code": 4}).message, "fake:code=4 is 3 or 4")

    def test_settings_raise(self):
        self.assert_rule_fails(make_rule(kanary.ExpectedValueRule, "fake:t"), "expected must be set")
        self.assert_rule_fails(make_rule(kanary.ExpectedValueRule, "fake:t", expected=1, fire_when="x"), "fire_when must be")

    def test_missing_fires_reports_the_error(self):
        driver = self.drive(make_rule(kanary.RangeRule, "fake:t", high=10.0, missing_fires=True))
        items = [("t", None, T0, {"error": "TimeoutError: timed out"})]
        alert = driver.engine.evaluate_source("fake", kanary.inputs(items), now=T0)["test.rule"]
        self.assertEqual((alert.state, alert.message), (kanary.FIRING, "no value: fake:t (TimeoutError: timed out)"))


class SelectInputsTest(unittest.TestCase):
    def test_exact_names_globs_and_source_parts(self) -> None:
        from kanary.selectors import select_inputs

        names = {"det1.radon": 1, "det2.radon": 2, "det10.radon": 3, "pump.rpm": 4}
        self.assertEqual(select_inputs("radon", names, ["radon:det2.radon"]), ["det2.radon"])
        self.assertEqual(select_inputs("radon", names, ["det2.radon"]), ["det2.radon"])  # any source
        self.assertEqual(select_inputs("radon", names, ["radon:missing"]), [])
        self.assertEqual(select_inputs("other", names, ["radon:det2.radon"]), [])
        self.assertEqual(
            select_inputs("radon", names, ["radon:det?.radon", "rad*:pump.rpm"]),
            ["det1.radon", "det2.radon", "pump.rpm"],
        )
        self.assertEqual(select_inputs("radon", names, ["radon:det[12].radon"]), ["det1.radon", "det2.radon"])
        self.assertEqual(select_inputs("radon", names, ["radon:*", "radon:pump.rpm"]), sorted(names))
        self.assertEqual(select_inputs("radon", names, ["radon:DET1.RADON"]), [])  # case-sensitive


class RemovedSettingsTest(unittest.TestCase):
    def test_lint_warns_about_field_settings(self):
        rule = make_rule(kanary.RangeRule, "fake:t", high=10.0, field="channels.t.value")
        report = validate_registries(sources={"fake": FakeSource}, rules={"test.rule": rule}, outputs={"null": NullOutput})
        self.assertTrue(any("sets field, which built-in rules no longer read" in w for w in report.warnings), report.warnings)


class ContextHelperTest(HelperTestCase):
    def test_age_delta_changed_missing(self):
        seen = {}

        class Probe(kanary.Rule):
            rule_id = "test.rule"
            inputs = ["fake:a", "fake:b", "fake:never"]
            severity = kanary.WARN
            tags = ["test"]

            def evaluate(self, ctx):
                seen.update(
                    age=ctx.age("fake:a"),
                    delta=ctx.delta("fake:a"),
                    changed=ctx.changed("fake:a"),
                    unchanged=ctx.changed("fake:b"),
                    missing=ctx.missing(),
                    explicit=ctx.missing("fake:a"),
                )
                return kanary.ok()

        driver = self.drive(Probe)
        driver.feed({"a": 1.0, "b": "x"}, at=T0)
        self.assertIsNone(seen["delta"])
        self.assertFalse(seen["changed"])
        later = T0 + timedelta(seconds=30)
        driver.engine.evaluate_source(
            "fake", kanary.inputs([("a", 3.5, T0), ("b", "x", later), ("c", None, later)]), now=later
        )
        self.assertEqual(seen["age"], 30.0)
        self.assertEqual(seen["delta"], 2.5)
        self.assertTrue(seen["changed"])
        self.assertFalse(seen["unchanged"])
        self.assertEqual(seen["missing"], ["fake:never"])
        self.assertEqual(seen["explicit"], [])


class InputRulesTest(HelperTestCase):
    def test_difference_rule(self):
        driver = self.drive(make_rule(kanary.DifferenceRule, "fake:t*", max_difference=0.5))
        self.assertEqual(driver.feed({"t1": 20.0}).state, kanary.OK)  # needs two inputs
        self.assertEqual(driver.feed({"t1": 20.0, "t2": 20.4}).state, kanary.OK)
        alert = driver.feed({"t1": 20.0, "t2": 21.0})
        self.assertEqual(alert.state, kanary.FIRING)
        self.assertEqual(alert.payload["highest"], "fake:t2")

    def test_k_of_n_rule(self):
        driver = self.drive(make_rule(kanary.KOfNRule, "fake:ch*", k=2, low=1.0, count_missing=True))
        self.assertEqual(driver.feed({"ch1": 0.0, "ch2": 5.0, "ch3": 5.0}).state, kanary.OK)
        alert = driver.feed({"ch1": 0.0, "ch2": None, "ch3": 5.0})
        self.assertEqual(alert.state, kanary.FIRING)
        self.assertEqual(sorted(alert.payload["bad_inputs"]), ["fake:ch1", "fake:ch2"])

    def test_aggregate_rule_with_hysteresis(self):
        driver = self.drive(make_rule(kanary.AggregateRule, "fake:ch*", aggregate="sum", high=10.0, hysteresis=1.0))
        self.assertEqual(driver.feed({"ch1": 4.0, "ch2": 5.0}).state, kanary.OK)
        self.assertEqual(driver.feed({"ch1": 6.0, "ch2": 5.0}).state, kanary.FIRING)
        self.assertEqual(driver.feed({"ch1": 4.5, "ch2": 5.0}).state, kanary.FIRING)  # 9.5 > 9.0
        alert = driver.feed({"ch1": 3.0, "ch2": 5.0})
        self.assertEqual(alert.state, kanary.OK)
        self.assertEqual(alert.payload["aggregate"], 8.0)

    def test_count_rule(self):
        driver = self.drive(make_rule(kanary.CountRule, "fake:ch*", min_count=3))
        self.assertEqual(driver.feed({"ch1": 1, "ch2": 1, "ch3": 1}).state, kanary.OK)
        alert = driver.feed({"ch1": 1, "ch2": 1, "ch3": None})
        self.assertEqual(alert.state, kanary.FIRING)
        self.assertEqual(alert.payload["missing_inputs"], ["fake:ch3"])

    def test_invalid_settings_raise_in_evaluate(self):
        cases = [
            (make_rule(kanary.KOfNRule, k=0, high=1.0), "KOfNRule.k must"),
            (make_rule(kanary.AggregateRule, aggregate="avg", high=1.0), "AggregateRule.aggregate must"),
            (make_rule(kanary.DifferenceRule), "DifferenceRule.max_difference"),
            (make_rule(kanary.CountRule), "CountRule needs min_count"),
        ]
        for rule_cls, message in cases:
            with self.subTest(message=message):
                self.assert_rule_fails(rule_cls, message)

    def test_lint_does_not_report_helper_settings(self):
        # Settings are checked where they are used, so lint stays quiet about them.
        report = validate_registries(
            sources={"fake": FakeSource}, rules={"test.rule": make_rule(kanary.KOfNRule, k=0, high=1.0)}, outputs={}
        )
        self.assertEqual(report.errors, [])


def filled_buffer(name, values, *, step_seconds=60, **limits):
    buffer = kanary.Buffer(**limits)
    for index, value in enumerate(values):
        buffer.add(kanary.inputs((name, value, T0 + timedelta(seconds=step_seconds * index))))
    return buffer


class SeriesTest(unittest.TestCase):
    def test_samples_and_statistics(self):
        p = filled_buffer("p", [1.0, 2.0, 3.0, 4.0, 10.0])["p"]
        self.assertEqual(len(p), 5)
        self.assertEqual((p.first.value, p.last.value, p.last.timestamp), (1.0, 10.0, T0 + timedelta(minutes=4)))
        self.assertEqual(p.values, [1.0, 2.0, 3.0, 4.0, 10.0])
        self.assertEqual(p.span, 240.0)
        self.assertEqual((p.mean(), p.median(), p.min(), p.max(), p.sum()), (4.0, 3.0, 1.0, 10.0, 20.0))
        self.assertAlmostEqual(p.stddev(), 3.5355, places=3)
        self.assertEqual(p.delta(), 9.0)
        self.assertAlmostEqual(p.rate() * kanary.minute, 2.25)
        self.assertAlmostEqual(p.slope() * kanary.minute, 2.0)
        self.assertGreater(p.zscore(), 1.5)
        self.assertAlmostEqual(p.eta(14.0), 120.0)
        self.assertIsNone(p.eta(0.0))  # moving away
        self.assertEqual(p.tail(2).values, [4.0, 10.0])
        self.assertEqual(p.window(120).values, [3.0, 4.0, 10.0])

    def test_not_enough_data_gives_none(self):
        empty = kanary.Series("missing")
        self.assertEqual((len(empty), empty.first, empty.last, empty.span), (0, None, None, 0.0))
        for statistic in (empty.mean, empty.min, empty.max, empty.median, empty.stddev, empty.delta, empty.rate, empty.slope, empty.zscore, empty.unchanged_seconds):
            self.assertIsNone(statistic())
        one = filled_buffer("p", [5.0])["p"]
        self.assertEqual(one.mean(), 5.0)
        self.assertIsNone(one.rate())
        self.assertIsNone(one.stddev())
        text = filled_buffer("state", ["open", None, 3.0])["state"]
        self.assertEqual(text.mean(), 3.0)  # only numbers count in statistics

    def test_changes_and_unchanged_time(self):
        up = filled_buffer("up", [1, 0, 1, 1, 0, 0.0005])["up"]
        self.assertEqual(up.changes(), 4)
        self.assertEqual(up.changes(tolerance=0.001), 3)
        self.assertEqual(up.window(120).changes(), 2)  # samples at minutes 3, 4, 5
        self.assertEqual(up.unchanged_seconds(tolerance=0.001), 60.0)
        flat = filled_buffer("p", [1.0, 2.0, 10.0, 10.0, 10.0])["p"]
        self.assertEqual(flat.unchanged_seconds(), 120.0)


class BufferTest(unittest.TestCase):
    def test_select_gathers_inputs_by_pattern(self):
        buffer = kanary.Buffer()
        buffer.add(kanary.inputs(("temp_a", 20.0, T0), ("temp_b", 21.0, T0), ("humidity", 40.0, T0)))
        self.assertEqual(buffer.names, ["temp_a", "temp_b", "humidity"])
        self.assertEqual(list(buffer.select("temp_*")), ["temp_a", "temp_b"])
        self.assertEqual(buffer.last_values("temp_*", "humidity"), {"temp_a": 20.0, "temp_b": 21.0, "humidity": 40.0})
        self.assertIn("humidity", buffer)
        self.assertNotIn("pressure", buffer)

    def test_same_reading_replaces_and_older_one_is_ignored(self):
        buffer = filled_buffer("p", [1.0, 2.0])
        buffer.add(kanary.inputs(("p", 2.5, T0 + timedelta(minutes=1))))  # same timestamp: a correction
        buffer.add(kanary.inputs(("p", 9.0, T0)))  # older than the newest: ignored
        self.assertEqual(buffer["p"].values, [1.0, 2.5])

    def test_size_and_age_limits(self):
        self.assertEqual(filled_buffer("p", range(10), size=3)["p"].values, [7, 8, 9])
        self.assertEqual(filled_buffer("p", range(10), max_age=120)["p"].values, [7, 8, 9])
        with self.assertRaisesRegex(ValueError, "size must be a positive integer"):
            kanary.Buffer(size=0)

    def test_copy_is_independent(self):
        buffer = filled_buffer("p", [1.0])
        copied = buffer.copy()
        copied.add(kanary.inputs(("p", 2.0, T0 + timedelta(minutes=1))))
        self.assertEqual((len(buffer["p"]), len(copied["p"])), (1, 2))


class BufferAddTest(unittest.TestCase):
    def test_items_are_written_as_for_inputs(self):
        buffer = kanary.Buffer()
        before = datetime.now(timezone.utc)
        buffer.add(("p", 1.0))
        self.assertGreaterEqual(buffer["p"].last.timestamp, before)  # no time given: now
        buffer.add(("p", 2.0, T0.timestamp() + 3600 * 24 * 365, {"unit": "Pa"}), ("q", 3.0, T0))
        self.assertEqual((buffer["p"].last.value, buffer["p"].last.metadata), (2.0, {"unit": "Pa"}))
        buffer.add({"r": 4.0}, timestamp=T0)
        buffer.add(("s", 5.0, T0.astimezone().replace(tzinfo=None)))  # naive: this host's local time
        buffer.add(("u", 6.0, T0.isoformat()))  # ISO 8601 text
        self.assertEqual(
            (buffer["r"].last.timestamp, buffer["s"].last.timestamp, buffer["u"].last.timestamp), (T0, T0, T0)
        )
        with self.assertRaisesRegex(TypeError, "timestamp must be a datetime"):
            buffer.add(("p", 1.0, "yesterday"))

    def test_unix_time_is_honoured_by_inputs_too(self):
        result = kanary.inputs(("p", 1.0, T0.timestamp()))
        self.assertEqual(result.inputs[0].timestamp, T0)

    def test_a_poll_result_is_added_when_ok(self):
        buffer = kanary.Buffer()
        buffer.add(kanary.inputs(("a", 1.0, T0), ("b", 2.0, T0)))
        for nothing in (kanary.no_update(), kanary.skip(), kanary.no_data(), None):
            buffer.add(nothing)
        self.assertEqual(buffer.names, ["a", "b"])
        with self.assertRaisesRegex(TypeError, "carries its own"):
            buffer.add(kanary.inputs(("a", 1.0)), timestamp=T0)

    def test_a_name_never_added_is_a_key_error(self):
        buffer = kanary.Buffer()
        buffer.add(("pressure", 1.0, T0))
        with self.assertRaisesRegex(KeyError, "no input 'presure'; it has: pressure"):
            buffer["presure"]
        self.assertNotIn("presure", buffer)

    def test_printing_shows_what_is_kept(self):
        buffer = filled_buffer("p", [1.0, 2.5], size=8, max_age=3600)
        self.assertEqual(
            str(buffer),
            "Buffer (at most 8 samples per input, none older than 1 h): 1 inputs\n"
            "  p: 2 samples, 2026-11-18T12:00:00+00:00 .. 2026-11-18T12:01:00+00:00, last 2.5",
        )
        self.assertEqual(repr(buffer), "Buffer(p: 2)")


class DiscardingChangesTest(unittest.TestCase):
    def test_changes_inside_are_seen_there_and_dropped_afterwards(self):
        buffer = filled_buffer("p", [1.0])
        with discarding_changes():
            buffer.add(("p", 2.0, T0 + timedelta(minutes=1)), ("q", 3.0, T0))
            self.assertEqual((buffer["p"].values, buffer.names), ([1.0, 2.0], ["p", "q"]))
            buffer.clear()
            self.assertEqual(len(buffer), 0)
        self.assertEqual((buffer["p"].values, buffer.names), ([1.0], ["p"]))

    def test_other_threads_keep_the_real_samples(self):
        buffer = filled_buffer("p", [1.0])
        with discarding_changes():
            buffer.add(("p", 2.0, T0 + timedelta(minutes=1)))
            elsewhere = threading.Thread(target=buffer.add, args=(("p", 9.0, T0 + timedelta(minutes=5)),))
            elsewhere.start()
            elsewhere.join()
            self.assertEqual(buffer["p"].values, [1.0, 2.0])  # this thread: its own copy
        self.assertEqual(buffer["p"].values, [1.0, 9.0])  # the other thread's sample was kept


class BufferInSourceTest(unittest.TestCase):
    def test_the_source_decides_the_warm_up(self):
        class Warm(kanary.Source):
            source_id = "warm"
            minute = 0

            def init(self):
                self.buffer = kanary.Buffer()

            def poll(self):
                self.minute += 1
                self.buffer.add(("p", float(self.minute), T0 + timedelta(minutes=self.minute)))
                p = self.buffer["p"]
                if p.span < 5 * kanary.minute:
                    return kanary.skip(reason="filling the buffer")
                return kanary.inputs(("p_min_5m", p.window(5 * kanary.minute).min()))

        source = Warm()
        source.init()
        statuses = [source.poll().status for _ in range(5)]
        self.assertEqual(statuses, ["skip"] * 5)  # spans of 0 to 4 minutes
        self.assertEqual(source.poll().inputs[0].value, 1.0)  # 6 samples over 5 minutes

    def test_a_helper_result_can_be_kept(self):
        class Reading(kanary.Source):
            source_id = "reading"
            value = 0.0

            def poll(self):
                self.value += 1.0
                return kanary.inputs(("p", self.value, T0 + timedelta(minutes=self.value)))

        class Averaged(Reading):
            def init(self):
                super().init()
                self.buffer = kanary.Buffer()

            def poll(self):
                self.buffer.add(super().poll())
                return kanary.inputs(("p_mean", self.buffer["p"].mean()))

        source = Averaged()
        source.init()
        means = [source.poll().inputs[0].value for _ in range(3)]
        self.assertEqual(means, [1.0, 1.5, 2.0])


METRICS_PAGE = """\
# HELP node_load1 1m load average.
# TYPE node_load1 gauge
node_load1 1.5
node_filesystem_avail_bytes{device="/dev/sda1",fstype="ext4",mountpoint="/"} 1.2e+10
node_filesystem_avail_bytes{device="/dev/sdb1",fstype="ext4",mountpoint="/data"} 3e+11
node_hwmon_temp_celsius{chip="platform_coretemp_0",sensor="temp1"} 45
node_hwmon_temp_celsius{chip="platform_coretemp_0",sensor="temp2"} 47.5
http_requests_total{code="500",path="/a \\"b\\""} 12 1763467200000
quantile_metric{quantile="0.99"} NaN
up_ratio +Inf
"""


class PrometheusTest(unittest.TestCase):
    def serve(self, page):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = page.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; version=0.0.4")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_address[1]}/metrics"

    def test_the_text_format_is_read(self):
        from kanary.helpers.prometheus import parse_metrics
        samples = parse_metrics(METRICS_PAGE)
        self.assertEqual(len(samples), 8)
        requests = samples[5]
        self.assertEqual((requests.labels, requests.value), ({"code": "500", "path": '/a "b"'}, 12.0))
        self.assertEqual(requests.timestamp, datetime(2025, 11, 18, 12, 0, tzinfo=timezone.utc))
        self.assertIsNone(samples[6].value)  # NaN
        self.assertEqual(samples[7].value, float("inf"))
        with self.assertRaisesRegex(ValueError, "line 1 is not a metric sample"):
            parse_metrics("this is not a metric\n")

    def test_selected_series_become_inputs(self):
        url = self.serve(METRICS_PAGE)
        source = type("Metrics", (kanary.PrometheusSource,), {"source_id": "m", "url": url, "metrics": {
            "load1": "node_load1",
            "root_free": 'node_filesystem_avail_bytes{mountpoint="/"}',
            "temp.{sensor}": 'node_hwmon_temp_celsius{chip="platform_coretemp_0"}',
            "errors": 'http_requests_total{code="500"}',
            "gone": "node_not_here",
        }})()
        result = source.poll()
        values = {m.name: m.value for m in result.inputs}
        self.assertEqual(values, {"load1": 1.5, "root_free": 1.2e10, "temp.temp1": 45.0, "temp.temp2": 47.5, "errors": 12.0})
        self.assertEqual(result.metadata["missing"], ["gone"])
        errors = next(m for m in result.inputs if m.name == "errors")
        self.assertEqual(errors.timestamp, datetime(2025, 11, 18, 12, 0, tzinfo=timezone.utc))  # the page's own time

    def test_several_matches_need_a_label_in_the_name(self):
        url = self.serve(METRICS_PAGE)
        source = type("Metrics", (kanary.PrometheusSource,), {"source_id": "m", "url": url,
                                                              "metrics": {"free": "node_filesystem_avail_bytes"}})()
        with self.assertRaisesRegex(ValueError, r"matches 2 series; .* e\.g\. 'free\.\{device\}'"):
            source.poll()


class InboxTest(unittest.TestCase):
    def test_wait_returns_every_input_with_its_newest_value(self):
        inbox = kanary.Inbox()
        inbox.put(("t", 1.0, T0), ("h", 40.0, T0))
        inbox.put({"t": 2.0}, timestamp=T0 + timedelta(seconds=1))
        result = inbox.wait(timeout=0.1)
        self.assertEqual({m.name: m.value for m in result.inputs}, {"t": 2.0, "h": 40.0})
        self.assertEqual(result.metadata, {"arrived": 3, "dropped": 0})
        inbox.put(("h", 41.0))
        later = inbox.wait(timeout=0.1)
        self.assertEqual({m.name: m.value for m in later.inputs}, {"t": 2.0, "h": 41.0})  # t keeps its value

    def test_nothing_arrived_is_skip(self):
        started = time.monotonic()
        result = kanary.Inbox().wait(timeout=0.2)
        self.assertEqual(result.status, "skip")
        self.assertGreaterEqual(time.monotonic() - started, 0.15)

    def test_a_put_from_another_thread_ends_the_wait(self):
        inbox = kanary.Inbox()
        timer = threading.Timer(0.1, inbox.put, args=(("t", 5.0),))
        timer.start()
        started = time.monotonic()
        result = inbox.wait(timeout=5.0)
        timer.join()
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertEqual(result.inputs[0].value, 5.0)
        self.assertGreater(result.inputs[0].timestamp, T0)  # stamped when it arrived

    def test_take_returns_every_sample_and_the_size_is_bounded(self):
        inbox = kanary.Inbox(size=3)
        for value in range(5):
            inbox.put(("t", float(value)))
        self.assertEqual([m.value for m in inbox.take()], [2.0, 3.0, 4.0])
        self.assertEqual((inbox.dropped, len(inbox)), (2, 0))
        self.assertEqual(inbox.latest()["t"].value, 4.0)

    def test_a_test_poll_takes_nothing(self):
        inbox = kanary.Inbox()
        inbox.put(("t", 1.0))
        inbox.wait(timeout=0.1)
        inbox.put(("t", 2.0))
        with discarding_changes():
            started = time.monotonic()
            shown = inbox.wait(timeout=5.0)
            self.assertLess(time.monotonic() - started, 1.0)  # no waiting
        self.assertEqual(shown.inputs[0].value, 2.0)
        self.assertEqual(len(inbox), 1)  # still there for the real poll
        self.assertEqual(inbox.wait(timeout=0.1).inputs[0].value, 2.0)


class HTTPPushSourceTest(unittest.TestCase):
    def post(self, source, body, path="/", token=None):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(f"http://127.0.0.1:{source._server.server_address[1]}{path}",
                                         data=json.dumps(body).encode(), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status
        except urllib.error.HTTPError as exc:
            return exc.code

    def start(self, **settings):
        source = type("Push", (kanary.HTTPPushSource,), {"source_id": "push", "port": 0, **settings})()
        source.init()
        self.addCleanup(source.terminate)
        return source

    def test_posted_values_are_polled(self):
        source = self.start()
        self.assertEqual(kanary.HTTPPushSource.interval, 0)
        self.assertEqual(self.post(source, {"temperature": 21.5, "humidity": {"value": 40, "timestamp": "2026-11-18T21:00:00+09:00"}}), 202)
        result = source.poll()
        values = {m.name: (m.value, m.timestamp) for m in result.inputs}
        self.assertEqual(values["temperature"][0], 21.5)
        self.assertEqual(values["humidity"], (40, T0))

    def test_requests_are_checked(self):
        source = self.start(token="s3cret", path="/inputs")
        self.assertEqual(self.post(source, {"t": 1}, path="/inputs"), 401)
        self.assertEqual(self.post(source, {"t": 1}, path="/other", token="s3cret"), 404)
        self.assertEqual(self.post(source, [1, 2], path="/inputs", token="s3cret"), 400)
        self.assertEqual(self.post(source, {"t": 1}, path="/inputs", token="s3cret"), 202)
        self.assertEqual(source.poll().inputs[0].value, 1)

    def test_a_broken_content_length_is_answered(self):
        source = self.start()
        port = source._server.server_address[1]

        def status_of(head):
            with socket.create_connection(("127.0.0.1", port), timeout=5) as connection:
                connection.sendall(b"POST / HTTP/1.1\r\nHost: x\r\n" + head + b"\r\n{}")
                return connection.recv(100).split(b"\r\n")[0]

        self.assertIn(b" 411 ", status_of(b"Content-Length: abc\r\n"))
        self.assertIn(b" 400 ", status_of(b"Content-Length: -1\r\n"))
        self.assertIn(b" 411 ", status_of(b""))


class WaitingHelperIntervalTest(unittest.TestCase):
    def setUp(self):
        registries = (kanary.get_source_registry(), kanary.get_rule_registry(), kanary.get_output_registry())
        self.addCleanup(lambda: kanary.registry.replace_registries(sources=registries[0], rules=registries[1], outputs=registries[2]))

    def test_the_setting_written_on_the_class_wins_over_the_inherited_one(self):
        @kanary.source(source_id="x.scheduled", schedule="*/5 * * * *")
        class Scheduled(kanary.HTTPPushSource):
            pass

        self.assertEqual((Scheduled.interval, Scheduled.schedule), (None, "*/5 * * * *"))

        class Base(kanary.Source):
            schedule = "@hourly"

            def poll(self):
                return kanary.no_update()

        @kanary.source(source_id="x.every", interval=30)
        class Every(Base):
            pass

        self.assertEqual((Every.interval, Every.schedule), (30, None))
        with self.assertRaisesRegex(ValueError, "must not define both"):
            @kanary.source(source_id="x.both", interval=30, schedule="@hourly")
            class Both(Base):
                pass

    def test_a_waiting_helper_refuses_another_interval(self):
        @kanary.source(source_id="x.ten", interval=10)
        class Ten(kanary.HTTPPushSource):
            pass

        @kanary.source(source_id="x.cron", schedule="@hourly")
        class Cron(kanary.MQTTSource):
            topics = ["a/b"]

        with self.assertRaisesRegex(ValueError, r"HTTPPushSource waits for values in poll\(\) and runs with interval 0; remove interval=10 from source 'x.ten'"):
            Ten().init()
        with self.assertRaisesRegex(ValueError, "MQTTSource .* remove schedule='@hourly' from source 'x.cron'"):
            Cron().init()


class FakeMqttClient:
    def __init__(self, api, client_id=""):
        self.subscribed = []
        self.login = self.address = None
        self.stopped = False
        FakeMqttClient.last = self

    def username_pw_set(self, username, password):
        self.login = (username, password)

    def tls_set(self):
        self.tls = True

    def reconnect_delay_set(self, min_delay, max_delay):
        pass

    def connect_async(self, host, port, keepalive=60):
        self.address = (host, port)

    def loop_start(self):
        pass

    def loop_stop(self):
        self.stopped = True

    def disconnect(self):
        pass

    def subscribe(self, topic, qos=0):
        self.subscribed.append((topic, qos))


def fake_paho():
    import re

    client = types.ModuleType("paho.mqtt.client")
    client.Client = FakeMqttClient
    client.CallbackAPIVersion = types.SimpleNamespace(VERSION2=2)
    client.topic_matches_sub = lambda sub, topic: re.fullmatch(
        "/".join("[^/]+" if part == "+" else ".*" if part == "#" else re.escape(part) for part in sub.split("/")), topic
    ) is not None
    mqtt = types.ModuleType("paho.mqtt")
    mqtt.client = client
    paho = types.ModuleType("paho")
    paho.mqtt = mqtt
    return {"paho": paho, "paho.mqtt": mqtt, "paho.mqtt.client": client}


class MQTTSourceTest(unittest.TestCase):
    def make(self, **settings):
        return type("Mqtt", (kanary.MQTTSource,), {"source_id": "mqtt", **settings})()

    def message(self, source, topic, payload):
        source._on_message(None, None, types.SimpleNamespace(topic=topic, payload=payload))

    def test_messages_become_inputs(self):
        source = self.make(host="broker", username="kanary", password="pw", qos=1, topics={
            "plant/room_a/temperature": "room_a.temperature",
            "plant/pumps/#": None,
            "plant/env": "env",
        })
        with mock.patch.dict(sys.modules, fake_paho()):
            source.init()
        client = FakeMqttClient.last
        self.assertEqual((client.address, client.login), (("broker", 1883), ("kanary", "pw")))
        source._on_connect(client, None, None, types.SimpleNamespace(is_failure=False), None)
        self.assertEqual([topic for topic, _ in client.subscribed], ["plant/room_a/temperature", "plant/pumps/#", "plant/env"])
        self.message(source, "plant/room_a/temperature", b"21.5")
        self.message(source, "plant/pumps/p1/flow", b"3")
        self.message(source, "plant/pumps/p1/state", b"RUNNING")
        self.message(source, "plant/pumps/p2/state", b"ON")
        self.message(source, "plant/env", b'{"rh": "40", "co2": {"ppm": 420}}')
        self.message(source, "elsewhere", b"1")
        values = {m.name: m.value for m in source.poll().inputs}
        self.assertEqual(values, {
            "mqtt.connected": 1, "room_a.temperature": 21.5, "plant.pumps.p1.flow": 3.0,
            "plant.pumps.p1.state": "RUNNING", "plant.pumps.p2.state": 1.0, "env.rh": 40.0, "env.co2.ppm": 420.0,
        })
        source._on_disconnect(client, None, None, "connection lost", None)
        self.assertEqual({m.name: m.value for m in source.poll().inputs}["mqtt.connected"], 0)
        source.terminate()
        self.assertTrue(client.stopped)

    def test_settings_are_checked(self):
        with mock.patch.dict(sys.modules, fake_paho()):
            with self.assertRaisesRegex(ValueError, "map it to None"):
                self.make(topics={"plant/+/temperature": "temperature"}).init()
            with self.assertRaisesRegex(ValueError, "topics is empty"):
                self.make().init()
        with mock.patch.dict(sys.modules, {"paho": None, "paho.mqtt": None, "paho.mqtt.client": None}):
            with self.assertRaisesRegex(RuntimeError, "needs the paho-mqtt package: install it into the Python environment that runs KANARY"):
                self.make(topics=["a/b"]).init()


class PluginHealthTest(unittest.TestCase):
    def test_other_plugins_become_inputs(self):
        class Health(kanary.PluginHealthSource):
            source_id = "health"
            interval = 30.0

        class Broken(kanary.Source):
            source_id = "broken"
            interval = 30.0

            def init(self):
                raise RuntimeError("no database")

            def poll(self):
                return kanary.inputs(("x", 1))

        class Quiet(kanary.Output):
            output_id = "quiet"

            def emit(self, event):
                pass

        engine = kanary.Engine(source_registry={"health": Health, "broken": Broken}, rule_registry={}, output_registry={"quiet": Quiet})
        with self.assertLogs("kanary", level="ERROR"):
            engine.start()
        self.addCleanup(engine.shutdown)
        shown = engine.test_poll("health")["inputs"]
        mine = sorted(name for name in shown if "quiet" in name or "broken" in name or "health." in name)
        self.assertEqual(mine, ["health:output.quiet.dropped", "health:output.quiet.failed", "health:output.quiet.queued", "health:source.broken.failed"])
        self.assertEqual(shown["health:source.broken.failed"]["value"], 1)
        self.assertEqual(shown["health:source.broken.failed"]["metadata"]["error"], "no database")
        self.assertEqual(shown["health:output.quiet.failed"]["value"], 0)
        with mock.patch.object(engine, "output_backlog", return_value={"queued": 4, "dropped": 7}):
            engine.test_poll("health")
            shown = engine.test_poll("health")["inputs"]
        self.assertEqual((shown["health:output.quiet.queued"]["value"], shown["health:output.quiet.dropped"]["value"]), (4, 0))
        with self.assertRaisesRegex(RuntimeError, "reads the engine it runs in"):
            Health().poll()


class TimezoneTest(unittest.TestCase):
    def test_the_usual_ways_to_write_a_time_zone(self):
        nine = timezone(timedelta(hours=9))
        for written in ("+9", "+09", "+0900", "+09:00", "UTC+9", "GMT+09:00", "utc+9", 9):
            self.assertEqual(kanary.to_timezone(written), nine, written)
        self.assertEqual(kanary.to_timezone("-05:30"), timezone(-timedelta(hours=5, minutes=30)))
        self.assertEqual(kanary.to_timezone(-5.5), timezone(-timedelta(hours=5, minutes=30)))
        for written in ("UTC", "Z", "GMT", "+0", 0):
            self.assertEqual(kanary.to_timezone(written), timezone.utc, written)
        self.assertEqual(str(kanary.to_timezone("JST")), "Asia/Tokyo")
        self.assertEqual(str(kanary.to_timezone("Europe/Berlin")), "Europe/Berlin")
        self.assertIsNotNone(datetime.now(kanary.to_timezone("local")).utcoffset())
        for wrong in ("Mars/Olympus", "+25", "", None):
            with self.assertRaisesRegex(ValueError, "time zone"):
                kanary.to_timezone(wrong)

    def test_helpers_take_them(self):
        self.assertEqual(kanary.to_datetime("2026-11-18 21:00:00", naive_timezone="+9"), T0)
        quiet = kanary.QuietHours([("22:00", "07:00")], timezone="+09:00")
        self.addCleanup(quiet.close)
        self.assertTrue(quiet.applies(make_event(severity=kanary.WARN, at=T0 + timedelta(hours=2))))  # 23:00 at +09:00
        self.assertFalse(quiet.applies(make_event(severity=kanary.WARN, at=T0)))  # 21:00


class ValuesTest(unittest.TestCase):
    def test_to_number_and_parse_values(self):
        self.assertEqual(kanary.to_number("85%"), 85.0)
        self.assertEqual(kanary.to_number("yes"), 1.0)
        self.assertIsNone(kanary.to_number("RUNNING"))
        self.assertEqual(kanary.parse_values(" 42\n"), ({"value": 42.0}, {}))
        self.assertEqual(kanary.parse_values("load=0.5 state=RUN\nusers=3"), ({"load": 0.5, "users": 3.0}, {"state": "RUN"}))
        self.assertEqual(kanary.parse_values('{"a": {"b": "1.5"}, "on": true}'), ({"a.b": 1.5, "on": 1.0}, {}))
        self.assertEqual(kanary.to_datetime("2026-11-18T12:00:00Z"), T0)


class CommandTest(HelperTestCase):
    def command_source(self, script, **attrs):
        cls = type("Cmd", (kanary.CommandSource,), {"source_id": "cmd", "command": [sys.executable, "-c", script], **attrs})
        source = cls()
        source.init()
        return source

    def test_command_source_parses_stdout_and_exit_code(self):
        result = self.command_source("print('load=0.5 mode=auto')").poll()
        values = {m.name: m.value for m in result.inputs}
        self.assertEqual(values, {"load": 0.5, "exit_code": 0})
        self.assertEqual(result.metadata["text"], {"mode": "auto"})

        failing = self.command_source("import sys; print('oops: not parsable'); sys.stderr.write('bad'); sys.exit(3)").poll()
        self.assertEqual({m.name: m.value for m in failing.inputs}, {"exit_code": 3})
        self.assertEqual(failing.metadata["stderr"], "bad")

    def test_command_source_timeout_raises(self):
        source = self.command_source("import time; time.sleep(5)", timeout=0.2)
        with self.assertRaises(Exception):
            source.poll()

    def test_ssh_command_source_builds_noninteractive_command(self):
        cls = type("Remote", (kanary.SSHCommandSource,), {
            "source_id": "ssh", "host": "daq01", "user": "mon", "port": 2222,
            "command": ["df", "-P", "/"], "ssh_options": ["-o", "StrictHostKeyChecking=yes"],
        })
        self.assertEqual(
            cls().args(),
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "-p", "2222",
             "-o", "StrictHostKeyChecking=yes", "mon@daq01", cls().remote_command()],
        )
        self.assertTrue(cls().remote_command().startswith("( df -P /\n); printf"))
        failed = mock.Mock(returncode=255, stdout="", stderr="Connection refused")
        with mock.patch("kanary.helpers.command.subprocess.run", return_value=failed):
            with self.assertRaisesRegex(RuntimeError, "ssh to daq01 failed"):
                cls().poll()
        no_host = type("NoHost", (kanary.SSHCommandSource,), {"source_id": "ssh", "command": "true"})
        with self.assertRaisesRegex(ValueError, "NoHost.host is not set"):
            no_host().poll()
        with self.assertRaisesRegex(ValueError, "command is empty"):
            type("Empty", (kanary.CommandSource,), {"source_id": "e"})().poll()
        with self.assertRaisesRegex(ValueError, "output_format must be one of"):
            self.command_source("print(1)", output_format="yaml").poll()

    def test_ssh_marker_separates_remote_exit_codes_from_ssh_failures(self):
        def local_shell(self):  # stand-in for ssh: run the wrapped command with sh
            return ["sh", "-c", self.remote_command()]

        def remote(command, **attrs):
            cls = type("R", (kanary.SSHCommandSource,), {
                "source_id": "r", "host": "h", "command": command, "args": local_shell, **attrs})
            return cls()

        result = remote("echo load=0.5; exit 255  # comment").poll()
        self.assertEqual({m.name: m.value for m in result.inputs}, {"load": 0.5, "exit_code": 255})
        self.assertEqual(result.metadata, {"program": "ssh", "host": "h"})
        self.assertEqual({m.name: m.value for m in remote("echo 7").poll().inputs}, {"value": 7.0, "exit_code": 0})
        no_marker = mock.Mock(returncode=255, stdout="", stderr="Permission denied (publickey).")
        with mock.patch("kanary.helpers.command.subprocess.run", return_value=no_marker):
            with self.assertRaisesRegex(RuntimeError, "Permission denied"):
                remote("true").poll()
            with self.assertRaisesRegex(RuntimeError, "ssh to h failed"):
                remote("true", wrap_exit_code=False).poll()

    def test_command_metadata_keeps_only_the_program_name(self):
        result = self.command_source("print(1)", command=[sys.executable, "-c", "print(1)", "--token=secret"]).poll()
        self.assertEqual(result.metadata, {"program": Path(sys.executable).name})

    def test_command_output_passes_event_on_stdin_and_env(self):
        with TemporaryDirectory() as tmp:
            out = Path(tmp) / "out.json"
            script = (
                "import json, os, sys; data = json.load(sys.stdin); "
                f"json.dump({{'stdin': data, 'rule': os.environ['KANARY_RULE_ID']}}, open({str(out)!r}, 'w'))"
            )
            output = type("CmdOut", (kanary.CommandOutput,), {"output_id": "c", "command": [sys.executable, "-c", script]})()
            output.emit(make_event())
            written = json.loads(out.read_text())
        self.assertEqual(written["rule"], "oil.temperature")
        self.assertEqual(written["stdin"]["severity"], "ERROR")
        failing = type("Fail", (kanary.CommandOutput,), {"output_id": "f", "command": [sys.executable, "-c", "raise SystemExit(2)"]})()
        with self.assertRaisesRegex(RuntimeError, "exited with 2"):
            failing.emit(make_event())


@contextmanager
def http_server(handler_factory):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_factory)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def make_handler(received):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            return None

        def do_GET(self):
            if self.path == "/fail":
                self.send_response(500)
                self.end_headers()
                return
            body = json.dumps({"data": {"temperature": "21.5", "pump": {"running": True}, "state": "RUN",
                                        "sensors": [{"value": 3}]}}).encode()
            received.append(("GET", self.path, dict(self.headers), None))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            length = int(self.headers["Content-Length"])
            received.append(("POST", self.path, dict(self.headers), json.loads(self.rfile.read(length))))
            self.send_response(403 if self.path == "/denied" else 204)
            self.end_headers()

    return Handler


def make_event(state=kanary.FIRING, severity=kanary.ERROR, at=T0, rule_id="oil.temperature"):
    alert = kanary.Alert(rule_id=rule_id, state=state, severity=severity, tags=("oil",), message="value=95 out of range")
    return kanary.AlertEvent(
        rule_id=rule_id,
        previous_state=kanary.OK,
        current_state=state,
        previous_severity=None,
        current_severity=severity,
        transition=None,
        alert=alert,
        occurred_at=at,
    )


class HTTPTest(HelperTestCase):
    def test_http_json_source_selects_paths_and_converts(self):
        received = []
        with http_server(make_handler(received)) as base:
            cls = type("Api", (kanary.HTTPJSONSource,), {
                "source_id": "api", "url": base + "/status", "headers": {"X-Token": "t"},
                "fields": {"temperature": "data.temperature", "pump": "data.pump.running",
                           "first": "data.sensors.0.value", "state": "data.state", "gone": "data.nope"},
            })
            result = cls().poll()
            everything = type("All", (kanary.HTTPJSONSource,), {"source_id": "all", "url": base + "/"})().poll()
        self.assertEqual({m.name: m.value for m in result.inputs}, {"temperature": 21.5, "pump": 1.0, "first": 3.0})
        self.assertEqual(result.metadata["missing"], ["gone"])
        self.assertEqual(result.metadata["text"], {"state": "RUN"})
        self.assertEqual(received[0][2]["X-Token"], "t")
        self.assertIn("data.temperature", {m.name for m in everything.inputs})

    def test_webhook_and_chat_outputs_post_their_payloads(self):
        received = []
        with http_server(make_handler(received)) as base:
            webhook = type("W", (kanary.WebhookOutput,), {"output_id": "w", "url": base + "/hook"})()
            webhook.init()
            webhook.emit(make_event())
            for cls, path in ((kanary.SlackOutput, "/slack"), (kanary.DiscordOutput, "/discord"), (kanary.MattermostOutput, "/mm")):
                output = type("Chat", (cls,), {"output_id": "x", "url": base + path, "username": "kanary"})()
                output.init()
                output.emit(make_event())
            denied = type("D", (kanary.WebhookOutput,), {"output_id": "d", "url": base + "/denied"})()
            denied.init()
            with self.assertRaisesRegex(RuntimeError, "HTTP 403"):
                denied.emit(make_event())
        bodies = {path: body for _, path, _, body in received}
        self.assertEqual(bodies["/hook"]["severity"], "ERROR")
        line = "[ERROR] FIRING oil.temperature: value=95 out of range"
        slack = bodies["/slack"]
        self.assertEqual((slack["text"], slack["attachments"][0]["color"]), (line, "#E74C3C"))
        self.assertEqual(slack["attachments"][0]["blocks"][0]["text"]["text"], "*🔴 FIRING ERROR oil.temperature*\nvalue=95 out of range")
        embed = bodies["/discord"]["embeds"][0]
        self.assertEqual((embed["title"], embed["description"], embed["color"]), ("🔴 FIRING ERROR oil.temperature", "value=95 out of range", 0xE74C3C))
        self.assertEqual(bodies["/discord"]["username"], "kanary")
        attachment = bodies["/mm"]["attachments"][0]
        self.assertEqual((attachment["fallback"], attachment["color"], bodies["/mm"]["username"]), (line, "#E74C3C", "kanary"))
        missing = type("M", (kanary.WebhookOutput,), {"output_id": "m"})()
        with self.assertRaisesRegex(ValueError, "M.url is not set"):
            missing.init()

    def test_secrets_come_from_the_environment_or_a_json_file(self):
        with TemporaryDirectory() as tmp:
            (Path(tmp) / "secrets.json").write_text('{"slack_webhook_url": "https://hooks.example/abc"}')
            plugin = Path(tmp) / "chat.py"
            plugin.write_text(
                "import os\n\nimport kanary\n\n"
                "class FromFile(kanary.SlackOutput):\n"
                "    url = kanary.load_json('slack_webhook_url', filename='secrets.json')\n\n"
                "class FromEnvironment(kanary.SlackOutput):\n"
                "    url = os.environ.get('KANARY_TEST_WEBHOOK_URL')\n"
            )
            namespace = {"__file__": str(plugin), "__name__": "chat_plugin"}
            with mock.patch.dict(os.environ, {}, clear=True):
                exec(compile(plugin.read_text(), str(plugin), "exec"), namespace)
        self.assertEqual(namespace["FromFile"]().url, "https://hooks.example/abc")
        with self.assertRaisesRegex(ValueError, "url is not set"):
            namespace["FromEnvironment"]().init()  # a missing variable fails this output only

    def test_lint_goes_on_without_the_secrets_file(self):
        registries = (kanary.get_source_registry(), kanary.get_rule_registry(), kanary.get_output_registry())
        self.addCleanup(lambda: kanary.registry.replace_registries(sources=registries[0], rules=registries[1], outputs=registries[2]))
        with TemporaryDirectory() as tmp:
            (Path(tmp) / "chat.py").write_text(
                "import kanary\n\n"
                "@kanary.output(output_id='chat')\n"
                "class Chat(kanary.SlackOutput):\n"
                "    url = kanary.load_json('slack_webhook_url', filename='secrets.json')\n"
            )
            loader = kanary.RuleDirectoryLoader(Path(tmp))
            _, report = loader.inspect(lint=True)
            self.assertEqual(report.errors, [])
            self.assertTrue(any("plugin config file not found" in w and "gave None while linting" in w for w in report.warnings))
            with self.assertRaisesRegex(RuntimeError, "plugin config file not found"):
                loader.load()  # starting or reloading for real needs it
            output = io.StringIO()
            with redirect_stdout(output), mock.patch.object(sys, "argv", ["kanary", "lint", tmp]):
                self.assertEqual(kanary_main.main(), 0)
            self.assertIn("lint ok", output.getvalue())


class ProbeTest(HelperTestCase):
    def test_tcp_and_http_status(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        self.addCleanup(listener.close)
        closed = socket.socket()
        closed.bind(("127.0.0.1", 0))
        closed_port = closed.getsockname()[1]
        closed.close()
        tcp = type("T", (kanary.TCPSource,), {"source_id": "t", "timeout": 2.0, "targets": {
            "open": f"127.0.0.1:{listener.getsockname()[1]}",
            "closed": f"127.0.0.1:{closed_port}",
        }})().poll()
        values = {m.name: m.value for m in tcp.inputs}
        self.assertGreaterEqual(values["open.connect_ms"], 0)
        self.assertIsNone(values["closed.connect_ms"])
        self.assertIn("error", {m.name: m.metadata for m in tcp.inputs}["closed.connect_ms"])

        with http_server(make_handler([])) as base:
            http = type("H", (kanary.HTTPStatusSource,), {"source_id": "h", "timeout": 2.0, "urls": {
                "web": base + "/health", "broken": base + "/fail", "nothing": f"http://127.0.0.1:{closed_port}/",
            }})().poll()
        values = {m.name: m.value for m in http.inputs}
        self.assertEqual((values["web.status"], values["broken.status"], values["nothing.status"]), (200, 500, None))
        self.assertIsNone(values["nothing.response_ms"])

    def test_bad_settings_raise(self):
        cases = [
            (kanary.TCPSource, {"targets": {"x": "host"}}, "must be 'host:port'"),
            (kanary.TCPSource, {"targets": {}}, "targets is empty"),
            (kanary.HTTPStatusSource, {"urls": {"x": "ftp://host"}}, "must start with http"),
            (kanary.PingSource, {"hosts": {}}, "hosts is empty"),
        ]
        for base, attrs, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                type("B", (base,), {"source_id": "b", **attrs})().poll()

    def test_status_rule_fires_on_error_status_and_no_answer(self):
        rule = make_rule(kanary.ExpectedValueRule, "fake:*", expected=[200, 204], missing_fires=True)
        driver = self.drive(rule)
        self.assertEqual(driver.feed({"web.status": 200, "api.status": 204}).state, kanary.OK)
        alert = driver.feed({"web.status": 503, "api.status": 204})
        self.assertEqual((alert.state, alert.message), (kanary.FIRING, "fake:web.status=503 is not the expected 200 or 204"))
        self.assertEqual(driver.feed({"web.status": None, "api.status": 204}).state, kanary.FIRING)


class FilesTest(HelperTestCase):
    def test_file_source(self):
        with TemporaryDirectory() as tmp:
            level = Path(tmp) / "level.txt"
            level.write_text("1.0\n2.5\n\n")
            cls = type("F", (kanary.FileSource,), {"source_id": "f", "read_last_line": True,
                                                    "paths": {"level": str(level), "gone": str(Path(tmp) / "nope")}})
            values = {m.name: m.value for m in cls().poll().inputs}
        self.assertEqual(values["level.exists"], 1)
        self.assertEqual(values["level.value"], 2.5)
        self.assertEqual(values["level.size_bytes"], 9)
        self.assertLess(values["level.age_seconds"], 60)
        self.assertEqual(values["gone.exists"], 0)

    def test_file_source_globs_and_lists(self):
        with TemporaryDirectory() as tmp:
            old, new = Path(tmp) / "run_1.root", Path(tmp) / "run_2.root"
            old.write_text("12")
            new.write_text("3.5\n")
            os.utime(old, (1_000_000_000, 1_000_000_000))
            cls = type("F", (kanary.FileSource,), {"source_id": "f", "read_last_line": True, "paths": {
                "runs": str(Path(tmp) / "run_*.root"), "none": str(Path(tmp) / "*.txt"),
            }})
            result = cls().poll()
            listed = type("L", (kanary.FileSource,), {"source_id": "l", "paths": [str(old), str(Path(tmp) / "*.root")]})
            listed_names = {m.name for m in listed().poll().inputs}
        values = {m.name: m.value for m in result.inputs}
        self.assertEqual((values["runs.count"], values["runs.total_size_bytes"], values["runs.value"]), (2, 6, 3.5))
        self.assertEqual({m.name: m.metadata for m in result.inputs}["runs.age_seconds"]["path"], str(new))
        self.assertEqual((values["none.count"], values["none.exists"]), (0, 0))
        self.assertNotIn("none.age_seconds", values)
        self.assertTrue({"run_1.root.exists", "root.count", "root.age_seconds"} <= listed_names)
        clash = type("C", (kanary.FileSource,), {"source_id": "c", "paths": ["/a/x.txt", "/b/x.txt"]})
        with self.assertRaisesRegex(ValueError, "both get the name 'x.txt'"):
            clash().poll()

    def test_log_count_source_handles_partial_lines_and_rotation(self):
        with TemporaryDirectory() as tmp:
            log = Path(tmp) / "run.log"
            log.write_text("ERROR before start\n")
            cls = type("L", (kanary.LogCountSource,), {"source_id": "l", "path": str(log),
                                                        "patterns": {"errors": r"\bERROR\b", "warn": "WARN"}})
            source = cls()
            source.init()

            def counts():
                return {m.name: m.value for m in source.poll().inputs}

            self.assertEqual(counts(), {"errors": 0, "warn": 0})
            with log.open("a") as handle:
                handle.write("ERROR one\nWARN two\nERROR thr")
            self.assertEqual(counts(), {"errors": 1, "warn": 1})
            with log.open("a") as handle:
                handle.write("ee\n")
            self.assertEqual(counts(), {"errors": 1, "warn": 0})  # the completed partial line
            log.unlink()
            log.write_text("ERROR after rotation\n")
            self.assertEqual(counts(), {"errors": 1, "warn": 0})
            # copytruncate: same file truncated, then written past the old position before the next poll.
            with log.open("r+") as handle:
                handle.truncate(0)
            with log.open("a") as handle:
                handle.write("WARN new content that is longer than before\nERROR again\n")
            self.assertEqual(counts(), {"errors": 1, "warn": 1})

    def test_file_output_appends_json_lines_next_to_plugin(self):
        with TemporaryDirectory() as tmp:
            cls = type("O", (kanary.FileOutput,), {"output_id": "o", "path": "logs/alerts.jsonl"})
            cls.__kanary_definition_file__ = str(Path(tmp) / "plugin.py")
            output = cls()
            output.init()
            output.emit(make_event())
            output.emit(make_event(state=kanary.OK))
            lines = (Path(tmp) / "logs" / "alerts.jsonl").read_text().splitlines()
        self.assertEqual([json.loads(line)["state"] for line in lines], ["FIRING", "OK"])


class SQLTest(HelperTestCase):
    def connect(self):
        conn = sqlite3.connect(":memory:")
        conn.executescript(
            """
            CREATE TABLE wide (observed_at TEXT, temperature REAL, state TEXT);
            INSERT INTO wide VALUES ('2026-11-18T11:00:00+00:00', 19.0, 'OFF'), ('2026-11-18T12:00:00+00:00', 21.5, 'RUN');
            CREATE TABLE long (observed_at TEXT, name TEXT, value REAL);
            INSERT INTO long VALUES ('2026-11-18T12:00:00+00:00', 'a.temp_c', 21.0), ('2026-11-18T11:00:00+00:00', 'a.temp_c', 18.0),
                                    ('2026-11-18T12:00:00+00:00', 'b.temp_c', 22.0), ('2026-11-18T12:00:00+00:00', 'other', 1.0);
            """
        )
        return conn

    def test_wide_and_long_sources(self):
        connect = lambda _self: self.connect()
        wide = type("W", (kanary.SQLWideSource,), {
            "source_id": "w", "connect": connect, "timestamp_column": "observed_at",
            "query": "SELECT * FROM wide ORDER BY observed_at DESC LIMIT 1",
        })()
        wide.init()
        result = wide.poll()
        wide.terminate()
        self.assertEqual([(m.name, m.value, m.timestamp) for m in result.inputs], [("temperature", 21.5, T0)])
        self.assertEqual(result.metadata["text"], {"state": "RUN"})

        long = type("L", (kanary.SQLLongSource,), {
            "source_id": "l", "connect": connect, "timestamp_column": "observed_at",
            "query": "SELECT * FROM long ORDER BY observed_at DESC",
            "names": {"a.temp_c": "a.temperature", "b.temp_c": "b.temperature"},
        })()
        long.init()
        values = {m.name: (m.value, m.metadata["name"]) for m in long.poll().inputs}
        local = type("Z", (kanary.SQLWideSource,), {
            "source_id": "z", "connect": connect, "timestamp_column": "observed_at", "timestamp_timezone": "Asia/Tokyo",
            "query": "SELECT '2026-11-18 21:00:00' AS observed_at, 1.0 AS x",
        })()
        local.init()
        self.assertEqual(local.poll().inputs[0].timestamp, T0)  # 21:00 JST == 12:00 UTC
        bad_zone = type("BZ", (kanary.SQLWideSource,), {
            "source_id": "bz", "connect": connect, "timestamp_column": "observed_at", "timestamp_timezone": "Mars/Base",
            "query": "SELECT '2026-11-18 21:00:00' AS observed_at, 1.0 AS x",
        })()
        bad_zone.init()
        with self.assertRaises(Exception):
            bad_zone.poll()
        self.assertEqual(values, {"a.temperature": (21.0, "a.temp_c"), "b.temperature": (22.0, "b.temp_c")})

        empty = type("E", (kanary.SQLWideSource,), {"source_id": "e", "connect": connect, "query": "SELECT * FROM wide WHERE 0"})()
        empty.init()
        self.assertEqual(empty.poll().status, "empty")
        with self.assertRaisesRegex(ValueError, "X.database is empty"):
            type("X", (kanary.SQLLongSource,), {"source_id": "x"})().init()
        no_query = type("Q", (kanary.SQLLongSource,), {"source_id": "q", "connect": connect})()
        no_query.init()
        with self.assertRaisesRegex(ValueError, "Q needs table"):
            no_query.poll()

    def test_queries_are_built_from_settings(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "plant.db"
            conn = sqlite3.connect(path)
            now = datetime.now().replace(microsecond=0)  # stored without a zone: this host's local time
            recent = (now - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
            older = (now - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
            ancient = (now - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")
            conn.executescript(f"""
                CREATE TABLE env (observed_at TEXT, temperature REAL, state TEXT);
                INSERT INTO env VALUES ('{older}', 19.0, 'OFF'), ('{recent}', 21.5, 'RUN');
                CREATE TABLE samples (observed_at TEXT, metric TEXT, val REAL);
                INSERT INTO samples VALUES ('{recent}', 'a', 2.0), ('{older}', 'a', 1.0),
                                           ('{older}', 'b', 5.0), ('{ancient}', 'c', 9.0);
            """)
            conn.commit()
            conn.close()
            settings = {"database_type": "sqlite", "database": str(path), "timestamp_column": "observed_at"}

            def poll(base, **attrs):
                source = type("S", (base,), {"source_id": "s", **settings, **attrs})()
                source.init()
                try:
                    return source, source.poll()
                finally:
                    source.terminate()

            wide, result = poll(kanary.SQLWideSource, table="env")
            self.assertEqual({m.name: m.value for m in result.inputs}, {"temperature": 21.5})
            self.assertEqual(result.metadata["text"], {"state": "RUN"})
            _, result = poll(kanary.SQLWideSource, table="env", columns={"t": "temperature"})
            self.assertEqual({m.name: m.value for m in result.inputs}, {"t": 21.5})
            long, result = poll(kanary.SQLLongSource, table="samples", name_column="metric", value_column="val",
                                lookback=3600)
            self.assertEqual({m.name: m.value for m in result.inputs}, {"a": 2.0, "b": 5.0})
            self.assertIn('JOIN (SELECT "metric", MAX("observed_at")', long.build_query()[0])
        with self.assertRaisesRegex(ValueError, "not a plain table or column name"):
            type("S", (kanary.SQLWideSource,), {"source_id": "s", **settings, "table": "env; DROP"})().build_query()
        with self.assertRaisesRegex(ValueError, "database_type must be"):
            type("S", (kanary.SQLWideSource,), {"source_id": "s", **settings, "database_type": "oracle"})().connect()
        mysql = type("S", (kanary.SQLWideSource,), {"source_id": "s", **settings, "database_type": "mysql", "table": "db.env"})()
        self.assertEqual(mysql.build_query()[0], "SELECT * FROM `db`.`env` ORDER BY `observed_at` DESC LIMIT 1")


class QuietHoursAndOnCallTest(HelperTestCase):
    def test_on_call_schedule(self):
        schedule = kanary.OnCallSchedule(
            [
                {"weekdays": ["mon", "tue", "wed", "thu", "fri"], "from": "09:00", "to": "18:00", "recipients": ["day"]},
                {"weekdays": ["fri"], "from": "18:00", "to": "09:00", "recipients": "night, backup"},
                {"start": "2026-12-28T09:00:00", "end": "2027-01-04T09:00:00", "recipients": ["holiday"]},
            ],
            timezone="Asia/Tokyo",
            default=["control-room"],
        )
        tokyo = lambda *args: datetime(*args, tzinfo=timezone(timedelta(hours=9)))
        self.assertEqual(schedule.recipients(tokyo(2026, 11, 18, 10, 0)), ["day"])  # Wednesday
        self.assertEqual(schedule.recipients(tokyo(2026, 11, 20, 23, 0)), ["night", "backup"])  # Friday night
        self.assertEqual(schedule.recipients(tokyo(2026, 11, 21, 3, 0)), ["night", "backup"])  # wraps into Saturday
        self.assertEqual(schedule.recipients(tokyo(2026, 11, 19, 3, 0)), ["control-room"])  # Thursday early
        self.assertEqual(schedule.recipients(tokyo(2026, 12, 29, 10, 0)), ["holiday"])  # dated wins
        with self.assertRaisesRegex(ValueError, "shift #1 must list recipients"):
            kanary.OnCallSchedule([{"weekdays": ["mon"]}])

    def test_mail_output_uses_recipients_for(self):
        sent = []

        class FakeSMTP:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def starttls(self):
                pass

            def send_message(self, message):
                sent.append(message["To"])

        cls = type("M", (kanary.MailOutput,), {
            "output_id": "m", "use_starttls": False, "smtp_host": "localhost", "sender": "kanary@example.org",
            "recipients_for": lambda self, event: ["oncall@example.org"],
        })
        with mock.patch("kanary.helpers.mail.smtplib.SMTP", FakeSMTP):
            output = cls()
            output.init()  # recipients_for() replaces recipients
            output.emit(make_event())
        self.assertEqual(sent, ["oncall@example.org"])


class NoticeTest(unittest.TestCase):
    def event(self, **changes):
        alert = kanary.Alert(
            rule_id="oil.temperature", state=changes.get("state", kanary.FIRING), severity=changes.get("severity", kanary.ERROR),
            tags=("oil",), owner="cryo", message=changes.get("message", "value=95 out of range"),
            payload={"values": {"oil:temperature": 95.0}}, active_since=changes.get("active_since"),
            runbook=changes.get("runbook"),
        )
        return kanary.AlertEvent(
            rule_id="oil.temperature", previous_state=changes.get("previous_state", kanary.OK),
            current_state=alert.state, previous_severity=changes.get("previous_severity"),
            current_severity=alert.severity, transition=changes.get("transition"), alert=alert, occurred_at=T0,
        )

    def test_title_color_and_fields(self):
        notice = kanary.describe(self.event())
        self.assertEqual((notice.headline, notice.color_hex), ("🔴 FIRING ERROR oil.temperature", "#E74C3C"))
        self.assertEqual(
            [label for label, _ in notice.fields], ["State", "Severity", "Owner", "Tags", "Values"],
        )  # no "Since" for an alert that began with this event
        self.assertEqual(dict(notice.fields)["Values"], "oil:temperature = 95")
        self.assertEqual(notice.text, "[ERROR] FIRING oil.temperature: value=95 out of range")

    def test_the_rules_runbook_goes_with_its_notifications(self):
        runbook = "Check the chiller: https://slowdash.example.org/oil?panel=1."
        event = self.event(runbook=runbook)
        self.assertEqual(kanary.describe(event).fields[-1], ("Runbook", runbook))
        self.assertEqual(kanary.event_record(event)["runbook"], runbook)
        html = type("M", (kanary.MailOutput,), {"sender": "k@example.org"})().html(event)
        self.assertIn('<a href="https://slowdash.example.org/oil?panel=1">https://slowdash.example.org/oil?panel=1</a>.', html)
        self.assertNotIn("Runbook", [label for label, _ in kanary.describe(self.event()).fields])

    def test_states_and_changes(self):
        recovered = kanary.describe(self.event(state=kanary.OK, previous_state=kanary.FIRING, previous_severity=kanary.ERROR))
        self.assertEqual((recovered.headline, recovered.color), ("✅ OK oil.temperature", 0x2ECC71))
        self.assertEqual(dict(recovered.fields)["Severity"], "was ERROR")
        self.assertEqual(recovered.text, "OK oil.temperature: value=95 out of range")  # no "[ERROR]" on a recovery
        silenced = kanary.describe(self.event(state=kanary.SILENCED, previous_state=kanary.FIRING))
        self.assertEqual((silenced.symbol, silenced.color_hex), ("🔕", "#95A5A6"))
        escalated = kanary.describe(self.event(
            severity=kanary.CRITICAL, previous_state=kanary.FIRING, previous_severity=kanary.ERROR,
            transition=kanary.ESCALATED, active_since=T0 - timedelta(minutes=5),
        ))
        self.assertEqual(escalated.headline, "🚨 ESCALATED CRITICAL oil.temperature")
        fields = dict(escalated.fields)
        self.assertEqual((fields["State"], fields["Severity"]), ("FIRING", "ERROR → CRITICAL"))
        self.assertIn("Since", fields)

    def test_chat_payloads_escape_link_and_cut(self):
        event = self.event(message="a < b & c" + "x" * 5000)
        slack = type("S", (kanary.SlackOutput,), {"viewer_url": "http://kanary.example.org/"})().payload(event)
        text = slack["attachments"][0]["blocks"][0]["text"]["text"]
        self.assertTrue(text.startswith("*<http://kanary.example.org/|🔴 FIRING ERROR oil.temperature>*\na &lt; b &amp; c"))
        self.assertEqual(len(text), 3000)
        discord = type("D", (kanary.DiscordOutput,), {"viewer_url": "http://kanary.example.org/"})().payload(event)
        self.assertEqual((discord["embeds"][0]["url"], len(discord["embeds"][0]["description"])), ("http://kanary.example.org/", 4096))

    def test_a_summary_is_one_message(self):
        events = [
            self.event(),
            self.event(state=kanary.OK, previous_state=kanary.FIRING, previous_severity=kanary.ERROR),
            kanary.AlertEvent(rule_id="pump.flow", previous_state=kanary.OK, current_state=kanary.FIRING, previous_severity=None,
                              current_severity=kanary.WARN, transition=None, occurred_at=T0,
                              alert=kanary.Alert(rule_id="pump.flow", state=kanary.FIRING, severity=kanary.WARN, message="flow=0")),
        ]
        summary = kanary.digest(events)
        self.assertEqual(summary.title, "3 alert events, 2 rules")
        self.assertEqual(summary.lines(), [
            "✅ OK oil.temperature (2 events): value=95 out of range",
            "🟡 FIRING WARN pump.flow: flow=0",
        ])
        self.assertEqual((summary.color_hex, summary.text), ("#F1C40F", "3 alert events, 2 rules: 1 still alerting"))
        slack = kanary.SlackOutput().summary_payload(events)
        self.assertEqual(slack["attachments"][0]["color"], "#F1C40F")
        self.assertIn("pump.flow: flow=0", slack["attachments"][0]["blocks"][0]["text"]["text"])
        self.assertEqual(kanary.DiscordOutput().summary_payload(events)["embeds"][0]["title"], "3 alert events, 2 rules")
        self.assertEqual(len(kanary.WebhookOutput().summary_payload(events)["events"]), 3)
        sent = []
        mail = type("M", (kanary.MailOutput,), {"sender": "k@example.org", "recipients": ["o@example.org"],
                                                "send_mail": lambda self, message: sent.append(message)})()
        mail.emit_summary(events)
        self.assertEqual(sent[0]["Subject"], "[KANARY] 3 alert events, 2 rules")
        self.assertIn("🟡 FIRING WARN pump.flow: flow=0", sent[0].get_body(("plain",)).get_content())

    def test_mail_has_text_and_html_parts(self):
        mail = type("M", (kanary.MailOutput,), {"sender": "kanary@example.org", "subject_prefix": "[KANARY]"})()
        message = mail.message(self.event(message="<b>hot</b>"))
        self.assertEqual(message["Subject"], "[KANARY] FIRING ERROR oil.temperature")
        text = message.get_body(("plain",)).get_content()
        self.assertIn("State:    OK → FIRING", text)
        page = message.get_body(("html",)).get_content()
        self.assertIn("border-left:6px solid #E74C3C", page)
        self.assertIn("&lt;b&gt;hot&lt;/b&gt;", page)
        plain = type("P", (kanary.MailOutput,), {"sender": "k@example.org", "html": lambda self, event: None})()
        self.assertFalse(plain.message(self.event()).is_multipart())


class BrokenTemplateTest(unittest.TestCase):
    def test_a_broken_template_does_not_stop_kanary(self):
        with mock.patch.object(kanary_main.scaffold, "load_templates", side_effect=ValueError("template header is missing: level")):
            parser = kanary_main.build_parser()  # built on every start, also for `kanary run`
            args = parser.parse_args(["run", "plugins"])
        self.assertEqual(args.command, "run")


class ReviewRegressionTest(HelperTestCase):
    def test_append_keeps_imports_out_of_docstrings(self):
        merged = kanary.scaffold._insert_imports('"""Plugins.\n\nMore.\n"""\n\nX = 1\n', ["import os"])
        self.assertEqual(merged, '"""Plugins.\n\nMore.\n"""\nimport os\n\n\nX = 1\n')

    def test_decimal_values_are_numbers(self):
        from decimal import Decimal
        self.assertEqual(kanary.to_number(Decimal("1.5")), 1.5)

    def test_settings_can_be_assigned_in_init(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        self.addCleanup(listener.close)
        port = listener.getsockname()[1]

        def init(self):
            self.targets = {"a": f"127.0.0.1:{port}"}  # e.g. from kanary.load_toml()

        source = type("P", (kanary.TCPSource,), {"source_id": "probe", "init": init})()
        source.init()
        self.assertIsNotNone({m.name: m.value for m in source.poll().inputs}["a.connect_ms"])
        self.assertEqual(validate_registries(sources={"probe": type(source)}, rules={}, outputs={}).errors, [])

    def test_sql_accepts_mapping_rows_and_percent_without_params(self):
        def connect(_self):
            conn = sqlite3.connect(":memory:")
            conn.execute("CREATE TABLE t (name TEXT, value REAL)")
            conn.execute("INSERT INTO t VALUES ('a%', 1.0)")
            conn.row_factory = lambda cursor, row: {c[0]: v for c, v in zip(cursor.description, row)}
            return conn

        source = type("L", (kanary.SQLLongSource,), {
            "source_id": "l", "connect": connect, "query": "SELECT * FROM t WHERE name LIKE 'a%'",
        })()
        source.init()
        self.assertEqual({m.name: m.value for m in source.poll().inputs}, {"a%": 1.0})

    def test_http_json_metadata_drops_query_string(self):
        with http_server(make_handler([])) as base:
            cls = type("Api", (kanary.HTTPJSONSource,), {"source_id": "api", "url": base + "/s?api_key=secret"})
            self.assertNotIn("secret", cls().poll().metadata["url"])


class QuietHoursTest(unittest.TestCase):
    """QuietHours is called explicitly from emit(), like OutputFollowups."""

    NIGHT = datetime(2026, 11, 18, 14, 0, tzinfo=timezone.utc)  # 23:00 in Tokyo
    DAY = datetime(2026, 11, 18, 3, 0, tzinfo=timezone.utc)  # 12:00 in Tokyo
    MORNING = datetime(2026, 11, 18, 22, 0, tzinfo=timezone.utc)  # 07:00 in Tokyo

    def setUp(self):
        self.now = self.NIGHT
        self.sent = []
        self.quiet = kanary.QuietHours(
            [("22:00", "07:00")], timezone="Asia/Tokyo", minimum_severity="CRITICAL", now_fn=lambda: self.now
        )
        self.addCleanup(self.quiet.close)

    def send(self, state, severity, *, at=None, rule="r"):
        event = make_event(state=state, severity=severity, at=at or self.now, rule_id=rule)
        return self.quiet.send(event, lambda e: self.sent.append((e.rule_id, e.current_state.value, kanary.severity_label(e.current_severity))))

    def quiet_with(self, release):
        self.summaries = []
        quiet = kanary.QuietHours(
            [("22:00", "07:00")], timezone="Asia/Tokyo", minimum_severity="CRITICAL", now_fn=lambda: self.now,
            release=release, summary=lambda events: self.summaries.append([e.rule_id for e in events]),
        )
        self.addCleanup(quiet.close)
        self.quiet = quiet
        return quiet

    def test_release_latest_sends_one_event_per_rule(self):
        self.quiet_with("latest")
        for state in (kanary.FIRING, kanary.OK, kanary.FIRING):
            self.send(state, kanary.WARN, rule="a")
        self.send(kanary.FIRING, kanary.WARN, rule="b")
        self.assertEqual(self.quiet.release_due(now=self.MORNING), 2)
        self.assertEqual(self.sent, [("a", "FIRING", "WARN"), ("b", "FIRING", "WARN")])

    def test_release_summary_sends_one_message(self):
        self.quiet_with("summary")
        self.send(kanary.FIRING, kanary.WARN, rule="a")
        self.send(kanary.OK, kanary.WARN, rule="a")
        self.send(kanary.FIRING, kanary.WARN, rule="b")
        self.assertEqual(self.quiet.release_due(now=self.MORNING), 3)
        self.assertEqual((self.summaries, self.sent), ([["a", "a", "b"]], []))
        self.send(kanary.FIRING, kanary.WARN, rule="c", at=self.NIGHT + timedelta(days=1))
        self.quiet.release_due(now=self.MORNING + timedelta(days=1))
        self.assertEqual(self.sent, [("c", "FIRING", "WARN")])  # a single held event goes out as it is

    def test_an_urgent_event_replaces_held_ones_unless_all(self):
        self.quiet_with("summary")
        self.send(kanary.FIRING, kanary.WARN, rule="a")
        self.send(kanary.FIRING, kanary.CRITICAL, rule="a")  # urgent: now, the held one is older
        self.assertEqual((self.sent, self.quiet.held()), ([("a", "FIRING", "CRITICAL")], []))

    def test_release_is_checked(self):
        with self.assertRaisesRegex(ValueError, "release must be one of"):
            kanary.QuietHours([("22:00", "07:00")], release="newest")
        with self.assertRaisesRegex(ValueError, "needs summary="):
            kanary.QuietHours([("22:00", "07:00")], release="summary")

    def test_holds_below_minimum_at_night_and_sends_in_order_at_the_end(self):
        self.assertTrue(self.send(kanary.FIRING, kanary.ERROR, at=self.DAY))  # daytime: now
        self.sent.clear()
        self.assertFalse(self.send(kanary.FIRING, kanary.WARN))
        self.assertFalse(self.send(kanary.OK, kanary.WARN))
        self.assertTrue(self.send(kanary.FIRING, kanary.CRITICAL, rule="other"))  # urgent: now
        self.assertEqual(self.sent, [("other", "FIRING", "CRITICAL")])
        self.assertEqual([(release, event.current_state.value) for release, event in self.quiet.held()],
                         [(self.MORNING, "FIRING"), (self.MORNING, "OK")])
        self.assertIsNotNone(self.quiet._timer)
        self.assertEqual(self.quiet.release_due(now=self.MORNING - timedelta(seconds=1)), 0)
        self.assertEqual(self.quiet.release_due(now=self.MORNING), 2)
        self.assertEqual(self.sent[1:], [("r", "FIRING", "WARN"), ("r", "OK", "WARN")])
        self.assertEqual(self.quiet.held(), [])
        self.assertIsNone(self.quiet._timer)

    def test_urgent_event_sends_the_rules_held_events_first(self):
        self.send(kanary.FIRING, kanary.WARN)
        self.send(kanary.FIRING, kanary.WARN, rule="other")
        self.send(kanary.FIRING, kanary.CRITICAL)
        self.assertEqual(self.sent, [("r", "FIRING", "WARN"), ("r", "FIRING", "CRITICAL")])
        self.assertEqual([event.rule_id for _, event in self.quiet.held()], ["other"])

    def test_immediate_failure_propagates_and_held_failure_is_logged(self):
        def broken(_event):
            raise RuntimeError("down")

        with self.assertRaisesRegex(RuntimeError, "down"):
            self.quiet.send(make_event(severity=kanary.CRITICAL, at=self.NIGHT), broken)
        self.quiet.send(make_event(severity=kanary.WARN, at=self.NIGHT), broken)
        with self.assertLogs("kanary.output", level="ERROR"):
            self.assertEqual(self.quiet.release_due(now=self.MORNING), 1)

    def test_drop_mode_close_and_validation(self):
        dropping = kanary.QuietHours([("22:00", "07:00")], timezone="Asia/Tokyo", defer=False)
        self.addCleanup(dropping.close)
        self.assertFalse(dropping.send(make_event(severity=kanary.WARN, at=self.NIGHT), self.sent.append))
        self.assertEqual((self.sent, dropping.held()), ([], []))
        self.send(kanary.FIRING, kanary.WARN)
        with self.assertLogs("kanary.output", level="WARNING"):
            self.quiet.close()
        with self.assertRaisesRegex(RuntimeError, "closed"):
            self.send(kanary.FIRING, kanary.WARN)
        with self.assertRaisesRegex(ValueError, "must be a"):
            kanary.QuietHours([("25:00", "x")])
        with self.assertRaises(Exception):
            kanary.QuietHours([("22:00", "07:00")], timezone="Mars/Base")

    def test_timer_sends_held_events_in_real_time(self):
        quiet = kanary.QuietHours([("00:00", "00:00")])  # placeholder windows; patched below
        self.addCleanup(quiet.close)
        done = threading.Event()
        soon = datetime.now(timezone.utc) + timedelta(seconds=0.3)
        with mock.patch.object(quiet, "applies", return_value=True), \
                mock.patch("kanary.helpers.oncall.quiet_hours_end", return_value=soon):
            quiet.send(make_event(severity=kanary.WARN), lambda e: done.set())
        self.assertTrue(done.wait(timeout=3.0))


class NewShowTest(unittest.TestCase):
    def test_show_prints_template_without_writing(self):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = kanary_main._run_new(kanary_main.build_parser().parse_args(["new", "--show", "ping"]))
        self.assertEqual(code, 0)
        self.assertIn("class TemplatePing(kanary.PingSource):", buffer.getvalue())
        self.assertNotIn("# kanary-template:", buffer.getvalue())
        with redirect_stdout(io.StringIO()), mock.patch("sys.stderr", io.StringIO()):
            self.assertEqual(kanary_main._run_new(kanary_main.build_parser().parse_args(["new", "--show", "nope"])), 2)

    def test_list_separates_basic_and_advanced(self):
        table = kanary.scaffold.format_template_table()
        self.assertLess(table.index("BASIC"), table.index("ADVANCED"))
        self.assertLess(table.index("ping"), table.index("ADVANCED"))
        self.assertGreater(table.index("followups"), table.index("ADVANCED"))


if __name__ == "__main__":
    unittest.main()
