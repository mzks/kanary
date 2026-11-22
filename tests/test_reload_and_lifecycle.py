"""Source threads across reloads, two-phase reloads and their result, and the plugin loader."""

import contextlib
import io
import os
from pathlib import Path
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from unittest import mock

import kanary
from kanary import ctl as kanaryctl
from kanary.loader import RuleDirectoryLoader
from kanary.runtime import EngineRuntime, RuntimeConfig


def wait_until(condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.02)
    return True


def plugin_module(name_part: str):
    """The loaded plugin module that defines name_part (its globals hold what the plugins record)."""
    return next(
        module
        for name, module in reversed(list(sys.modules.items()))
        if name.startswith("_kanary_rules_") and hasattr(module, name_part)
    )


class PluginDirectoryCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self._tick = 1_700_000_000

    def write(self, name: str, text: str) -> None:
        path = self.root / name
        path.write_text(textwrap.dedent(text))
        self._tick += 2
        os.utime(path, (self._tick, self._tick))

    def start_runtime(self) -> EngineRuntime:
        runtime = EngineRuntime(RuntimeConfig(rule_directories=[self.root], api_port=0))
        runtime.start()
        self.addCleanup(runtime.stop)
        return runtime


class SourceThreadTest(PluginDirectoryCase):
    """init(), poll(), and terminate() of a source instance run on its own thread."""

    def test_a_reloaded_source_inits_polls_and_terminates_on_its_thread(self):
        self.write("db.py", '''
            import sqlite3, threading, kanary
            LIFECYCLE = []

            @kanary.source(source_id="db", interval=0.05)
            class Db:
                max_retry = 0
                max_reinit = 0

                def init(self):
                    self.conn = sqlite3.connect(":memory:")  # usable only on this thread
                    LIFECYCLE.append(("init", id(self), threading.get_ident()))

                def poll(self):
                    return {"v": self.conn.execute("select 1").fetchone()[0]}

                def terminate(self):
                    self.conn.close()
                    LIFECYCLE.append(("terminate", id(self), threading.get_ident()))

            @kanary.rule(rule_id="db.v", inputs="db:v")
            class V(kanary.RangeRule):
                high = 5
        ''')
        runtime = self.start_runtime()
        status = runtime.engine._plugin_status("source", "db")
        self.assertTrue(wait_until(lambda: status.run_count >= 2))

        summary = runtime.reload_now({"source": "db"})

        self.assertEqual(summary["status"], "reloaded")
        self.assertEqual(summary["sources"]["reloaded"], ["db"])
        runs = status.run_count
        self.assertTrue(wait_until(lambda: status.run_count >= runs + 2))
        self.assertEqual((status.state, status.last_error), ("READY", None))
        lifecycle = plugin_module("LIFECYCLE").LIFECYCLE
        (_, old_id, init_thread), (_, terminated_id, terminate_thread), (_, new_id, _) = lifecycle[:3]
        self.assertEqual([step for step, _, _ in lifecycle[:3]], ["init", "terminate", "init"])
        self.assertEqual((terminated_id, terminate_thread), (old_id, init_thread))
        self.assertNotEqual(new_id, old_id)

    def test_the_old_instance_is_terminated_only_after_its_poll_returns(self):
        self.write("slow.py", '''
            import threading, kanary
            IN_POLL = threading.Event()
            RELEASE = threading.Event()
            EVENTS = []

            @kanary.source(source_id="slow", interval=0.05)
            class Slow:
                def poll(self):
                    if not RELEASE.is_set():
                        IN_POLL.set()
                        RELEASE.wait(10)
                        EVENTS.append(f"poll returned {id(self)}")
                    return {"v": 1}

                def terminate(self):
                    EVENTS.append(f"terminate {id(self)}")

            @kanary.rule(rule_id="slow.v", inputs="slow:v")
            class V(kanary.RangeRule):
                high = 5
        ''')
        runtime = self.start_runtime()
        module = plugin_module("RELEASE")
        self.assertTrue(module.IN_POLL.wait(5))
        old = runtime.engine.sources["slow"]

        with mock.patch("kanary.runtime.SOURCE_STOP_WAIT_SECONDS", 0.1), self.assertLogs("kanary.runtime", "WARNING"):
            summary = runtime.reload_now({"source": "slow"})

        self.assertEqual(summary["status"], "reloaded")
        self.assertIsNot(runtime.engine.sources["slow"], old)
        self.assertEqual(module.EVENTS, [])  # the old poll() still runs: nothing was closed under it
        module.RELEASE.set()
        self.assertTrue(wait_until(lambda: f"terminate {id(old)}" in module.EVENTS))
        self.assertLess(module.EVENTS.index(f"poll returned {id(old)}"), module.EVENTS.index(f"terminate {id(old)}"))

    def test_a_reload_reports_a_new_source_whose_init_fails(self):
        self.write("down.py", '''
            import kanary
            BROKEN = []

            @kanary.source(source_id="down", interval=0.05)
            class Down:
                max_reinit = 0

                def init(self):
                    if BROKEN:
                        raise ConnectionError("database is down")

                def poll(self):
                    return {"v": 1}

            @kanary.rule(rule_id="down.v", inputs="down:v")
            class V(kanary.RangeRule):
                high = 5
        ''')
        runtime = self.start_runtime()
        status = runtime.engine._plugin_status("source", "down")
        self.assertTrue(wait_until(lambda: status.run_count >= 1))
        plugin_module("BROKEN").BROKEN.append(True)

        with self.assertLogs("kanary.runtime", "ERROR"):
            summary = runtime.reload_now({"source": "down"})

        self.assertEqual(summary["status"], "partial")  # applied, but not working
        self.assertEqual(summary["sources"]["reloaded"], ["down"])
        self.assertEqual(summary["sources"]["init_failed"], ["down"])
        self.assertIn("database is down", summary["error"])
        self.assertEqual((status.state, status.last_error), ("FAILED", "database is down"))

    def test_reloads_at_the_same_moment_leave_the_source_polling(self):
        # An operator in the viewer and auto-reload, say: one reload waits for the other.
        self.write("slow.py", '''
            import time, kanary

            @kanary.source(source_id="slow", interval=0.05)
            class Slow:
                def init(self):
                    time.sleep(0.2)

                def poll(self):
                    return {"v": 1}
        ''')
        runtime = self.start_runtime()
        status = runtime.engine._plugin_status("source", "slow")
        self.assertTrue(wait_until(lambda: status.run_count >= 1))
        reloads = [threading.Thread(target=runtime.reload_now, args=({"source": "slow"},)) for _ in range(2)]
        for reload in reloads:
            reload.start()
        for reload in reloads:
            reload.join()
        thread = runtime._source_threads["slow"]
        self.assertTrue(thread.is_alive())
        runs = status.run_count
        self.assertTrue(wait_until(lambda: status.run_count >= runs + 2), "the source stopped polling")

    def test_a_poll_result_that_breaks_the_contract_shows_the_source_failed(self):
        self.write("bad.py", '''
            import kanary

            @kanary.source(source_id="bad", interval=0.05)
            class Bad:
                def poll(self):
                    return object()

            @kanary.rule(rule_id="bad.v", inputs="bad:v")
            class V(kanary.RangeRule):
                high = 5
        ''')
        with self.assertLogs("kanary.runtime", "ERROR"):
            runtime = self.start_runtime()
            status = runtime.engine._plugin_status("source", "bad")
            self.assertTrue(wait_until(lambda: status.run_count >= 2))
        self.assertEqual(status.state, "FAILED")
        self.assertTrue(status.last_error.startswith("poll() returned an invalid result:"), status.last_error)
        self.assertIn("Traceback", status.last_error_detail)


