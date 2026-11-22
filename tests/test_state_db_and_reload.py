"""A state database that cannot be written, a restart that brings each rule's own judgment
back, and a reload during which evaluations wait."""

from datetime import datetime, timedelta, timezone
import logging
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import textwrap
import threading
import time
import unittest

import kanary
from kanary.runtime import EngineRuntime, RuntimeConfig
from kanary.store import build_store

T0 = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)


def minutes(n):
    return T0 + timedelta(minutes=n)


def source(source_id):
    return type(source_id, (kanary.Source,), {"source_id": source_id, "interval": 60.0, "poll": lambda self: kanary.no_update()})


class Recorder(kanary.Output):
    output_id = "chat"

    def __init__(self):
        self.sent = []

    def emit(self, event):
        self.sent.append((event.rule_id, event.current_state.value))


def link_rule():
    return type("Link", (kanary.ExpectedValueRule,), {"rule_id": "link", "inputs": "net:up", "expected": 1, "tags": ["t"]})


def hv_rule(**attrs):
    return type("HV", (kanary.RangeRule,), {"rule_id": "hv", "inputs": "daq:hv", "high": 1, "tags": ["t"], **attrs})


class EngineCase(unittest.TestCase):
    def setUp(self):
        self.now = T0
        self.db = Path(tempfile.mkdtemp()) / "state.db"

    def start(self, rules):
        engine = kanary.Engine(
            source_registry={"net": source("net"), "daq": source("daq")},
            rule_registry={rule.rule_id: rule for rule in rules},
            output_registry={"chat": Recorder},
            store=build_store(self.db),
            now_fn=lambda: self.now,
        )
        engine.start()
        self.addCleanup(engine.shutdown)
        return engine

    def give(self, engine, source_id, name, value, at):
        self.now = at
        engine.evaluate_source(source_id, kanary.inputs((name, value, at)), now=at)

    def sent(self, engine):
        engine.flush_outputs(5)
        return engine.outputs["chat"].sent


class LockedStateDatabaseTest(EngineCase):
    def lock(self, engine):
        engine.store._conn.execute("PRAGMA busy_timeout = 50")
        other = sqlite3.connect(self.db, isolation_level=None)
        self.addCleanup(other.close)
        other.execute("BEGIN IMMEDIATE")  # another process holds the write lock (a backup, a sqlite3 shell)
        return other

    def wait_for(self, condition, timeout=5.0):
        deadline = time.monotonic() + timeout
        while not condition() and time.monotonic() < deadline:
            time.sleep(0.02)
        return condition()

    def test_an_alert_is_sent_at_once_and_its_history_waits_for_the_database(self):
        engine = self.start([hv_rule()])
        other = self.lock(engine)
        started = time.monotonic()
        with self.assertLogs("kanary.engine", "ERROR"):
            self.give(engine, "daq", "hv", 5, minutes(1))
            self.assertLess(time.monotonic() - started, 0.5)  # the evaluation did not wait for the lock
            self.assertEqual(self.sent(engine), [("hv", "FIRING")])
            self.assertTrue(self.wait_for(lambda: engine.state_db_error is not None))
        self.assertIn("waits for the state database", engine.state_db_error)
        self.assertEqual(engine._plugin_status("rule", "hv").state, "READY")

        self.give(engine, "daq", "hv", 0, minutes(2))
        self.assertEqual(self.sent(engine), [("hv", "FIRING"), ("hv", "OK")])
        other.execute("ROLLBACK")
        self.assertTrue(engine.flush_history(10))
        self.assertIsNone(engine.state_db_error)  # written again
        events = engine.get_rule_history("hv")["alert_events"]
        self.assertEqual([event["current_state"] for event in events], ["OK", "FIRING"])  # nothing lost

    def test_an_ack_holds_while_the_database_does_not_take_writes(self):
        engine = self.start([hv_rule()])
        self.give(engine, "daq", "hv", 5, minutes(1))
        engine.flush_history()
        other = self.lock(engine)
        with self.assertLogs("kanary.engine", "ERROR"):
            engine.create_silence(operator="op", start_at=minutes(1), end_at=minutes(2), rule_patterns=["none"])
            self.assertTrue(self.wait_for(lambda: engine.state_db_error is not None))
        warnings = []
        alert = engine.acknowledge("hv", operator="alice", warnings=warnings)
        self.assertEqual((alert.state, alert.acked_by), (kanary.ACKED, "alice"))
        self.assertTrue(any("does not take writes now" in warning for warning in warnings))
        other.execute("ROLLBACK")
        self.assertTrue(engine.flush_history(10))
        actions = engine.get_rule_history("hv")["operator_actions"]
        self.assertIn("ack", [action["action_type"] for action in actions])

    def test_the_history_is_read_at_once_while_the_database_refuses_writes(self):
        engine = self.start([hv_rule()])
        self.give(engine, "daq", "hv", 0, minutes(1))
        engine.flush_history()
        other = self.lock(engine)
        with self.assertLogs("kanary.engine", "ERROR"):
            self.give(engine, "daq", "hv", 5, minutes(2))
            self.assertTrue(self.wait_for(lambda: engine.state_db_error is not None))
        started = time.monotonic()
        events = engine.get_rule_history("hv")["alert_events"]
        engine.get_alert_history(limit=10)
        self.assertLess(time.monotonic() - started, 0.5)  # no wait for the writes that wait
        self.assertEqual([event["current_state"] for event in events], ["OK"])  # what is in it now
        other.execute("ROLLBACK")
        self.assertTrue(engine.flush_history(10))

    def test_a_restart_after_the_database_came_back_does_not_fire_again(self):
        engine = self.start([hv_rule()])
        self.give(engine, "daq", "hv", 0, minutes(1))
        other = self.lock(engine)
        with self.assertLogs("kanary.engine", "ERROR"):
            self.give(engine, "daq", "hv", 5, minutes(2))  # OK -> FIRING while locked
            self.assertTrue(self.wait_for(lambda: engine.state_db_error is not None))
        other.execute("ROLLBACK")
        self.assertTrue(engine.flush_history(10))
        engine.shutdown()
        self.now = minutes(3)
        engine = self.start([hv_rule()])
        self.give(engine, "daq", "hv", 6, minutes(3))  # still FIRING
        self.assertEqual(self.sent(engine), [])  # no second FIRING


