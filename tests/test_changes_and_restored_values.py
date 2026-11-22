"""What reload --dirty counts as a change (the rest of the file, helpers of helpers, base
classes), helper packages with relative imports, plugins removed by reload --full, the
values a restored alert comes back with, and a cron run on the day the clock goes forward."""

from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import tempfile
import textwrap
import time
import unittest
from zoneinfo import ZoneInfo

import kanary
from kanary.loader import RuleDirectoryLoader
from kanary.registry import get_output_registry, get_rule_registry, get_source_registry, replace_registries
from kanary.runtime import EngineRuntime, RuntimeConfig
from kanary.schedule import parse_schedule
from kanary.store import build_store

T0 = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)
_REGISTRIES = {}


def setUpModule():
    # Loading a plugin directory replaces the registries; other test modules use theirs.
    _REGISTRIES.update(rules=get_rule_registry(), sources=get_source_registry(), outputs=get_output_registry())


def tearDownModule():
    replace_registries(**_REGISTRIES)


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text))
    later = time.time() + 5 + len(path.read_text())  # a new mtime, even within one second
    os.utime(path, (later, later))


class RuntimeCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.plugins = self.root / "plugins"
        self.plugins.mkdir()

    def start(self, **config):
        runtime = EngineRuntime(RuntimeConfig(rule_directories=[self.plugins], api_port=0, **config))
        runtime.start()
        self.addCleanup(runtime.stop)
        return runtime

    def scan(self, runtime):
        runtime.reload_now_if_changed()
        return {plugin_id: status for (kind, plugin_id), status in
                ((key.split(":", 1), value) for key, value in runtime.engine.plugin_states.items()) if kind == "rule"}


class DirtyReloadTest(RuntimeCase):
    def test_a_constant_at_the_top_of_the_file_is_a_change(self):
        write(self.plugins / "p.py", '''
            import kanary
            LIMIT = 10

            @kanary.rule(rule_id="r", inputs="s:v", tags=["t"])
            class R(kanary.RangeRule):
                high = LIMIT
        ''')
        runtime = self.start()
        write(self.plugins / "p.py", (self.plugins / "p.py").read_text().replace("LIMIT = 10", "LIMIT = 20"))
        self.assertEqual(self.scan(runtime)["r"].dirty_reason, "definition_changed")
        self.assertEqual(runtime.reload_now({"dirty": True})["status"], "reloaded")
        self.assertEqual(runtime.engine.rules["r"].high, 20)

    def test_a_helper_of_a_helper_is_a_dependency(self):
        write(self.plugins / "b.py", "LIMIT = 10\n")
        write(self.plugins / "a.py", "from b import LIMIT\n")
        write(self.plugins / "p.py", '''
            import kanary
            from a import LIMIT

            @kanary.rule(rule_id="r", inputs="s:v", tags=["t"])
            class R(kanary.RangeRule):
                high = LIMIT
        ''')
        runtime = self.start()
        write(self.plugins / "b.py", "LIMIT = 20\n")
        self.assertEqual(self.scan(runtime)["r"].dirty_reason, "dependency_changed")
        runtime.reload_now({"dirty": True})
        self.assertEqual(runtime.engine.rules["r"].high, 20)

    def test_an_edit_to_one_plugin_class_changes_only_it_and_what_extends_it(self):
        text = '''
            import kanary

            class Base(kanary.RangeRule):
                high = 10

            @kanary.rule(rule_id="extends", inputs="s:v", tags=["t"])
            class Extends(Base):
                pass

            @kanary.rule(rule_id="one", inputs="s:v", tags=["t"])
            class One(kanary.RangeRule):
                high = 1

            @kanary.rule(rule_id="other", inputs="s:v", tags=["t"])
            class Other(kanary.RangeRule):
                high = 2

            @kanary.rule(rule_id="child", inputs="s:v", tags=["t"])
            class Child(One):
                pass
        '''
        write(self.plugins / "p.py", text)
        runtime = self.start()
        write(self.plugins / "p.py", text.replace("high = 1\n", "high = 3\n"))
        dirty = {rule_id for rule_id, status in self.scan(runtime).items() if status.state == "DIRTY"}
        self.assertEqual(dirty, {"one", "child"})
        runtime.reload_now({"dirty": True})
        write(self.plugins / "p.py", text.replace("high = 1\n", "high = 3\n").replace("high = 10", "high = 11"))
        dirty = {rule_id for rule_id, status in self.scan(runtime).items() if status.state == "DIRTY"}
        self.assertEqual(dirty, {"extends", "one", "other", "child"})  # Base is not a plugin: shared by the file

    def test_a_helper_package_with_relative_imports(self):
        write(self.plugins / "pkg" / "__init__.py", "")
        write(self.plugins / "pkg" / "limit_values.py", "LIMIT = 10\n")
        write(self.plugins / "pkg" / "limits.py", "from .limit_values import LIMIT\n")
        write(self.plugins / "pkg" / "rules.py", '''
            import kanary
            from . import limits

            @kanary.rule(rule_id="in.pkg", inputs="s:v", tags=["t"])
            class InPackage(kanary.RangeRule):
                high = limits.LIMIT
        ''')
        write(self.plugins / "p.py", '''
            import kanary
            from pkg.limits import LIMIT
            import pkg.rules  # also a plugin file: loaded once all the same

            @kanary.rule(rule_id="r", inputs="s:v", tags=["t"])
            class R(kanary.RangeRule):
                high = LIMIT
        ''')
        self.assertEqual(RuleDirectoryLoader(self.plugins).load().rules["in.pkg"].high, 10)
        runtime = self.start()
        write(self.plugins / "pkg" / "limit_values.py", "LIMIT = 20\n")
        self.assertEqual({rule_id for rule_id, status in self.scan(runtime).items() if status.state == "DIRTY"}, {"r", "in.pkg"})
        runtime.reload_now({"dirty": True})
        self.assertEqual((runtime.engine.rules["r"].high, runtime.engine.rules["in.pkg"].high), (20, 20))


