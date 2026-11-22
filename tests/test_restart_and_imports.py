"""What outputs were told survives a restart; shared helper modules are read again on a
reload; test-evaluate cannot change the live state; cron schedules far ahead and around
daylight saving time."""

from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest import mock
from zoneinfo import ZoneInfo

import kanary
from kanary.loader import RuleDirectoryLoader
from kanary.runtime import EngineRuntime, RuntimeConfig
from kanary.schedule import parse_schedule
from kanary.store import build_store

T0 = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)


def source(source_id="daq"):
    return type(source_id, (kanary.Source,), {"source_id": source_id, "interval": 60.0, "poll": lambda self: kanary.no_update()})


def threshold_rule():
    return type("T", (kanary.ThresholdRule,), {
        "rule_id": "hv", "inputs": "daq:hv", "tags": ["t"], "thresholds": [(50, kanary.WARN), (90, kanary.CRITICAL)],
    })


SENT: list[tuple[str, str]] = []


def pager(minimum):
    return type("Pager", (kanary.Output,), {
        "output_id": f"pager.{minimum.name.lower()}", "minimum_severity": minimum,
        "emit": lambda self, event: SENT.append((self.output_id, event.current_state.value)),
    })


class RestartedNoticesTest(unittest.TestCase):
    def setUp(self):
        SENT.clear()
        self.db = Path(tempfile.mkdtemp()) / "state.db"

    def run_engine(self, values):
        engine = kanary.Engine(
            source_registry={"daq": source()},
            rule_registry={"hv": threshold_rule()},
            output_registry={"pager.error": pager(kanary.ERROR), "pager.critical": pager(kanary.CRITICAL)},
            store=build_store(self.db),
        )
        engine.start()
        for minute, value in values:
            engine.evaluate_source("daq", {"hv": value}, now=T0 + timedelta(minutes=minute))
        engine.flush_outputs(5)
        engine.shutdown()

    def test_the_ok_after_a_restart_has_the_severity_each_output_was_sent(self):
        self.run_engine([(1, 95), (2, 60)])  # CRITICAL, then down to WARN
        self.run_engine([(3, 0)])
        self.assertEqual(SENT.count(("pager.error", "OK")), 1)
        self.assertEqual(SENT.count(("pager.critical", "OK")), 1)

    def test_an_output_never_sent_the_alert_gets_no_ok_after_a_restart(self):
        self.run_engine([(1, 60)])  # WARN only: neither pager takes it
        self.run_engine([(2, 0)])
        self.assertEqual(SENT, [])

    def test_a_database_from_before_counts_open_alerts_as_sent_and_keeps_that(self):
        self.run_engine([(1, 95)])
        connection = sqlite3.connect(self.db)
        connection.execute("DROP TABLE output_notices")  # as a database of an earlier KANARY
        connection.commit()
        connection.close()
        self.run_engine([])  # the first start after the upgrade: nothing happens
        self.run_engine([(2, 0)])
        self.assertIn(("pager.error", "OK"), SENT)


class SharedHelperTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())

    def write_helper(self, value):
        path = self.root / "shared_value.py"
        path.write_text(f"VALUE = {value}\n")
        os.utime(path, ns=(1_700_000_000_000_000_000, 1_700_000_000_000_000_000))  # same second, same size

    def test_an_edited_helper_reaches_the_plugins_on_the_next_load(self):
        self.write_helper(10)
        (self.root / "rule.py").write_text(textwrap.dedent('''
            import kanary
            from shared_value import VALUE

            @kanary.rule(rule_id="shared.r", inputs="s:v", tags=["t"])
            class R(kanary.RangeRule):
                high = VALUE
        '''))
        loader = RuleDirectoryLoader(self.root)
        with mock.patch.object(sys, "dont_write_bytecode", False):
            self.assertEqual(loader.load().rules["shared.r"].high, 10)
            self.write_helper(20)
            self.assertEqual(loader.load().rules["shared.r"].high, 20)
        self.assertIn(str(self.root.resolve()), sys.path)

    def test_the_watch_goes_on_when_a_file_goes_away_during_the_scan(self):
        (self.root / "p.py").write_text("import kanary\n")
        (self.root / "gone.py").write_text("import kanary\n")
        runtime = EngineRuntime(RuntimeConfig(rule_directories=[self.root], api_port=0, reload_interval=0.05))
        runtime.start()
        self.addCleanup(runtime.stop)
        listed = runtime.loader._iter_rule_files

        def racing():
            files = listed()
            (self.root / "gone.py").unlink(missing_ok=True)
            return files

        with mock.patch.object(runtime.loader, "_iter_rule_files", racing):
            time.sleep(0.3)
        self.assertTrue(runtime._reload_thread.is_alive())
        (self.root / "new.py").write_text("import kanary\n")
        deadline = time.monotonic() + 5
        while str(self.root / "new.py") not in dict(runtime._signature) and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertIn(str(self.root / "new.py"), dict(runtime._signature))  # changes are still seen