class HistoryWriterTest(EngineCase):
    def test_a_write_that_cannot_work_does_not_hold_up_the_others(self):
        engine = self.start([hv_rule()])

        def broken(*args, **kwargs):
            raise sqlite3.OperationalError("no such table: somewhere")  # waiting does not help

        with self.assertLogs("kanary.engine", "ERROR"):
            engine._record("a broken write", broken)
            self.give(engine, "daq", "hv", 5, minutes(1))
            self.assertTrue(engine.flush_history(5))
        self.assertEqual([event["current_state"] for event in engine.get_rule_history("hv")["alert_events"]], ["FIRING"])

    def test_an_engine_whose_plugins_cannot_be_created_leaves_no_writer(self):
        def writers():
            return sum(1 for thread in threading.enumerate() if thread.name == "kanary-state-db")

        broken = type("Broken", (kanary.Source,), {"source_id": "daq", "interval": 60.0, "poll": lambda self: {}})
        broken.__init__ = lambda self: (_ for _ in ()).throw(RuntimeError("cannot be created"))
        before = writers()
        with self.assertRaises(RuntimeError):
            kanary.Engine(source_registry={"daq": broken}, rule_registry={}, output_registry={}, store=build_store(self.db))
        self.assertEqual(writers(), before)


class RestartWithJudgmentsTest(EngineCase):
    def silenced_parent_and_held_child(self):
        engine = self.start([link_rule(), hv_rule(suppressed_by="link")])
        self.give(engine, "net", "up", 0, minutes(1))
        self.give(engine, "daq", "hv", 5, minutes(1))
        self.now = minutes(2)
        silence = engine.create_silence(operator="op", start_at=minutes(2), end_at=minutes(10), rule_patterns=["link"])
        self.assertEqual((engine.alerts["link"].state, engine.alerts["hv"].state), (kanary.SILENCED, kanary.SUPPRESSED))
        engine.shutdown()
        return silence

    def test_a_silenced_parent_still_holds_its_child_after_a_restart(self):
        self.silenced_parent_and_held_child()
        self.now = minutes(3)
        engine = self.start([link_rule(), hv_rule(suppressed_by="link")])
        self.give(engine, "daq", "hv", 5, minutes(3))  # the child's value comes before the parent's
        self.assertEqual(engine.alerts["hv"].state, kanary.SUPPRESSED)
        self.assertEqual(self.sent(engine), [])

    def test_a_silence_that_ended_while_stopped_shows_the_alert_at_the_start(self):
        self.silenced_parent_and_held_child()
        self.now = minutes(11)  # the silence ended at 10
        engine = self.start([link_rule(), hv_rule(suppressed_by="link")])
        self.assertEqual(engine.alerts["link"].state, kanary.FIRING)
        self.assertEqual(engine.alerts["hv"].state, kanary.SUPPRESSED)
        self.assertIn(("link", "FIRING"), self.sent(engine))

    def test_a_parent_that_recovered_under_its_silence_is_ok_when_the_silence_ends(self):
        engine = self.start([link_rule()])
        self.give(engine, "net", "up", 0, minutes(1))
        self.now = minutes(2)
        engine.create_silence(operator="op", start_at=minutes(2), end_at=minutes(10), rule_patterns=["link"])
        self.give(engine, "net", "up", 1, minutes(3))  # recovered: still shown SILENCED
        self.assertEqual(engine.alerts["link"].state, kanary.SILENCED)
        engine.shutdown()
        self.now = minutes(11)
        engine = self.start([link_rule()])
        self.assertEqual(engine.alerts["link"].state, kanary.OK)
        self.assertEqual(self.sent(engine), [("link", "OK")])  # closes the FIRING the output was sent

    def test_a_silence_that_began_while_stopped_applies_at_the_start(self):
        engine = self.start([hv_rule()])
        self.give(engine, "daq", "hv", 5, minutes(1))
        self.now = minutes(2)
        engine.create_silence(operator="op", start_at=minutes(5), end_at=minutes(30), rule_patterns=["hv"])
        engine.shutdown()
        self.now = minutes(6)
        engine = self.start([hv_rule()])
        self.assertEqual(engine.alerts["hv"].state, kanary.SILENCED)

    def test_a_removed_rule_forgets_its_judgment(self):
        engine = self.start([hv_rule()])
        self.give(engine, "daq", "hv", 5, minutes(1))
        engine.flush_history()
        self.assertIn("hv", engine.store.load_runtime_state().judgments)
        engine.reload_rule_plugins(replacements={}, removed_rule_ids={"hv"})
        engine.flush_history()
        self.assertNotIn("hv", engine.store.load_runtime_state().judgments)

    def test_a_judgment_is_written_when_it_changes_only(self):
        engine = self.start([hv_rule()])
        writes = []
        original = engine.store.record_judgment
        engine.store.record_judgment = lambda rule_id, judgment: (writes.append(judgment.state.value), original(rule_id, judgment))
        for at, value in enumerate((5, 6, 7, 0, 0), start=1):
            self.give(engine, "daq", "hv", value, minutes(at))
        engine.flush_history()
        self.assertEqual(writes, ["FIRING", "OK"])