class FullRestartRemovalTest(RuntimeCase):
    def write_plugins(self, *, rule_high=None, value=50):
        rule = f'''
            @kanary.rule(rule_id="r", inputs="s:v", tags=["t"])
            class R(kanary.RangeRule):
                high = {rule_high}
        ''' if rule_high is not None else ""
        write(self.plugins / "p.py", f'''
            import kanary

            @kanary.source(source_id="s", interval=0.05)
            class S:
                def poll(self):
                    return {{"v": {value}}}
        ''' + rule)

    def wait_for(self, condition):
        deadline = time.monotonic() + 5
        while not condition():
            self.assertLess(time.monotonic(), deadline, "timed out")
            time.sleep(0.02)

    def test_a_rule_removed_by_a_full_restart_loses_its_ack(self):
        self.write_plugins(rule_high=25)
        runtime = self.start(state_db_path=self.root / "state.db")
        self.wait_for(lambda: getattr(runtime.engine.alerts.get("r"), "state", None) == kanary.FIRING)
        runtime.engine.acknowledge("r", operator="alice")
        self.write_plugins()
        summary = runtime.reload_now({"full": True})
        self.assertEqual((summary["status"], summary["rules"]["removed"]), ("reloaded", ["r"]))
        self.write_plugins(rule_high=30)
        self.assertEqual(runtime.reload_now({"full": True})["status"], "reloaded")
        self.wait_for(lambda: not getattr(runtime.engine.alerts.get("r"), "restored", True))
        self.assertEqual(runtime.engine.alerts["r"].state, kanary.FIRING)
        self.assertIsNone(runtime.engine.alerts["r"].acked_by)


SENT = []


