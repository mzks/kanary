"""A full restart that cannot save the running engine's state, operator actions during the
switch, an output added while a source is about to evaluate, an OK whose alert never reached
an output, the history writer around the write in progress, and history reads while the
state database is locked."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import sys
import tempfile
import textwrap
import threading
import time
import types
import unittest
from unittest import mock

import kanary
import kanary.engine as engine_module
import kanary.runtime as runtime_module
from kanary.engine import EngineRestarting
from kanary.registry import get_output_registry, get_rule_registry, get_source_registry, replace_registries
from kanary.runtime import EngineRuntime, RuntimeConfig
from kanary.store import build_store

T0 = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)
_REGISTRIES = {}
# What the plugin files of a test and the test share: a module the plugins import.
SHARED = types.ModuleType("kanary_test_shared")
sys.modules[SHARED.__name__] = SHARED


def setUpModule():
    # Loading a plugin directory replaces the registries; other test modules use theirs.
    _REGISTRIES.update(rules=get_rule_registry(), sources=get_source_registry(), outputs=get_output_registry())


def tearDownModule():
    replace_registries(**_REGISTRIES)


def minutes(n):
    return T0 + timedelta(minutes=n)


def source():
    return type("daq", (kanary.Source,), {"source_id": "daq", "interval": 60.0, "poll": lambda self: kanary.no_update()})


def hv_rule():
    return type("HV", (kanary.RangeRule,), {"rule_id": "hv", "inputs": "daq:hv", "high": 1, "tags": ["t"]})


def engine_with(outputs=None, *, store=None):
    engine = kanary.Engine(
        source_registry={"daq": source()},
        rule_registry={"hv": hv_rule()},
        output_registry=outputs or {},
        store=store,
    )
    engine.start()
    return engine


class FullRestartTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.db = self.root / "state.db"
        plugins = self.root / "plugins"
        plugins.mkdir()
        (plugins / "p.py").write_text(textwrap.dedent('''
            import kanary
            SENT = []

            @kanary.source(source_id="s", interval=0.05)
            class S:
                def poll(self):
                    return {"v": 50}

            @kanary.rule(rule_id="s.v", inputs="s:v", tags=["t"])
            class V(kanary.RangeRule):
                high = 25

            @kanary.output(output_id="o")
            class O:
                def emit(self, event):
                    SENT.append(event.current_state.value)
        '''))
        self.runtime = EngineRuntime(RuntimeConfig(rule_directories=[plugins], api_port=0, state_db_path=self.db))
        self.runtime.start()
        self.addCleanup(self.runtime.stop)
        self.wait_for(lambda: self.runtime.engine.alerts.get("s.v") is not None
                      and self.runtime.engine.alerts["s.v"].state == kanary.FIRING)

    def wait_for(self, condition, timeout=5.0):
        deadline = time.monotonic() + timeout
        while not condition():
            self.assertLess(time.monotonic(), deadline, "timed out")
            time.sleep(0.02)

    def sent(self):
        return [state for name, module in list(sys.modules.items())
                if name.startswith("_kanary_rules_") and hasattr(module, "SENT")
                for state in module.SENT]

    def test_a_database_that_does_not_take_the_last_writes_keeps_the_running_engine(self):
        old = self.runtime.engine
        old.store._conn.execute("PRAGMA busy_timeout = 50")
        other = sqlite3.connect(self.db, isolation_level=None)
        self.addCleanup(other.close)
        other.execute("BEGIN IMMEDIATE")  # another process holds the write lock
        old.evaluate_source("s", {"v": 0})  # OK, then FIRING again: their history waits
        old.evaluate_source("s", {"v": 50})
        with self.assertLogs("kanary", level="WARNING"):
            with mock.patch.object(runtime_module, "HISTORY_DRAIN_SECONDS", 0.3):
                summary = self.runtime.reload_now({"full": True})
        self.assertEqual(summary["status"], "reload_failed")
        self.assertIn("state database", summary["error"])
        self.assertIs(self.runtime.engine, old)
        # It goes on: its sources poll again, and operator actions work.
        polled = old.source_states["s"].poll_count
        self.wait_for(lambda: old.source_states["s"].poll_count > polled)
        other.execute("ROLLBACK")
        old.acknowledge("s.v", operator="op")
        self.assertEqual(old.alerts["s.v"].state, kanary.ACKED)
        # Once the database takes the writes, the restart goes through and fires nothing again.
        firing = self.sent().count("FIRING")
        self.assertEqual(self.runtime.reload_now({"full": True})["status"], "reloaded")
        self.assertIsNot(self.runtime.engine, old)
        self.assertEqual(self.runtime.engine.alerts["s.v"].state, kanary.ACKED)
        time.sleep(0.3)
        self.runtime.engine.flush_outputs(5)
        self.assertEqual(self.sent().count("FIRING"), firing)

    def test_an_operator_action_during_the_switch_is_refused_and_works_after(self):
        old = self.runtime.engine
        refused = []
        original_start = kanary.Engine.start

        def start_then_act(engine, *args, **kwargs):
            original_start(engine, *args, **kwargs)
            for action in (
                lambda: old.acknowledge("s.v", operator="op"),
                lambda: old.create_silence(operator="op", rule_patterns=["s.v"], start_at=minutes(0), end_at=minutes(60)),
            ):
                try:
                    action()
                except EngineRestarting as exc:
                    refused.append(str(exc))

        with mock.patch.object(kanary.Engine, "start", start_then_act):
            self.assertEqual(self.runtime.reload_now({"full": True})["status"], "reloaded")
        self.assertEqual(len(refused), 2, refused)
        self.assertEqual(self.runtime.engine.alerts["s.v"].state, kanary.FIRING)  # not ACKED by a lost ack
        self.runtime.engine.acknowledge("s.v", operator="op")
        self.assertEqual(self.runtime.engine.alerts["s.v"].state, kanary.ACKED)


class SwitchWithBusyPluginsTest(unittest.TestCase):
    def start(self, plugin_text):
        root = Path(tempfile.mkdtemp())
        (root / "plugins").mkdir()
        (root / "plugins" / "p.py").write_text(textwrap.dedent(plugin_text))
        self.db = root / "state.db"
        runtime = EngineRuntime(RuntimeConfig(rule_directories=[root / "plugins"], api_port=0, state_db_path=self.db))
        runtime.start()
        self.addCleanup(runtime.stop)
        return runtime

    def wait_for(self, condition, timeout=5.0):
        deadline = time.monotonic() + timeout
        while not condition():
            self.assertLess(time.monotonic(), deadline, "timed out")
            time.sleep(0.02)

    def start_with_a_slow_output(self):
        SHARED.gate = threading.Event()
        SHARED.gate.set()
        SHARED.value = [0]
        SHARED.sent = []
        SHARED.fail = False
        self.addCleanup(SHARED.gate.set)
        runtime = self.start('''
            import kanary
            import kanary_test_shared as shared

            @kanary.source(source_id="s", interval=0.05)
            class S:
                def poll(self):
                    return {"v": shared.value[0]}

            @kanary.rule(rule_id="r", inputs="s:v", tags=["t"])
            class R(kanary.RangeRule):
                high = 25

            @kanary.output(output_id="o", max_retry=0, max_reinit=0)
            class O:
                def emit(self, event):
                    shared.gate.wait(30)
                    if shared.fail:
                        raise ConnectionError("the destination is down")
                    shared.sent.append(event.current_state.value)
        ''')
        self.wait_for(lambda: runtime.engine.alerts.get("r") is not None)
        SHARED.gate.clear()  # the destination is slow: the FIRING is still being sent at the switch
        SHARED.value[0] = 50
        self.wait_for(lambda: runtime.engine.alerts["r"].state == kanary.FIRING)
        return runtime

    def restart_while_it_sends(self, runtime):
        with mock.patch.object(runtime_module, "SHUTDOWN_DRAIN_SECONDS", 0.3), self.assertLogs("kanary", level="ERROR"):
            return runtime.reload_now({"full": True})

    def test_a_restart_waits_for_an_output_still_sending_and_is_given_up(self):
        runtime = self.start_with_a_slow_output()
        old = runtime.engine
        summary = self.restart_while_it_sends(runtime)
        self.assertEqual(summary["status"], "reload_failed")
        self.assertIn("output o still sending", summary["error"])
        self.assertIs(runtime.engine, old)  # it goes on, and its output sends in order
        SHARED.gate.set()
        SHARED.value[0] = 0
        self.wait_for(lambda: SHARED.sent == ["FIRING", "OK"])
        self.assertEqual(runtime.reload_now({"full": True})["status"], "reloaded")  # once it has sent
        self.assertIsNot(runtime.engine, old)

    def test_a_firing_that_fails_after_a_restart_given_up_gets_no_ok(self):
        runtime = self.start_with_a_slow_output()
        SHARED.fail = True
        self.assertEqual(self.restart_while_it_sends(runtime)["status"], "reload_failed")
        with self.assertLogs("kanary", level="WARNING"):
            SHARED.gate.set()
            runtime.engine.flush_outputs(5)
        SHARED.fail = False
        SHARED.value[0] = 0
        self.wait_for(lambda: runtime.engine.alerts["r"].state == kanary.OK)
        runtime.engine.flush_outputs(5)
        self.assertEqual(SHARED.sent, [])  # not an OK alone

    def test_a_restart_given_up_leaves_each_source_with_its_one_thread(self):
        SHARED.release = threading.Event()
        SHARED.polling = [0]
        SHARED.most = [0]
        SHARED.terminated = []
        self.addCleanup(SHARED.release.set)
        runtime = self.start('''
            import kanary
            import kanary_test_shared as shared

            @kanary.source(source_id="s", interval=0.05)
            class S:
                def poll(self):
                    shared.polling[0] += 1
                    shared.most[0] = max(shared.most[0], shared.polling[0])
                    try:
                        shared.release.wait(3)
                        return {"v": 0}
                    finally:
                        shared.polling[0] -= 1

                def terminate(self):
                    shared.terminated.append(self)
        ''')
        self.wait_for(lambda: SHARED.polling[0] == 1)  # in a poll() that takes long
        old = runtime.engine
        old.store._conn.execute("PRAGMA busy_timeout = 50")
        other = sqlite3.connect(self.db, isolation_level=None)
        self.addCleanup(other.close)
        other.execute("BEGIN IMMEDIATE")  # the state database does not take the last writes
        now = datetime.now().astimezone()
        old.create_silence(operator="op", start_at=now, end_at=now + timedelta(hours=1), rule_patterns=["nothing"])
        with mock.patch.object(runtime_module, "HISTORY_DRAIN_SECONDS", 0.3), \
                mock.patch.object(runtime_module, "SOURCE_STOP_WAIT_SECONDS", 0.2), \
                self.assertLogs("kanary", level="WARNING"):
            self.assertEqual(runtime.reload_now({"full": True})["status"], "reload_failed")
        time.sleep(0.3)
        SHARED.release.set()
        time.sleep(0.3)
        other.execute("ROLLBACK")
        self.assertIs(runtime.engine, old)
        self.assertEqual(SHARED.most[0], 1)  # never two polls of one instance at once
        self.assertEqual(SHARED.terminated, [])  # the running instance is not closed under its thread
        polled = old.source_states["s"].poll_count
        self.wait_for(lambda: old.source_states["s"].poll_count > polled)


class HistoryLimitTest(unittest.TestCase):
    def test_the_limit_keeps_the_newest_by_time(self):
        gate = threading.Event()
        self.addCleanup(gate.set)
        slow = type("Slow", (kanary.Output,), {"output_id": "slow", "exclude_states": ["ACKED"], "emit": lambda self, event: gate.wait(5)})
        fast = type("Fast", (kanary.Output,), {"output_id": "fast", "emit": lambda self, event: None})
        engine = engine_with({"slow": slow, "fast": fast}, store=build_store(Path(tempfile.mkdtemp()) / "state.db"))
        self.addCleanup(engine.shutdown)
        engine.evaluate_source("daq", {"hv": 5}, now=T0 - timedelta(days=3650))  # before the ack, whatever the clock says
        engine.acknowledge("hv", operator="op")  # its dispatch is written first: the slow output is still on the FIRING
        self.assertTrue(engine.flush_history(5))
        gate.set()
        self.assertTrue(engine.flush_outputs(5))
        self.assertTrue(engine.flush_history(5))
        every = [row["current_state"] for row in engine.get_rule_history("hv")["output_dispatches"]]
        newest = [row["current_state"] for row in engine.get_rule_history("hv", limit=1)["output_dispatches"]]
        self.assertEqual(every, ["ACKED", "FIRING"])
        self.assertEqual(newest, ["ACKED"])


class OutputAddedDuringEvaluationTest(unittest.TestCase):
    def test_a_source_past_the_gate_check_waits_for_the_reload(self):
        sent = []
        pager = type("Pager", (kanary.Output,), {"output_id": "pager", "emit": lambda self, event: sent.append(event.current_state.value)})
        engine = engine_with()
        self.addCleanup(engine.shutdown)
        passed, go = threading.Event(), threading.Event()
        original = engine._wait_for_evaluations

        def slow_after_the_check(cancelled=None):
            through = original(cancelled)
            if threading.current_thread().name == "poller":
                passed.set()
                go.wait(5)  # past the check, not evaluating yet
            return through

        engine._wait_for_evaluations = slow_after_the_check
        poller = threading.Thread(target=lambda: engine.evaluate_source("daq", {"hv": 5}, now=minutes(1)), name="poller")
        poller.start()
        self.assertTrue(passed.wait(5))
        with engine.holding_evaluations():
            go.set()
            time.sleep(0.2)  # the poller takes the engine lock first, sees the hold, and waits
            engine.reload_output_plugins(replacements={"pager": pager})
        poller.join(5)
        engine.flush_outputs(5)
        self.assertEqual(engine.alerts["hv"].state, kanary.FIRING)
        self.assertEqual(sent, ["FIRING"])


class Flaky(kanary.Output):
    output_id = "flaky"
    max_retry = 0
    max_reinit = 0

    def init(self):
        self.up = False
        self.sent = []

    def emit(self, event):
        if not self.up:
            raise ConnectionError("down")
        self.sent.append(event.current_state.value)


class OkAfterAFailedFiringTest(unittest.TestCase):
    def test_an_output_that_did_not_get_the_firing_gets_no_ok(self):
        engine = engine_with({"flaky": Flaky})
        self.addCleanup(engine.shutdown)
        with self.assertLogs("kanary", level="WARNING"):
            engine.evaluate_source("daq", {"hv": 5}, now=minutes(1))
            engine.flush_outputs(5)
        engine.outputs["flaky"].up = True
        engine.evaluate_source("daq", {"hv": 0}, now=minutes(2))
        engine.flush_outputs(5)
        self.assertEqual(engine.outputs["flaky"].sent, [])
        # The next alert reaches it, and so does its OK.
        engine.evaluate_source("daq", {"hv": 5}, now=minutes(3))
        engine.evaluate_source("daq", {"hv": 0}, now=minutes(4))
        engine.flush_outputs(5)
        self.assertEqual(engine.outputs["flaky"].sent, ["FIRING", "OK"])


class HistoryWriterRaceTest(unittest.TestCase):
    def setUp(self):
        self.engine = engine_with(store=build_store(Path(tempfile.mkdtemp()) / "state.db"))
        self.addCleanup(self.engine.shutdown)
        self.written = []
        self.started = threading.Event()
        self.release = threading.Event()
        self.addCleanup(self.release.set)

    def slow(self, name):
        self.started.set()
        self.release.wait(5)
        self.written.append(name)

    def test_over_the_limit_the_oldest_waiting_write_goes_not_the_one_in_progress(self):
        with mock.patch.object(engine_module, "MAX_PENDING_HISTORY_WRITES", 1):
            self.engine._record("A", self.slow, "A")
            self.assertTrue(self.started.wait(2))
            self.engine._record("B", self.written.append, "B")
            with self.assertLogs("kanary.engine", level="WARNING"):
                self.engine._record("C", self.written.append, "C")
            self.release.set()
            self.assertTrue(self.engine.flush_history(5))
        self.assertEqual(self.written, ["A", "C"])

    def test_a_write_still_running_at_the_shutdown_deadline(self):
        errors = []
        self.engine._record("D", self.slow, "D")
        self.assertTrue(self.started.wait(2))
        with mock.patch.object(threading, "excepthook", lambda args: errors.append(args.exc_value)):
            given_up = self.engine._history.close(0.1)
            self.release.set()
            self.engine._history._thread.join(2)
        self.assertEqual(given_up, 1)
        self.assertEqual(errors, [])
        self.assertEqual(self.written, ["D"])  # it finished by itself; nothing came after it


class HistoryReadWhileLockedTest(unittest.TestCase):
    def test_a_rule_history_is_read_without_waiting_for_the_writes(self):
        db = Path(tempfile.mkdtemp()) / "state.db"
        engine = engine_with(store=build_store(db))
        self.addCleanup(engine.shutdown)
        engine.evaluate_source("daq", {"hv": 5}, now=minutes(1))
        self.assertTrue(engine.flush_history(5))
        other = sqlite3.connect(db, isolation_level=None)
        self.addCleanup(other.close)
        other.execute("BEGIN IMMEDIATE")  # the writer now waits SQLite's busy timeout (5 s) per try
        self.addCleanup(other.execute, "ROLLBACK")
        engine.evaluate_source("daq", {"hv": 0}, now=minutes(2))
        time.sleep(0.1)  # the writer is in its first try
        started = time.monotonic()
        history = engine.get_rule_history("hv")
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertEqual([event["current_state"] for event in history["alert_events"]], ["FIRING"])  # the OK is still waiting
        started = time.monotonic()
        engine.evaluate_source("daq", {"hv": 5}, now=minutes(3))
        self.assertLess(time.monotonic() - started, 0.5)


if __name__ == "__main__":
    unittest.main()
