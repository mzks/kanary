"""Operator actions while a peer is down, helper settings written as people write them,
plugin classes without a source file, and the kanary command's own errors."""

import contextlib
from datetime import datetime, time as clock, timedelta, timezone
import io
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock
from urllib.request import Request, urlopen

import kanary
from kanary import __main__ as kanary_main
from kanary.helpers.oncall import parse_clock, parse_weekdays
from kanary.helpers.probe import _named, _plain_name, _url_name
from kanary.helpers.remote import RemoteAlarm
from kanary.loader import RuleDirectoryLoader
from kanary.units import parse_duration
from kanary.values import address_list

T0 = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class PeerDownTest(unittest.TestCase):
    """A peer that cannot be reached does not stop the local action; it is a warning."""

    def setUp(self):
        peer = type("Peer", (kanary.RemoteKanarySource,), {
            "source_id": "peer", "base_url": f"http://127.0.0.1:{free_port()}", "timeout": 1.0,
        })
        daq = type("daq", (kanary.Source,), {"source_id": "daq", "interval": 60.0, "poll": lambda self: kanary.no_update()})
        mirror = type("M", (RemoteAlarm,), {
            "rule_id": "remote.link", "source": "peer", "remote_alarm_id": "link", "tags": ["t"],
            "propagate_ack": True, "propagate_silence": True,
        })
        local = type("L", (kanary.RangeRule,), {"rule_id": "local.hv", "inputs": "daq:hv", "high": 1, "tags": ["t"]})
        self.engine = kanary.Engine(
            source_registry={"peer": peer, "daq": daq},
            rule_registry={"remote.link": mirror, "local.hv": local},
            output_registry={},
            now_fn=lambda: T0,
        )
        self.engine.start()
        self.addCleanup(self.engine.shutdown)
        self.engine.evaluate_source("peer", kanary.inputs(("link", 1, T0, {"state": "FIRING", "severity": "ERROR"})), now=T0)
        self.engine.evaluate_source("daq", {"hv": 5}, now=T0)

    def test_a_silence_still_holds_here(self):
        warnings = []
        with self.assertLogs("kanary.engine", "WARNING"):
            silence = self.engine.create_silence(
                operator="op", start_at=T0, end_at=T0 + timedelta(hours=1), tags=["t"], warnings=warnings
            )
        self.assertEqual(silence.remote_silence_refs, ())
        self.assertEqual(self.engine.alerts["local.hv"].state, kanary.SILENCED)
        self.assertEqual(len(warnings), 1)
        self.assertIn("the silence of 'remote.link' did not reach its peer", warnings[0])

    def test_an_ack_still_holds_here(self):
        warnings = []
        with self.assertLogs("kanary.engine", "WARNING"):
            alert = self.engine.acknowledge("remote.link", operator="op", warnings=warnings)
        self.assertEqual((alert.state, alert.acked_by), (kanary.ACKED, "op"))
        self.assertIn("did not reach its peer", warnings[0])
        with self.assertLogs("kanary.engine", "WARNING"):
            self.engine.unacknowledge("remote.link", operator="op", warnings=warnings)
        self.assertEqual(self.engine.alerts["remote.link"].state, kanary.FIRING)

    def test_a_silence_with_a_copy_at_the_peer_can_be_cancelled_while_the_peer_is_down(self):
        silence = self.engine.create_silence(operator="op", start_at=T0, end_at=T0 + timedelta(hours=1), rule_patterns=["local.*"])
        silence.remote_silence_refs = ("peer:remote-1",)  # as if the peer had taken a copy
        warnings = []
        with self.assertLogs("kanary.engine", "WARNING"):
            self.engine.cancel_silence(silence.silence_id, operator="op", warnings=warnings)
        self.assertIsNotNone(silence.cancelled_at)
        self.assertIn("silence remote-1 at 'peer' was not cancelled", warnings[0])

    def test_the_api_answers_with_the_warnings(self):
        api = kanary.ControlAPI(engine_getter=lambda: self.engine, reload_callback=lambda body: {}, host="127.0.0.1", port=0)
        thread = threading.Thread(target=api.start, daemon=True)
        thread.start()
        self.addCleanup(lambda: (api.shutdown(), thread.join(timeout=2.0)))
        base = f"http://127.0.0.1:{api._server.server_address[1]}"

        def post(path, body):
            request = Request(base + path, method="POST", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
            with urlopen(request) as response:
                return json.loads(response.read())

        with self.assertLogs("kanary.engine", "WARNING"):
            acked = post("/alerts/remote.link/ack", {"operator": "op"})
            silenced = post("/silences/duration", {"operator": "op", "duration_minutes": 10, "tags": "t"})
        self.assertEqual(acked["status"], "acked")
        self.assertTrue(any("did not reach its peer" in warning for warning in acked["warnings"]))
        self.assertTrue(any("did not reach its peer" in warning for warning in silenced["warnings"]))


class RecipientsTest(unittest.TestCase):
    def test_one_string_is_addresses_not_letters(self):
        self.assertEqual(address_list("Ops Team <ops@x.org>, b@y.org; c@z.org"), ["Ops Team <ops@x.org>", "b@y.org", "c@z.org"])
        sent = []
        mail = type("M", (kanary.MailOutput,), {
            "output_id": "m", "smtp_host": "smtp.example", "sender": "k@example.org",
            "recipients": "ops@example.org, oncall@example.org", "send_mail": lambda self, message: sent.append(message["To"]),
        })()
        mail.init()
        alert = kanary.Alert(rule_id="r", state=kanary.FIRING, severity=kanary.ERROR)
        mail.emit(kanary.AlertEvent(rule_id="r", previous_state=None, current_state=kanary.FIRING, previous_severity=None,
                                    current_severity=kanary.ERROR, transition=None, alert=alert, occurred_at=T0))
        self.assertEqual(sent, ["ops@example.org, oncall@example.org"])
        self.assertEqual(kanary.OnCallSchedule([], default="ops@example.org").recipients(T0), ["ops@example.org"])

    def test_port_465_connects_with_tls_from_the_start(self):
        made = []

        class Fake:
            def __init__(self, kind):
                self.kind = kind

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def starttls(self):
                made.append("starttls")

            def send_message(self, message):
                made.append(self.kind)

        mail = type("M", (kanary.MailOutput,), {"output_id": "m", "smtp_host": "smtp.example", "sender": "k@x", "recipients": ["a@x"]})()
        with mock.patch("smtplib.SMTP_SSL", lambda *a, **k: Fake("ssl")), mock.patch("smtplib.SMTP", lambda *a, **k: Fake("plain")):
            mail.smtp_port = 465
            mail.send_mail(_message())
            mail.smtp_port = 587
            mail.send_mail(_message())
            mail.use_ssl = True
            mail.send_mail(_message())
        self.assertEqual(made, ["ssl", "starttls", "plain", "ssl"])


def _event():
    alert = kanary.Alert(rule_id="r", state=kanary.FIRING, severity=kanary.ERROR)
    return kanary.AlertEvent(rule_id="r", previous_state=None, current_state=kanary.FIRING, previous_severity=None,
                             current_severity=kanary.ERROR, transition=None, alert=alert, occurred_at=T0)


def _message():
    from email.message import EmailMessage

    message = EmailMessage()
    message["Subject"] = "test"
    message.set_content("body")
    return message


class OnCallTest(unittest.TestCase):
    def test_weekdays_as_people_write_them(self):
        self.assertEqual(parse_weekdays(["mon-fri"]), (0, 1, 2, 3, 4))
        self.assertEqual(parse_weekdays("mon,tue, Wednesday"), (0, 1, 2))
        self.assertEqual(parse_weekdays("fri-mon"), (4, 5, 6, 0))
        self.assertEqual(parse_weekdays(["Tues", "thurs"]), (1, 3))
        with self.assertRaisesRegex(ValueError, "not 'monkey'"):
            parse_weekdays(["monkey"])
        schedule = kanary.OnCallSchedule(
            [{"weekdays": "mon-fri", "from": "09:00", "to": "18:00", "recipients": "day@x, lead@x"}], timezone="UTC"
        )
        self.assertEqual(schedule.recipients(datetime(2026, 11, 27, 10, 0, tzinfo=timezone.utc)), ["day@x", "lead@x"])  # a Friday
        self.assertEqual(schedule.recipients(datetime(2026, 11, 28, 10, 0, tzinfo=timezone.utc)), [])  # a Saturday

    def test_a_shift_to_midnight_and_one_of_no_length(self):
        evening = kanary.OnCallSchedule([{"from": "18:00", "to": "24:00", "recipients": ["night@x"]}], timezone="UTC")
        self.assertEqual(evening.recipients(datetime(2026, 11, 22, 23, 30, tzinfo=timezone.utc)), ["night@x"])
        self.assertEqual(evening.recipients(datetime(2026, 11, 23, 0, 30, tzinfo=timezone.utc)), [])
        with self.assertRaisesRegex(ValueError, "for the whole day, leave both out"):
            kanary.OnCallSchedule([{"from": "09:00", "to": "09:00", "recipients": ["x"]}])


class QuietHoursSettingsTest(unittest.TestCase):
    def test_one_window_without_the_list_24_00_and_severity_in_any_form(self):
        quiet = kanary.QuietHours(("22:00", "24:00"), timezone="UTC", minimum_severity=30)
        self.addCleanup(quiet.close)
        self.assertEqual(quiet.minimum_severity, kanary.ERROR)
        warn = _event()
        warn.current_severity = kanary.WARN
        warn.occurred_at = datetime(2026, 11, 22, 23, 0, tzinfo=timezone.utc)
        self.assertTrue(quiet.applies(warn))
        warn.occurred_at = datetime(2026, 11, 23, 0, 30, tzinfo=timezone.utc)
        self.assertFalse(quiet.applies(warn))
        self.assertEqual(parse_clock("24:00"), clock(0, 0))
        self.assertEqual(kanary.QuietHours([("1", "2")], minimum_severity="critical").minimum_severity, kanary.CRITICAL)

    def test_a_window_of_the_whole_day_is_refused_with_the_way(self):
        for window in ([("00:00", "24:00")], [("09:00", "09:00")]):
            with self.subTest(window), self.assertRaisesRegex(ValueError, "minimum_severity instead"):
                kanary.QuietHours(window)


class RuleSettingsTest(unittest.TestCase):
    def test_one_tag_as_a_string_and_inputs_as_a_tuple(self):
        rule = kanary.rule(rule_id="one.tag", inputs=("s:a", "s:b"), tags="infra")(type("R", (kanary.RangeRule,), {"high": 1}))
        self.assertEqual((rule.tags, rule.inputs), (["infra"], ["s:a", "s:b"]))
        with self.assertRaisesRegex(ValueError, "tags must be a tag or a list of tags"):
            kanary.rule(rule_id="bad.tag", inputs="s:a", tags=[1])(type("R", (kanary.RangeRule,), {"high": 1}))

    def test_an_inherited_normalize_evaluation_is_kept(self):
        class Base(kanary.Rule):
            def normalize_evaluation(self, result, payload):
                evaluation = super().normalize_evaluation(result, payload)
                evaluation.message = "from the base"
                return evaluation

        child = kanary.rule(rule_id="child", inputs="s:v", tags=["t"])(
            type("Child", (Base,), {"evaluate": lambda self, ctx: kanary.ok("from evaluate")})
        )
        self.assertIs(child.normalize_evaluation, Base.normalize_evaluation)
        plain = kanary.rule(rule_id="plain", inputs="s:v", tags=["t"])(type("Plain", (), {"evaluate": lambda self, ctx: None}))
        self.assertIs(plain.normalize_evaluation, kanary.Rule.normalize_evaluation)

    def test_durations_as_text(self):
        self.assertEqual([parse_duration(text) for text in ("30s", "5m", "1h30m", "1.5 h", "2 days", "500ms", "300", 45)],
                         [30.0, 300.0, 5400.0, 5400.0, 172800.0, 0.5, 300.0, 45.0])
        for bad in ("", "5x", True, "inf"):
            with self.subTest(bad), self.assertRaises(ValueError):
                parse_duration(bad)
        source = kanary.source(source_id="every.30s", interval="30s")(type("S", (), {"poll": lambda self: {}}))
        rule = kanary.rule(rule_id="timer.1m", inputs="s:v", tags=["t"], evaluation_interval="1m")(type("R", (kanary.RangeRule,), {"high": 1}))
        self.assertEqual((source.interval, rule.evaluation_interval), (30.0, 60.0))
        with self.assertRaisesRegex(ValueError, "interval: a duration"):
            kanary.source(source_id="bad.interval", interval="soon")(type("S", (), {"poll": lambda self: {}}))


class NoSourceFileTest(unittest.TestCase):
    def test_a_plugin_defined_where_there_is_no_file(self):
        code = (
            "import kanary\n"
            "@kanary.source(source_id='s')\n"
            "class S:\n    def poll(self): return {'v': 5}\n"
            "@kanary.rule(rule_id='r', inputs='s:v', tags=['t'])\n"
            "class R(kanary.RangeRule):\n    high = 1\n"
            "e = kanary.Engine(source_registry={'s': S}, rule_registry={'r': R}, output_registry={}); e.start()\n"
            "e.evaluate_source('s', {'v': 5}); print(e.alerts['r'].state.value, R.__kanary_definition_file__); e.shutdown()\n"
        )
        completed = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.split(), ["FIRING", "None"])