def silenced_engine(db, now):
    source = type("daq", (kanary.Source,), {"source_id": "daq", "interval": 60.0, "poll": lambda self: kanary.no_update()})
    rule = type("HV", (kanary.RangeRule,), {"rule_id": "hv", "inputs": "daq:hv", "high": 1, "tags": ["t"]})
    output = type("Out", (kanary.Output,), {"output_id": "out", "emit": lambda self, event: SENT.append(event)})
    engine = kanary.Engine(source_registry={"daq": source}, rule_registry={"hv": rule}, output_registry={"out": output},
                           store=build_store(db), now_fn=lambda: now[0])
    engine.start()
    return engine


class RestoredValuesTest(unittest.TestCase):
    def setUp(self):
        SENT.clear()
        self.db = Path(tempfile.mkdtemp()) / "state.db"
        self.now = [T0]

    def run_silenced(self, end_at):
        engine = silenced_engine(self.db, self.now)
        engine.create_silence(operator="op", start_at=T0, end_at=end_at, rule_patterns=["hv"])
        for minute, value in ((1, 3), (2, 100)):  # worse under the silence: the state stays
            self.now[0] = T0 + timedelta(minutes=minute)
            engine.evaluate_source("daq", kanary.inputs(("hv", value, self.now[0])), now=self.now[0])
        engine.flush_outputs(5)
        engine.shutdown()

    def test_an_alert_whose_silence_ended_while_stopped_has_its_last_values(self):
        self.run_silenced(end_at=T0 + timedelta(minutes=30))
        self.now[0] = T0 + timedelta(hours=1)
        engine = silenced_engine(self.db, self.now)
        engine.flush_outputs(5)
        engine.shutdown()
        [event] = [event for event in SENT if event.current_state == kanary.FIRING]
        self.assertEqual(event.payload["values"], {"daq:hv": 100})
        self.assertTrue(event.alert.restored)
        fields = dict(kanary.describe(event).fields)
        self.assertIn("before the restart", fields["Evaluated"])

    def test_an_alert_still_silenced_shows_its_last_values(self):
        self.run_silenced(end_at=T0 + timedelta(hours=2))
        self.now[0] = T0 + timedelta(hours=1)
        engine = silenced_engine(self.db, self.now)
        self.addCleanup(engine.shutdown)
        self.assertEqual(engine.alerts["hv"].state, kanary.SILENCED)
        self.assertEqual(engine.alerts["hv"].payload["values"], {"daq:hv": 100})

    def test_a_notice_of_an_evaluated_alert_says_nothing_of_a_restart(self):
        engine = silenced_engine(self.db, self.now)
        self.addCleanup(engine.shutdown)
        engine.evaluate_source("daq", kanary.inputs(("hv", 5, T0)), now=T0)
        engine.flush_outputs(5)
        self.assertNotIn("Evaluated", dict(kanary.describe(SENT[-1]).fields))


class SkippedHourTest(unittest.TestCase):
    NEW_YORK = ZoneInfo("America/New_York")

    def runs(self, expression, hour, minute, count=3):
        schedule = parse_schedule(f"CRON_TZ=America/New_York {expression}")
        base = datetime(2026, 3, 8, hour, minute, tzinfo=self.NEW_YORK)  # the clock goes 02:00 -> 03:00
        runs = []
        for _ in range(count):
            base = schedule.next_after(base)
            runs.append(base.astimezone(self.NEW_YORK).strftime("%d %H:%M"))
        return runs

    def test_a_start_within_the_hour_after_the_change_keeps_the_days_run(self):
        self.assertEqual(self.runs("30 2 * * *", 3, 10), ["08 03:30", "09 02:30", "10 02:30"])
        self.assertEqual(self.runs("30 2 * * *", 3, 40), ["09 02:30", "10 02:30", "11 02:30"])

    def test_only_the_first_skipped_time_runs(self):
        self.assertEqual(self.runs("*/15 2 * * *", 1, 50, count=2), ["08 03:00", "09 02:00"])
        self.assertEqual(self.runs("*/15 2 * * *", 3, 10, count=1), ["09 02:00"])  # 03:00 has passed
        self.assertEqual(self.runs("0,45 2,3 * * *", 1, 0, count=3), ["08 03:00", "08 03:45", "09 02:00"])


if __name__ == "__main__":
    unittest.main()