class ReplacedSourceTest(unittest.TestCase):
    def test_a_value_read_by_a_replaced_instance_is_not_judged(self):
        engine = kanary.Engine(source_registry={"daq": source("daq")}, rule_registry={"hv": hv_rule()}, output_registry={})
        engine.start()
        self.addCleanup(engine.shutdown)
        old = engine.sources["daq"]
        engine.reload_source_plugins(replacements={"daq": source("daq")})
        new = engine.sources["daq"]
        engine.evaluate_source("daq", {"hv": 0}, now=minutes(1), source=new)
        engine.evaluate_source("daq", {"hv": 5}, now=minutes(2), source=old)  # the stopped thread, late
        self.assertEqual(engine.alerts["hv"].state, kanary.OK)
        self.assertEqual(engine.source_states["daq"].current.payload["inputs"]["hv"]["value"], 0)


class DeliveredNoticesTest(EngineCase):
    def test_an_alert_that_failed_to_send_is_not_closed_after_a_restart(self):
        class Broken(Recorder):
            output_id = "broken"
            max_retry = 0
            max_reinit = 0

            def emit(self, event):
                raise ConnectionError("down")

        def start():
            engine = kanary.Engine(
                source_registry={"daq": source("daq")}, rule_registry={"hv": hv_rule()},
                output_registry={"chat": Recorder, "broken": Broken}, store=build_store(self.db), now_fn=lambda: self.now,
            )
            engine.start()
            self.addCleanup(engine.shutdown)
            return engine

        engine = start()
        with self.assertLogs("kanary.engine", "ERROR"):
            self.give(engine, "daq", "hv", 5, minutes(1))
            engine.flush_outputs(5)
        engine.shutdown()
        self.now = minutes(2)
        engine = start()
        engine.outputs["broken"].emit = lambda event: engine.outputs["broken"].sent.append((event.rule_id, event.current_state.value))
        self.give(engine, "daq", "hv", 0, minutes(2))
        engine.flush_outputs(5)
        self.assertEqual(engine.outputs["chat"].sent, [("hv", "OK")])  # it was told FIRING: its OK
        self.assertEqual(engine.outputs["broken"].sent, [])  # never told: nothing to close


class ReloadBoundaryTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self._tick = 1_700_000_000

    def write(self, name, text):
        (self.root / name).write_text(textwrap.dedent(text))
        self._tick += 2
        os.utime(self.root / name, (self._tick, self._tick))

    def source_file(self, value):
        self.write("s.py", f'''
            import kanary

            @kanary.source(source_id="s", interval=0.05)
            class S:
                def poll(self):
                    return {{"v": {value}}}

            @kanary.rule(rule_id="s.v", inputs="s:v", tags=["t"])
            class R(kanary.RangeRule):
                high = 25
        ''')

    def test_a_changed_source_and_a_new_output_in_one_reload(self):
        logging.disable(logging.INFO)
        self.addCleanup(logging.disable, logging.NOTSET)
        self.source_file(0)
        runtime = EngineRuntime(RuntimeConfig(rule_directories=[self.root], api_port=0))
        runtime.start()
        self.addCleanup(runtime.stop)
        time.sleep(0.3)
        self.source_file(50)
        self.write("o.py", '''
            import kanary
            SENT = []

            @kanary.output(output_id="pager")
            class Pager:
                def emit(self, event):
                    SENT.append(event.current_state.value)
        ''')
        runtime.reload_now_if_changed()
        install = runtime.engine.reload_output_plugins

        def late(*args, **kwargs):  # the new source's first poll comes before the outputs
            time.sleep(0.3)
            return install(*args, **kwargs)

        runtime.engine.reload_output_plugins = late
        summary = runtime.reload_now({"dirty": True})
        self.assertEqual(summary["status"], "reloaded")
        deadline = time.monotonic() + 5
        module = next(m for n, m in reversed(list(sys.modules.items())) if n.startswith("_kanary_rules_") and hasattr(m, "SENT"))
        while not module.SENT and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(module.SENT, ["FIRING"])

    def test_evaluations_wait_while_held_and_a_stopped_source_gives_up(self):
        engine = kanary.Engine(source_registry={"daq": source("daq")}, rule_registry={"hv": hv_rule()}, output_registry={})
        engine.start()
        self.addCleanup(engine.shutdown)
        judged = threading.Event()
        gave_up = threading.Event()
        stopped = threading.Event()

        def stopped_source():
            engine.evaluate_source("daq", {"hv": 7}, now=minutes(2), cancelled=stopped.is_set)
            gave_up.set()

        with engine.holding_evaluations():
            engine.evaluate_source("daq", {"hv": 5}, now=T0)  # the holder itself does not wait
            waiting = threading.Thread(target=lambda: (engine.evaluate_source("daq", {"hv": 0}, now=minutes(1)), judged.set()))
            waiting.start()
            cancelled = threading.Thread(target=stopped_source)
            cancelled.start()
            self.assertFalse(judged.wait(0.2))
            stopped.set()  # the runtime stopped that source (a reload replaced it)
            self.assertTrue(gave_up.wait(2))  # while the hold is still on
        self.assertTrue(judged.wait(2))
        self.assertEqual(engine.alerts["hv"].state, kanary.OK)  # the waiting value, judged after the hold
        self.assertEqual(engine.source_states["daq"].current.payload["inputs"]["hv"]["value"], 0)  # not 7


if __name__ == "__main__":
    unittest.main()
