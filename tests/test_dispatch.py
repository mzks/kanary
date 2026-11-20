"""Outputs run on their own threads: what reaches them, in what order, and what never waits for them."""

from datetime import datetime, timedelta, timezone
import queue
from tempfile import TemporaryDirectory
from pathlib import Path
import threading
import time
import unittest
from unittest import mock

import kanary
from kanary import engine as engine_module
from kanary.api import PollUnavailableError
from kanary.runtime import EngineRuntime, RuntimeConfig

T0 = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)


class FakeSource(kanary.Source):
    source_id = "fake"
    interval = 60.0

    def poll(self):
        return kanary.no_update()


class OtherSource(FakeSource):
    source_id = "other"


def high_rule(rule_id, selector):
    return type(
        "HighRule",
        (kanary.ThresholdRule,),
        {
            "rule_id": rule_id,
            "inputs": selector,
            "severity": kanary.WARN,
            "tags": ["test"],
            "direction": "high",
            "thresholds": [(90, kanary.WARN)],
        },
    )


def recording_output(output_id, *, gate=None, entered=None, fail=False, log=None):
    """An output class that records events; emit() waits while gate is cleared."""
    events = []

    class Recording(kanary.Output):
        def init(self):
            if log is not None:
                log.append(("init", self, threading.current_thread().name))

        def emit(self, event):
            if log is not None:
                log.append(("emit", self, threading.current_thread().name))
            if entered is not None:
                entered.set()
            if gate is not None:
                gate.wait(5)
            if fail:
                raise RuntimeError("destination is down")
            events.append(event)

        def terminate(self):
            if log is not None:
                log.append(("terminate", self, threading.current_thread().name))

    Recording.output_id = output_id
    Recording.events = events
    return Recording


def wait_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class DispatchTestCase(unittest.TestCase):
    def make_engine(self, outputs, *, rules=None, sources=None, store=None):
        engine = kanary.Engine(
            source_registry=sources or {"fake": FakeSource, "other": OtherSource},
            rule_registry=rules or {"t.high": high_rule("t.high", "fake:t"), "o.high": high_rule("o.high", "other:o")},
            output_registry=outputs,
            now_fn=lambda: T0,
            store=store,
        )
        engine.start()
        self.addCleanup(engine.shutdown)
        return engine

    def feed(self, engine, value, *, source="fake", name="t", at=T0):
        engine.evaluate_source(source, kanary.inputs((name, value, at)), now=at)

    def open_gate_on_cleanup(self, gate):
        self.addCleanup(gate.set)  # registered after the engine, so it runs before shutdown


class IndependenceTest(DispatchTestCase):
    def test_a_stuck_output_delays_neither_evaluation_nor_other_outputs(self):
        gate = threading.Event()
        slow = recording_output("slow", gate=gate)
        fast = recording_output("fast")
        engine = self.make_engine({"slow": slow, "fast": fast})
        self.open_gate_on_cleanup(gate)

        started = time.monotonic()
        self.feed(engine, 95.0)
        self.feed(engine, 95.0, source="other", name="o")
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertTrue(wait_until(lambda: len(fast.events) == 2))
        self.assertEqual(slow.events, [])
        self.assertGreaterEqual(engine.output_backlog("slow")["queued"], 1)

        gate.set()
        self.assertTrue(engine.flush_outputs())
        self.assertEqual([event.rule_id for event in slow.events], ["t.high", "o.high"])

    def test_events_of_one_output_keep_their_order(self):
        gate = threading.Event()
        out = recording_output("out", gate=gate)
        engine = self.make_engine({"out": out})
        self.open_gate_on_cleanup(gate)
        for value in (95.0, 50.0, 95.0, 50.0):
            self.feed(engine, value)
        gate.set()
        self.assertTrue(engine.flush_outputs())
        self.assertEqual([event.current_state for event in out.events], [kanary.FIRING, kanary.OK, kanary.FIRING, kanary.OK])

    def test_a_full_queue_drops_the_oldest_event(self):
        gate, entered = threading.Event(), threading.Event()
        out = recording_output("out", gate=gate, entered=entered)
        engine = self.make_engine({"out": out})
        self.open_gate_on_cleanup(gate)
        engine._output_workers["out"].max_queued = 2
        self.feed(engine, 95.0)  # being sent
        self.assertTrue(entered.wait(2))
        with self.assertLogs("kanary.output", level="WARNING") as logs:
            for value in (50.0, 95.0, 50.0):  # three wait; the first of them is dropped
                self.feed(engine, value)
        self.assertTrue(any("dropped an event" in line for line in logs.output))
        self.assertEqual(engine.output_backlog("out"), {"queued": 3, "dropped": 1})
        gate.set()
        self.assertTrue(engine.flush_outputs())
        self.assertEqual([event.current_state for event in out.events], [kanary.FIRING, kanary.FIRING, kanary.OK])

    def test_a_queued_event_keeps_the_alert_as_it_was(self):
        gate = threading.Event()
        out = recording_output("out", gate=gate)
        engine = self.make_engine({"out": out})
        self.open_gate_on_cleanup(gate)
        self.feed(engine, 95.0)
        engine.acknowledge("t.high", operator="alice")  # changes the engine's alert in place
        gate.set()
        self.assertTrue(engine.flush_outputs())
        firing, acked = out.events
        self.assertEqual((firing.current_state, firing.alert.state, firing.alert.acked_by), (kanary.FIRING, kanary.FIRING, None))
        self.assertEqual((acked.current_state, acked.alert.acked_by), (kanary.ACKED, "alice"))

    def test_remote_ack_does_not_hold_the_engine(self):
        release = threading.Event()

        class RemoteRule(high_rule("t.high", "fake:t")):
            def acknowledge_remote(self, engine, *, operator, reason):
                release.wait(5)  # a slow peer

        engine = self.make_engine({}, rules={"t.high": RemoteRule, "o.high": high_rule("o.high", "other:o")})
        self.addCleanup(release.set)
        self.feed(engine, 95.0)
        acking = threading.Thread(target=engine.acknowledge, args=("t.high",), kwargs={"operator": "alice"})
        acking.start()
        time.sleep(0.05)
        started = time.monotonic()
        self.feed(engine, 95.0, source="other", name="o")
        self.assertLess(time.monotonic() - started, 0.5)
        release.set()
        acking.join(2)
        self.assertEqual(engine.alerts["t.high"].state, kanary.ACKED)