class TestEvaluateIsolationTest(unittest.TestCase):
    def test_the_previous_alert_and_the_snapshots_are_copies(self):
        testing = []

        class Meddle(kanary.Rule):
            rule_id = "hv"
            inputs = ["daq:hv", "other:x"]
            tags = ["t"]

            def evaluate(self, ctx):
                if testing:
                    ctx.previous_alert.message = "changed by a test"
                    ctx.previous_alert.payload["values"] = "changed"
                    ctx.source_payload("other")["inputs"]["x"]["value"] = 999
                return kanary.firing("fires")

        engine = kanary.Engine(source_registry={"daq": source(), "other": source("other")}, rule_registry={"hv": Meddle}, output_registry={})
        engine.start()
        self.addCleanup(engine.shutdown)
        engine.evaluate_source("other", {"x": 1}, now=T0)
        engine.evaluate_source("daq", {"hv": 5}, now=T0)
        testing.append(True)
        engine.test_evaluate("hv", {"inputs": {"daq:hv": {"value": 5}}}, now=T0)
        self.assertEqual(engine.alerts["hv"].message, "fires")
        self.assertNotEqual(engine.alerts["hv"].payload.get("values"), "changed")
        self.assertEqual(engine.source_states["other"].current.payload["inputs"]["x"]["value"], 1)


class CronHorizonAndDaylightSavingTest(unittest.TestCase):
    def test_a_run_decades_ahead_is_found(self):
        schedule = parse_schedule("0 9 * FEB MON#5")  # a fifth Monday in February: 2044
        self.assertFalse(schedule.never)
        self.assertEqual(schedule.next_after(datetime(2026, 11, 22)), datetime(2044, 2, 29, 9, 0))
        started = time.monotonic()
        self.assertTrue(parse_schedule("0 0 30 2 *").never)
        self.assertLess(time.monotonic() - started, 0.5)

    def test_the_next_run_is_always_later_in_real_time(self):
        new_york = ZoneInfo("America/New_York")
        for expression in ("50 * * * *", "*/30 * * * *", "30 1 * * *", "30 2 * * *", "0 * * * *"):
            schedule = parse_schedule(f"CRON_TZ=America/New_York {expression}")
            for day in (datetime(2026, 11, 1, tzinfo=timezone.utc), datetime(2026, 3, 8, tzinfo=timezone.utc)):
                base = day
                for _ in range(200):  # each run after the one before, through the change of the clock
                    following = schedule.next_after(base)
                    self.assertGreater(following.astimezone(timezone.utc), base.astimezone(timezone.utc), (expression, base))
                    self.assertTrue(schedule.matches(following.astimezone(new_york)) or following.astimezone(new_york).hour == 3)
                    base = following
        once = parse_schedule("CRON_TZ=America/New_York 30 1 * * *")
        first = once.next_after(datetime(2026, 11, 1, 4, 0, tzinfo=timezone.utc))  # 00:00 EDT
        self.assertEqual(first.astimezone(timezone.utc), datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc))  # 01:30 EDT
        self.assertEqual(once.next_after(first).astimezone(new_york).date().day, 2)  # not again at 01:30 EST
        skipped = parse_schedule("CRON_TZ=America/New_York 30 2 * * *").next_after(datetime(2026, 3, 8, 5, 0, tzinfo=timezone.utc))
        self.assertEqual(skipped.astimezone(timezone.utc), datetime(2026, 3, 8, 7, 30, tzinfo=timezone.utc))  # 03:30 EDT

    def test_the_host_local_time_around_daylight_saving_time(self):
        code = textwrap.dedent('''
            import time
            from datetime import datetime, timezone
            from kanary.schedule import parse_schedule
            schedule = parse_schedule("50 * * * *")
            base = datetime(2026, 11, 1, 5, 40, tzinfo=timezone.utc).astimezone()
            for _ in range(5):
                following = schedule.next_after(base)
                assert following.astimezone(timezone.utc) > base.astimezone(timezone.utc), (base, following)
                base = following
            print("ok")
        ''')
        completed = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60,
                                   env={**os.environ, "TZ": "America/New_York"})
        self.assertEqual(completed.stdout.strip(), "ok", completed.stderr)


if __name__ == "__main__":
    unittest.main()