class TwoPhaseReloadTest(PluginDirectoryCase):
    def bootstrap(self) -> EngineRuntime:
        runtime = EngineRuntime(RuntimeConfig(rule_directories=[self.root], api_port=0))
        snapshot = runtime.loader.load()
        runtime._signature = runtime.loader.snapshot_signature()
        runtime._discovered_snapshot = snapshot
        runtime._discovered_metadata = runtime._collect_plugin_metadata(snapshot)
        runtime._loaded_metadata = dict(runtime._discovered_metadata)
        runtime.engine = kanary.Engine(
            source_registry=snapshot.sources, rule_registry=snapshot.rules, output_registry=snapshot.outputs
        )
        runtime.engine.start()

        def close():
            runtime._stop_source_threads(set(runtime._source_threads))
            runtime.api._server.server_close()
            runtime.engine.shutdown()

        self.addCleanup(close)
        return runtime

    def test_a_new_rule_and_a_new_output_in_one_reload_get_the_first_alert(self):
        self.write("source.py", '''
            import kanary

            @kanary.source(source_id="s", interval=3600)
            class S:
                def poll(self):
                    return {"v": 50}
        ''')
        runtime = self.bootstrap()
        runtime.engine.evaluate_source("s", {"v": 50})
        self.write("new.py", '''
            import kanary
            SENT = []

            @kanary.rule(rule_id="s.v", inputs="s:v")
            class V(kanary.RangeRule):
                high = 25

            @kanary.output(output_id="pager")
            class Pager:
                def emit(self, event):
                    SENT.append(event.current_state.value)
        ''')
        runtime.reload_now_if_changed()

        summary = runtime.reload_now({"dirty": True})

        self.assertEqual(summary["status"], "reloaded")
        self.assertEqual((summary["rules"]["reloaded"], summary["outputs"]["reloaded"]), (["s.v"], ["pager"]))
        self.assertTrue(runtime.engine.flush_outputs(5))
        self.assertEqual(plugin_module("SENT").SENT, ["FIRING"])

    def test_every_replacement_is_created_before_any_is_applied(self):
        def plugins(high, broken):
            self.write("rules.py", f'''
                import kanary

                @kanary.source(source_id="s", interval=3600)
                class S:
                    def poll(self):
                        return {{"v": 1}}

                @kanary.rule(rule_id="r.a", inputs="s:v")
                class A(kanary.RangeRule):
                    high = {high}

                @kanary.rule(rule_id="r.b", inputs="s:v")
                class B(kanary.RangeRule):
                    high = {high}

                    def __init__(self):
                        if {broken}:
                            raise RuntimeError("broken constructor")
            ''')

        plugins(10, False)
        runtime = self.bootstrap()
        runtime.engine.evaluate_source("s", {"v": 1})
        old_b = runtime.engine.rules["r.b"]
        plugins(20, True)
        runtime.reload_now_if_changed()

        with self.assertLogs("kanary.engine", "ERROR"):
            summary = runtime.reload_now({"rule": "r.*"})

        self.assertEqual(summary["status"], "partial")
        self.assertEqual(summary["rules"]["reloaded"], ["r.a"])
        self.assertEqual(summary["rules"]["failed"], ["r.b"])
        self.assertEqual(summary["rules"]["errors"], {"r.b": "RuntimeError: broken constructor"})
        self.assertIn("rule 'r.b': RuntimeError: broken constructor", summary["error"])
        self.assertEqual(runtime.engine.rules["r.a"].high, 20)
        self.assertIs(runtime.engine.rules["r.b"], old_b)
        a = runtime.engine._plugin_status("rule", "r.a")
        b = runtime.engine._plugin_status("rule", "r.b")
        self.assertEqual((a.state, a.reload_error), ("READY", None))
        self.assertEqual((b.state, b.reload_error), ("DIRTY", "RuntimeError: broken constructor"))

        plugins(30, False)
        runtime.reload_now_if_changed()
        summary = runtime.reload_now({"dirty": True})
        self.assertEqual(summary["status"], "reloaded")
        self.assertIsNone(runtime.engine._plugin_status("rule", "r.b").reload_error)

    def test_nothing_applied_is_reload_failed(self):
        self.write("rules.py", '''
            import kanary

            @kanary.source(source_id="s", interval=3600)
            class S:
                def poll(self):
                    return {"v": 1}

            @kanary.rule(rule_id="r.a", inputs="s:v")
            class A(kanary.RangeRule):
                high = 10
        ''')
        runtime = self.bootstrap()
        self.write("more.py", '''
            import kanary

            @kanary.rule(rule_id="r.new", inputs="s:v")
            class New(kanary.RangeRule):
                high = 10

                def __init__(self):
                    raise ValueError("no")
        ''')
        runtime.reload_now_if_changed()
        with self.assertLogs("kanary.engine", "ERROR"):
            summary = runtime.reload_now({"dirty": True})
        self.assertEqual(summary["status"], "reload_failed")
        self.assertNotIn("r.new", runtime.engine.rules)
        self.assertEqual(runtime.engine._plugin_status("rule", "r.new").state, "DISCOVERED")

    def test_plugin_files_that_do_not_load_are_reload_failed_with_the_reason(self):
        self.write("rules.py", '''
            import kanary

            @kanary.source(source_id="s", interval=3600)
            class S:
                def poll(self):
                    return {"v": 1}
        ''')
        runtime = self.bootstrap()
        self.write("broken.py", "this is not python\n")
        with self.assertLogs("kanary.runtime", "ERROR"):
            summary = runtime.reload_now({"all": True})
        self.assertEqual(summary["status"], "reload_failed")
        self.assertIn("broken.py", summary["error"])

    def test_a_rule_that_was_never_evaluated_is_ready_after_a_reload(self):
        self.write("rules.py", '''
            import kanary

            @kanary.source(source_id="s", interval=3600)
            class S:
                def poll(self):
                    return {"v": 1}

            @kanary.rule(rule_id="r.a", inputs="s:v")
            class A(kanary.RangeRule):
                high = 10
        ''')
        runtime = self.bootstrap()
        summary = runtime.reload_now({"rule": "r.a"})
        self.assertEqual(summary["status"], "reloaded")
        self.assertEqual(runtime.engine._plugin_status("rule", "r.a").state, "READY")