class LifecycleTest(DispatchTestCase):
    def test_shutdown_sends_what_is_queued_then_terminates_on_the_output_thread(self):
        gate, log = threading.Event(), []
        out = recording_output("out", gate=gate, log=log)
        engine = kanary.Engine(
            source_registry={"fake": FakeSource},
            rule_registry={"t.high": high_rule("t.high", "fake:t")},
            output_registry={"out": out},
            now_fn=lambda: T0,
        )
        engine.start()
        self.feed(engine, 95.0)
        self.feed(engine, 50.0)
        threading.Timer(0.2, gate.set).start()
        engine.shutdown()
        self.assertEqual([event.current_state for event in out.events], [kanary.FIRING, kanary.OK])
        threads = {name for _step, _instance, name in log}
        self.assertEqual(len(threads), 1)  # init, emit and terminate all on one thread
        self.assertEqual(log[-1][0], "terminate")

    def test_reload_hands_queued_events_to_the_new_instance(self):
        gate, entered, log = threading.Event(), threading.Event(), []
        old_class = recording_output("out", gate=gate, entered=entered, log=log)
        engine = self.make_engine({"out": old_class})
        self.open_gate_on_cleanup(gate)
        self.feed(engine, 95.0)  # being sent by the old instance
        self.assertTrue(entered.wait(2))
        self.feed(engine, 50.0)  # queued
        new_class = recording_output("out", log=log)
        reloading = threading.Thread(target=engine.reload_output_plugins, kwargs={"replacements": {"out": new_class}})
        reloading.start()
        time.sleep(0.1)
        gate.set()
        reloading.join(5)
        self.assertTrue(engine.flush_outputs())
        self.assertEqual([event.current_state for event in old_class.events], [kanary.FIRING])
        self.assertEqual([event.current_state for event in new_class.events], [kanary.OK])
        steps = [(step, type(instance) is new_class) for step, instance, _name in log]
        self.assertEqual(steps, [("init", False), ("emit", False), ("terminate", False), ("init", True), ("emit", True)])

    def test_a_failing_output_gets_one_attempt_per_event_until_it_recovers(self):
        attempts = []

        class Down(kanary.Output):
            output_id = "down"

            def emit(self, event):
                attempts.append(event.current_state)
                raise RuntimeError("destination is down")

        with mock.patch.object(engine_module, "_retry_delay", lambda _attempt: 0.0):
            engine = self.make_engine({"down": Down})
            self.feed(engine, 95.0)
            self.assertTrue(engine.flush_outputs())
            self.assertEqual(len(attempts), 3)  # emit, retry, re-init + retry
            self.assertEqual(engine.plugin_states["output:down"].state, "FAILED")
            self.feed(engine, 50.0)
            self.assertTrue(engine.flush_outputs())
            self.assertEqual(len(attempts), 4)  # already FAILED: a single attempt

    def test_test_fire_reports_an_output_that_has_not_sent_yet(self):
        gate = threading.Event()
        out = recording_output("out", gate=gate)
        engine = self.make_engine({"out": out})
        self.open_gate_on_cleanup(gate)
        with mock.patch.object(engine_module, "TEST_FIRE_WAIT_SECONDS", 0.2):
            summary = engine.test_fire("t.high", state=kanary.FIRING)
        self.assertEqual(summary["pending_outputs"], ["out"])
        gate.set()
        summary = engine.test_fire("t.high", state=kanary.FIRING)
        self.assertEqual((summary["delivered_outputs"], summary["pending_outputs"]), (["out"], []))

    def test_dispatch_is_recorded_once_every_output_has_finished(self):
        with TemporaryDirectory() as tmp:
            engine = self.make_engine(
                {"a": recording_output("a"), "b": recording_output("b", fail=True)},
                store=kanary.SQLiteStore(Path(tmp) / "state.db"),
            )
            with mock.patch.object(engine_module, "_retry_delay", lambda _attempt: 0.0):
                self.feed(engine, 95.0)
                self.assertTrue(engine.flush_outputs())
            rows = engine.get_rule_history("t.high")["output_dispatches"]
            self.assertEqual(len(rows), 1)
            self.assertEqual((rows[0]["delivered_outputs"], rows[0]["failed_outputs"]), (["a"], ["b"]))