class ProbeTargetsTest(unittest.TestCase):
    def test_targets_as_a_list(self):
        self.assertEqual(_named(["db1", "db2"], "PingSource.hosts", "x", str), {"db1": "db1", "db2": "db2"})
        self.assertEqual(_named(["db1:5432", "[::1]:22"], "TCPSource.targets", "x", _plain_name), {"db1_5432": "db1:5432", "__1_22": "[::1]:22"})
        self.assertEqual(_named(["https://example.org/health", "http://h:8080/"], "HTTPStatusSource.urls", "x", _url_name),
                         {"example.org/health": "https://example.org/health", "h_8080": "http://h:8080/"})
        with self.assertRaisesRegex(ValueError, "both get the name"):
            _named(["a:1", "a_1"], "TCPSource.targets", "x", _plain_name)
        with self.assertRaisesRegex(ValueError, "must be a dict of input names to hosts, or a list of them"):
            _named(5, "PingSource.hosts", "input names to hosts", str)

    def test_a_ping_to_a_listed_host(self):
        result = type("P", (kanary.PingSource,), {"source_id": "p", "hosts": ["127.0.0.1"], "timeout": 2.0})().poll()
        self.assertEqual([item.name for item in result.inputs], ["127.0.0.1.rtt_ms"])


class CommandSettingsTest(unittest.TestCase):
    def test_env_values_as_text_and_cwd_next_to_the_plugin(self):
        root = Path(tempfile.mkdtemp())
        (root / "work").mkdir()
        source = type("C", (kanary.CommandSource,), {
            "source_id": "c", "command": [sys.executable, "-c", "import os; print(os.environ['N'], os.path.basename(os.getcwd()))"],
            "env": {"N": 3}, "cwd": "work", "output_format": "keyvalue", "__kanary_definition_file__": str(root / "plugin.py"),
        })()
        completed = source.run()
        self.assertEqual(completed.stdout.split(), ["3", "work"])