class ReloadCommandTest(unittest.TestCase):
    def run_ctl(self, answer):
        api = kanary.ControlAPI(
            engine_getter=lambda: None, reload_callback=lambda payload: answer, host="127.0.0.1", port=0
        )
        thread = threading.Thread(target=api.start, daemon=True)
        thread.start()
        self.addCleanup(lambda: (api.shutdown(), thread.join(timeout=2.0)))
        out = io.StringIO()
        argv = ["kanaryctl", "--base-url", f"http://127.0.0.1:{api._server.server_address[1]}", "reload", "--dirty"]
        with contextlib.redirect_stdout(out), mock.patch.object(sys, "argv", argv):
            code = kanaryctl.main()
        return code, out.getvalue()

    def test_a_partial_reload_lists_what_was_not_applied_and_exits_1(self):
        answer = {
            "status": "partial",
            "error": "rule 'r.b': RuntimeError: broken",
            "sources": {"errors": {"db": "init() failed: down"}, "init_failed": ["db"]},
            "rules": {"errors": {"r.b": "RuntimeError: broken"}, "init_failed": []},
            "outputs": {"errors": {}, "init_failed": []},
        }
        code, out = self.run_ctl(answer)
        self.assertEqual(code, 1)
        self.assertEqual(
            out.splitlines(),
            [
                "partial",
                "  source db: applied; init() failed: down (it keeps trying)",
                "  rule r.b: not applied, the running one goes on: RuntimeError: broken",
            ],
        )

    def test_a_failed_reload_prints_the_loader_error(self):
        code, out = self.run_ctl({"status": "reload_failed", "error": "failed to load plugin file 'x.py'"})
        self.assertEqual(code, 1)
        self.assertEqual(out.splitlines(), ["reload_failed", "  failed to load plugin file 'x.py'"])

    def test_a_clean_reload_exits_0(self):
        code, out = self.run_ctl({"status": "reloaded"})
        self.assertEqual((code, out), (0, "reloaded\n"))


class LoaderTest(unittest.TestCase):
    def test_an_edit_in_the_same_second_with_the_same_size_is_loaded(self):
        # The bytecode cache checks whole-second mtimes and sizes: it would run the old code.
        root = Path(tempfile.mkdtemp())
        path = root / "rule.py"
        loader = RuleDirectoryLoader(root)

        def write(high):
            path.write_text(textwrap.dedent(f'''
                import kanary

                @kanary.rule(rule_id="r", inputs="s:v")
                class R(kanary.RangeRule):
                    high = {high}
            '''))
            os.utime(path, ns=(1_700_000_000_000_000_000, 1_700_000_000_000_000_000))

        with mock.patch.object(sys, "dont_write_bytecode", False):
            write(10)
            self.assertEqual(loader.load().rules["r"].high, 10)
            write(20)
            self.assertEqual(loader.load().rules["r"].high, 20)


if __name__ == "__main__":
    unittest.main()