class OutputThreadHelpersTest(DispatchTestCase):
    def test_followup_callbacks_run_on_the_output_thread(self):
        threads = []

        class Later(kanary.Output):
            output_id = "later"

            def init(self):
                self.followups = kanary.OutputFollowups()

            def terminate(self):
                self.followups.close()

            def emit(self, event):
                threads.append(("emit", threading.current_thread().name))
                self.followups.for_event(event).after(0.05, self.remind)

            def remind(self, event):
                threads.append(("remind", threading.current_thread().name))

        engine = self.make_engine({"later": Later})
        self.feed(engine, 95.0)
        self.assertTrue(wait_until(lambda: len(threads) == 2))
        (_, emit_thread), (step, remind_thread) = threads
        self.assertEqual((step, remind_thread), ("remind", emit_thread))
        self.assertTrue(emit_thread.startswith("kanary-output-"))

    def test_quiet_hours_release_held_events_on_the_output_thread(self):
        threads = []

        class Night(kanary.Output):
            output_id = "night"

            def init(self):
                # T0 (12:00 UTC) is inside the window; "now" is already past its end.
                self.quiet = kanary.QuietHours(
                    [("11:00", "12:30")],
                    timezone="UTC",
                    now_fn=lambda: T0 + timedelta(hours=1),
                )

            def terminate(self):
                self.quiet.close()

            def emit(self, event):
                threads.append(("emit", threading.current_thread().name))
                self.quiet.send(event, self.post)

            def post(self, event):
                threads.append(("post", threading.current_thread().name))

        engine = self.make_engine({"night": Night})
        self.feed(engine, 95.0)
        self.assertTrue(wait_until(lambda: len(threads) == 2))
        self.assertEqual(threads[1], ("post", threads[0][1]))


class SlowRuleTest(DispatchTestCase):
    def test_a_slow_evaluate_is_reported_until_it_is_fast_again(self):
        class Slow(kanary.Rule):
            rule_id = "slow"
            inputs = "fake:t"
            severity = kanary.WARN
            delay = 0.15

            def evaluate(self, ctx):
                time.sleep(self.delay)
                return kanary.ok()

        engine = self.make_engine({}, rules={"slow": Slow})
        with self.assertLogs("kanary.engine", level="WARNING") as logs:
            self.feed(engine, 1.0)
        self.assertTrue(any("evaluate() took" in line for line in logs.output))
        status = engine.plugin_states["rule:slow"]
        self.assertTrue((status.warning or "").startswith("evaluate() took"))
        engine.rules["slow"].delay = 0.0
        self.feed(engine, 1.0)
        self.assertIsNone(status.warning)