class CommandLineTest(unittest.TestCase):
    def run_kanary(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "argv", ["kanary", *argv]), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = kanary_main.main()
        return code, out.getvalue() + err.getvalue()

    def test_a_directory_that_does_not_exist(self):
        code, text = self.run_kanary("lint", "/no/such/plugins")
        self.assertEqual(code, 1)
        self.assertIn("lint failed: plugin directory not found: /no/such/plugins", text)
        code, text = self.run_kanary("run", "/no/such/plugins")
        self.assertEqual(code, 2)
        self.assertIn("kanary: plugin directory not found: /no/such/plugins", text)
        with self.assertRaisesRegex(ValueError, "plugin directory not found"):
            RuleDirectoryLoader("/no/such/plugins").load()

    def test_a_port_that_is_taken(self):
        with socket.socket() as taken:
            taken.bind(("127.0.0.1", 0))
            taken.listen()
            port = taken.getsockname()[1]
            code, text = self.run_kanary("run", tempfile.mkdtemp(), "--api-host", "127.0.0.1", "--api-port", str(port))
        self.assertEqual(code, 1)
        self.assertIn(f"kanary: cannot listen on 127.0.0.1:{port}", text)
        self.assertIn("choose another --api-port", text)

    def test_option_values_in_any_case(self):
        args = kanary_main.build_parser().parse_args(["run", "x", "--log-level", "info", "--auto-reload", "Dirty"])
        self.assertEqual((args.log_level, args.auto_reload), ("INFO", "dirty"))


if __name__ == "__main__":
    unittest.main()