class TestPollTest(unittest.TestCase):
    """kanaryctl test-poll: the source's test_poll(), shown but never evaluated."""

    def make_runtime(self, sources):
        runtime = EngineRuntime(RuntimeConfig(rule_directories=[], api_port=0))
        engine = kanary.Engine(source_registry=sources, rule_registry={}, output_registry={}, now_fn=lambda: T0)
        engine.start()
        runtime.engine = engine

        def close():
            runtime._stop_source_threads(set(runtime._source_threads))
            runtime.api._server.server_close()
            engine.shutdown()

        self.addCleanup(close)
        return runtime, engine

    def test_runs_on_the_source_thread_without_evaluation_or_moving_the_next_poll(self):
        polls = []

        class Counting(kanary.Source):
            source_id = "counting"
            interval = 1.0

            def poll(self):
                polls.append(threading.current_thread().name)
                return kanary.inputs(("n", float(len(polls))))

            def test_poll(self):
                polls.append("test:" + threading.current_thread().name)
                return kanary.inputs(("n", -1.0))

        runtime, engine = self.make_runtime({"counting": Counting})
        runtime._sync_source_threads()
        self.assertTrue(wait_until(lambda: len(polls) == 1))
        time.sleep(0.5)
        result = runtime.test_poll("counting", timeout=2.0)
        self.assertEqual(result["channels"]["n"]["value"], -1.0)
        self.assertEqual(polls[1], "test:" + polls[0])  # same thread as poll()
        current = engine.source_states["counting"].current.payload
        self.assertEqual(current["channels"]["n"]["value"], 1.0)  # not evaluated
        # The second poll is still due 1 s after the first, not 1 s after the test poll.
        time.sleep(0.75)
        self.assertEqual(len(polls), 3)

    def test_a_waiting_source_keeps_its_queued_values(self):
        arrived = queue.Queue()

        class Waiting(kanary.Source):
            source_id = "waiting"
            interval = 0

            def init(self):
                self.latest = {}

            def poll(self):
                try:
                    name, value = arrived.get(timeout=0.2)
                except queue.Empty:
                    return kanary.no_update()
                self.latest[name] = value
                return kanary.inputs(*self.latest.items())

            def test_poll(self):
                return kanary.inputs(*self.latest.items()) if self.latest else kanary.no_update()

        runtime, engine = self.make_runtime({"waiting": Waiting})
        runtime._sync_source_threads()
        self.assertEqual(runtime.test_poll("waiting", timeout=2.0)["status"], "no_update")
        arrived.put(("temperature", 21.5))
        self.assertTrue(wait_until(lambda: engine.source_states["waiting"].current.payload.get("channels")))
        self.assertEqual(runtime.test_poll("waiting", timeout=2.0)["channels"]["temperature"]["value"], 21.5)

    def test_log_count_keeps_its_read_position(self):
        with TemporaryDirectory() as tmp:
            log = Path(tmp) / "app.log"
            log.write_text("", encoding="utf-8")

            class Errors(kanary.LogCountSource):
                source_id = "errors"
                path = str(log)
                patterns = {"errors": "ERROR"}

            engine = kanary.Engine(source_registry={"errors": Errors}, rule_registry={}, output_registry={})
            engine.start()
            self.addCleanup(engine.shutdown)
            with log.open("a", encoding="utf-8") as handle:
                handle.write("ERROR one\nok\nERROR two\n")
            self.assertEqual(engine.test_poll("errors")["channels"]["errors"]["value"], 2)
            self.assertEqual(engine.sources["errors"].poll().measurements[0].value, 2)  # nothing lost

    def test_what_a_test_poll_adds_to_a_buffer_is_dropped(self):
        class Pressure(kanary.Source):
            source_id = "pressure"
            reading = 1.0

            def init(self):
                self.buffer = kanary.Buffer()

            def poll(self):
                self.buffer.add(("p", self.reading))
                return kanary.inputs(("p_count", len(self.buffer["p"])), ("p_max", self.buffer["p"].max()))

        engine = kanary.Engine(source_registry={"pressure": Pressure}, rule_registry={}, output_registry={})
        engine.start()
        self.addCleanup(engine.shutdown)
        source = engine.sources["pressure"]
        source.poll()
        source.reading = 5.0
        channels = engine.test_poll("pressure")["channels"]
        # The test poll saw its own reading ...
        self.assertEqual((channels["p_count"]["value"], channels["p_max"]["value"]), (2, 5.0))
        # ... which is not kept for the next poll.
        self.assertEqual(source.buffer["p"].values, [1.0])

    def test_a_source_that_is_not_running_is_reported(self):
        runtime, _engine = self.make_runtime({"fake": FakeSource})
        with self.assertRaises(PollUnavailableError):
            runtime.test_poll("fake", timeout=1.0)  # no thread was started
        with self.assertRaises(KeyError):
            runtime.test_poll("missing", timeout=1.0)


if __name__ == "__main__":
    unittest.main()
