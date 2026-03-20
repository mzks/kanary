from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import textwrap
import threading
import unittest
from urllib.request import Request, urlopen

import kanary
from kanary.runtime import EngineRuntime, RuntimeConfig


def fetch_json(url: str, method: str = "GET", body: dict | None = None) -> dict:
    data = None if body is None else json.dumps(body).encode()
    request = Request(url, method=method, data=data)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    with urlopen(request) as response:
        return json.loads(response.read().decode())


@kanary.source(source_id="postgres", interval=5.0)
class SlowPostgresSource:

    def __init__(self) -> None:
        self.now = datetime(2026, 3, 17, 0, 0, tzinfo=timezone.utc)
        self.temperature = 123
        self.humidity = 45

    def poll(self, ctx):
        return kanary.SourceResult(
            measurements=[
                kanary.Measurement(name="temperature", value=self.temperature, timestamp=self.now),
                kanary.Measurement(name="humidity", value=self.humidity, timestamp=self.now),
            ]
        )


@kanary.source(source_id="buffered", interval=5.0)
class BufferedTemperatureSource(kanary.BufferedSource):
    history_limit = 8
    history_window_seconds = 3600.0

    def __init__(self) -> None:
        self.samples = [
            (datetime(2026, 3, 17, 0, 0, tzinfo=timezone.utc), 10.0),
            (datetime(2026, 3, 17, 0, 30, tzinfo=timezone.utc), 22.0),
            (datetime(2026, 3, 17, 1, 0, tzinfo=timezone.utc), 34.0),
        ]
        self.index = 0

    def fetch(self, ctx):
        timestamp, value = self.samples[min(self.index, len(self.samples) - 1)]
        self.index += 1
        return kanary.SourceResult(
            measurements=[
                kanary.Measurement(name="temperature", value=value, timestamp=timestamp),
            ]
        )


@kanary.rule(
    rule_id="postgres.temperature.stale",
    source="postgres",
    severity=kanary.ERROR,
    tags=["infra", "postgres"],
    owner="expert_db",
)
class SlowPostgresStale(kanary.StaleRule):
    measurement = "temperature"
    timeout = 10 * kanary.minute


@kanary.rule(
    rule_id="postgres.temperature.range",
    source="postgres",
    severity=kanary.WARN,
    tags=["infra", "postgres"],
    owner="expert_db",
)
class SlowPostgresHighValue(kanary.RangeRule):
    measurement = "temperature"
    high = 100
    hysteresis = 5.0


@kanary.rule(
    rule_id="postgres.humidity.range",
    source="postgres",
    severity=kanary.WARN,
    tags=["infra", "postgres"],
    owner="expert_db",
)
class SlowPostgresExclusiveRange(kanary.RangeRule):
    measurement = "humidity"
    low = 45
    high = 50
    lower_inclusive = False
    upper_inclusive = False


@kanary.rule(
    rule_id="postgres.humidity.suppressed_range",
    source="postgres",
    severity=kanary.WARN,
    tags=["infra", "postgres"],
    owner="expert_db",
)
class SuppressedByTemperatureRange(kanary.RangeRule):
    measurement = "humidity"
    low = 40
    high = 50
    suppressed_by = ["postgres.temperature.range"]


@kanary.rule(
    rule_id="postgres.temperature.rate",
    source="postgres",
    severity=kanary.WARN,
    tags=["infra", "postgres"],
    owner="expert_db",
)
class TemperatureRate(kanary.RateRule):
    measurement = "temperature"
    low = -1.0
    high = 0.5
    per_seconds = 1 * kanary.minute


@kanary.rule(
    rule_id="postgres.temperature.threshold",
    source="postgres",
    severity=kanary.WARN,
    tags=["infra", "postgres", "threshold"],
    owner="expert_db",
)
class TemperatureThreshold(kanary.ThresholdRule):
    measurement = "temperature"
    direction = "high"
    hysteresis = 1.0
    thresholds = [
        (20.0, kanary.WARN),
        (24.0, kanary.ERROR),
        (28.0, kanary.CRITICAL),
    ]


@kanary.rule(
    rule_id="postgres.temperature.custom_threshold",
    source="postgres",
    severity=kanary.WARN,
    tags=["infra", "postgres", "custom"],
    owner="expert_db",
)
class TemperatureCustomThreshold:

    def evaluate(self, payload, ctx):
        temperature = ctx.value("temperature")
        if temperature is None:
            return kanary.Evaluation(
                state=kanary.AlertState.OK,
                payload=payload,
                message="temperature is missing",
            )
        if temperature >= 28:
            severity = kanary.CRITICAL
        elif temperature >= 24:
            severity = kanary.ERROR
        elif temperature >= 20:
            severity = kanary.WARN
        else:
            return kanary.Evaluation(
                state=kanary.AlertState.OK,
                payload=payload,
                message=f"temperature={temperature} below escalation threshold",
            )

        return kanary.Evaluation(
            state=kanary.AlertState.FIRING,
            payload=payload,
            message=f"temperature={temperature} exceeded custom threshold",
            severity=severity,
        )


@kanary.source(source_id="remote-api", interval=60.0)
class RemoteAPISource(kanary.RemoteKanarySource):
    url = "http://127.0.0.1:1"


@kanary.rule(
    rule_id="mirror.postgres.temperature.stale",
    source="remote-api",
    severity=kanary.ERROR,
    tags=["remote", "mirror"],
    owner="expert_remote",
)
class MirroredTemperatureStale(kanary.RemoteAlarm):
    remote_alarm_id = "postgres.temperature.stale"
    propagate_ack = True
    propagate_silence = True


@kanary.rule(
    rule_id="postgres.temperature_humidity.balance",
    source="postgres",
    severity=kanary.ERROR,
    tags=["infra", "postgres", "composite"],
    owner="expert_db",
)
class TemperatureHumidityBalance:

    def evaluate(self, payload, ctx):
        temperature = ctx.value("temperature")
        humidity = ctx.value("humidity")
        if temperature is None or humidity is None:
            return kanary.Evaluation(
                state=kanary.AlertState.OK,
                payload=payload,
                message="temperature or humidity is missing",
            )

        expected_humidity = temperature / 2
        delta = humidity - expected_humidity
        result_payload = dict(payload)
        result_payload["expected_humidity"] = expected_humidity
        result_payload["delta"] = delta
        if abs(delta) <= 5:
            return kanary.Evaluation(
                state=kanary.AlertState.OK,
                payload=result_payload,
                message=f"humidity={humidity} consistent with temperature={temperature}",
            )

        return kanary.Evaluation(
            state=kanary.AlertState.FIRING,
            payload=result_payload,
            message=(
                f"humidity={humidity} inconsistent with temperature={temperature} "
                f"(expected {expected_humidity}, delta {delta})"
            ),
        )


class EngineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 3, 17, 0, 20, tzinfo=timezone.utc)
        self.engine = kanary.Engine(now_fn=lambda: self.now, output_registry={})
        self.engine.start()

    def tearDown(self) -> None:
        self.engine.shutdown()

    def test_stale_rule_fires(self) -> None:
        source = self.engine.sources["postgres"]
        alerts = self.engine.evaluate_source(source.source_id, source.poll({}), now=self.now)
        alert = alerts["postgres.temperature.stale"]
        self.assertEqual(alert.state, kanary.AlertState.FIRING)
        self.assertEqual(alert.owner, "expert_db")
        self.assertIn("age_seconds", alert.payload)

    def test_rule_resolves_when_source_recovers(self) -> None:
        source = self.engine.sources["postgres"]
        self.engine.evaluate_source(source.source_id, source.poll({}), now=self.now)
        source.now = self.now - timedelta(seconds=10)
        alerts = self.engine.evaluate_source(source.source_id, source.poll({}), now=self.now)
        self.assertEqual(alerts["postgres.temperature.stale"].state, kanary.AlertState.OK)

    def test_removed_rule_is_resolved_on_reload(self) -> None:
        source = self.engine.sources["postgres"]
        self.engine.evaluate_source(source.source_id, source.poll({}), now=self.now)
        self.engine.reload(rule_registry={})
        self.assertEqual(
            self.engine.alerts["postgres.temperature.stale"].state,
            kanary.AlertState.RESOLVED,
        )

    def test_range_rule_fires_when_value_is_high(self) -> None:
        source = self.engine.sources["postgres"]
        alerts = self.engine.evaluate_source(source.source_id, source.poll({}), now=self.now)
        alert = alerts["postgres.temperature.range"]
        self.assertEqual(alert.state, kanary.AlertState.FIRING)
        self.assertEqual(
            alert.message,
            "channels.temperature.value=123 out of range [-inf, 100]",
        )

    def test_range_rule_hysteresis_keeps_alert_active_until_clear_band(self) -> None:
        source = self.engine.sources["postgres"]
        self.engine.evaluate_source(source.source_id, source.poll({}), now=self.now)

        source.temperature = 97
        alerts = self.engine.evaluate_source(source.source_id, source.poll({}), now=self.now)
        self.assertEqual(alerts["postgres.temperature.range"].state, kanary.AlertState.FIRING)

        source.temperature = 95
        alerts = self.engine.evaluate_source(source.source_id, source.poll({}), now=self.now)
        self.assertEqual(alerts["postgres.temperature.range"].state, kanary.AlertState.OK)

    def test_engine_keeps_previous_source_snapshot(self) -> None:
        source = self.engine.sources["postgres"]
        self.engine.evaluate_source(source.source_id, source.poll({}), now=self.now)
        first_state = self.engine.source_states["postgres"]
        self.assertEqual(
            kanary.get_by_path(first_state.current.payload, "channels.temperature.value"),
            123,
        )
        self.assertEqual(first_state.previous.payload, {})

        source.now = self.now - timedelta(seconds=5)
        self.engine.evaluate_source(source.source_id, source.poll({}), now=self.now)
        second_state = self.engine.source_states["postgres"]
        self.assertEqual(
            kanary.get_by_path(second_state.previous.payload, "channels.temperature.value"),
            123,
        )
        self.assertEqual(
            kanary.get_by_path(second_state.current.payload, "channels.humidity.value"),
            45,
        )

    def test_rule_context_measurement_accessors_work_for_current_and_previous(self) -> None:
        source = self.engine.sources["postgres"]
        self.engine.evaluate_source(source.source_id, source.poll({}), now=self.now)
        source.now = self.now - timedelta(seconds=5)
        self.engine.evaluate_source(source.source_id, source.poll({}), now=self.now)

        source_state = self.engine.source_states["postgres"]
        ctx = kanary.RuleContext(
            now=self.now,
            source_id="postgres",
            source_state=source_state,
        )
        self.assertEqual(ctx.value("temperature"), 123)
        self.assertEqual(ctx.timestamp("temperature"), source.now)
        self.assertEqual(ctx.value("temperature", previous=True), 123)
        self.assertEqual(ctx.metadata("temperature"), {})

    def test_engine_can_exclude_rules_by_glob(self) -> None:
        engine = kanary.Engine(
            now_fn=lambda: self.now,
            output_registry={},
            exclude_rule_patterns=["postgres.temperature.*"],
        )
        engine.start()
        try:
            source = engine.sources["postgres"]
            alerts = engine.evaluate_source(source.source_id, source.poll({}), now=self.now)
            self.assertNotIn("postgres.temperature.stale", alerts)
            self.assertNotIn("postgres.temperature.range", alerts)
            self.assertIn("postgres.humidity.range", alerts)
        finally:
            engine.shutdown()

    def test_range_rule_supports_open_interval_bounds(self) -> None:
        source = self.engine.sources["postgres"]
        alerts = self.engine.evaluate_source(source.source_id, source.poll({}), now=self.now)
        alert = alerts["postgres.humidity.range"]
        self.assertEqual(alert.state, kanary.AlertState.FIRING)
        self.assertEqual(
            alert.message,
            "channels.humidity.value=45 out of range (45, 50)",
        )

    def test_rule_can_be_suppressed_by_other_rule(self) -> None:
        source = self.engine.sources["postgres"]
        alerts = self.engine.evaluate_source(source.source_id, source.poll({}), now=self.now)
        suppressed_alert = alerts["postgres.humidity.suppressed_range"]
        self.assertEqual(suppressed_alert.state, kanary.AlertState.SUPPRESSED)
        self.assertEqual(
            suppressed_alert.message,
            "suppressed by postgres.temperature.range",
        )

    def test_threshold_rule_can_raise_severity_by_band(self) -> None:
        source = self.engine.sources["postgres"]
        source.temperature = 26
        alerts = self.engine.evaluate_source(source.source_id, source.poll({}), now=self.now)
        self.assertEqual(alerts["postgres.temperature.threshold"].state, kanary.AlertState.FIRING)
        self.assertEqual(alerts["postgres.temperature.threshold"].severity, kanary.ERROR)

        source.temperature = 29
        alerts = self.engine.evaluate_source(source.source_id, source.poll({}), now=self.now)
        self.assertEqual(alerts["postgres.temperature.threshold"].severity, kanary.CRITICAL)

    def test_threshold_rule_hysteresis_holds_previous_severity_until_clear_band(self) -> None:
        source = self.engine.sources["postgres"]
        source.temperature = 29
        alerts = self.engine.evaluate_source(source.source_id, source.poll({}), now=self.now)
        self.assertEqual(alerts["postgres.temperature.threshold"].severity, kanary.CRITICAL)

        source.temperature = 27.5
        alerts = self.engine.evaluate_source(source.source_id, source.poll({}), now=self.now)
        self.assertEqual(alerts["postgres.temperature.threshold"].severity, kanary.CRITICAL)

        source.temperature = 26.5
        alerts = self.engine.evaluate_source(source.source_id, source.poll({}), now=self.now)
        self.assertEqual(alerts["postgres.temperature.threshold"].severity, kanary.ERROR)

    def test_custom_rule_can_override_severity(self) -> None:
        source = self.engine.sources["postgres"]
        source.temperature = 21
        alerts = self.engine.evaluate_source(source.source_id, source.poll({}), now=self.now)
        self.assertEqual(alerts["postgres.temperature.custom_threshold"].severity, kanary.WARN)

        source.temperature = 30
        alerts = self.engine.evaluate_source(source.source_id, source.poll({}), now=self.now)
        self.assertEqual(alerts["postgres.temperature.custom_threshold"].severity, kanary.CRITICAL)

    def test_acknowledge_tracks_operator_and_switches_to_acked(self) -> None:
        source = self.engine.sources["postgres"]
        self.engine.evaluate_source(source.source_id, source.poll({}), now=self.now)
        alert = self.engine.acknowledge(
            "postgres.temperature.stale",
            operator="alice",
            reason="investigating",
        )
        self.assertEqual(alert.state, kanary.AlertState.ACKED)
        self.assertEqual(alert.acked_by, "alice")
        self.assertEqual(alert.ack_reason, "investigating")

        alerts = self.engine.evaluate_source(source.source_id, source.poll({}), now=self.now)
        self.assertEqual(alerts["postgres.temperature.stale"].state, kanary.AlertState.ACKED)

    def test_unacknowledge_returns_acked_alert_to_firing(self) -> None:
        source = self.engine.sources["postgres"]
        self.engine.evaluate_source(source.source_id, source.poll({}), now=self.now)
        self.engine.acknowledge(
            "postgres.temperature.stale",
            operator="alice",
            reason="investigating",
        )
        alert = self.engine.unacknowledge(
            "postgres.temperature.stale",
            operator="alice",
            reason="re-open",
        )
        self.assertEqual(alert.state, kanary.AlertState.FIRING)
        self.assertIsNone(alert.acked_by)
        self.assertNotIn("postgres.temperature.stale", self.engine.acknowledgements)

    def test_silence_overrides_firing_and_future_silence_waits(self) -> None:
        source = self.engine.sources["postgres"]
        self.engine.create_silence(
            operator="bob",
            reason="maintenance",
            start_at=self.now + timedelta(minutes=5),
            end_at=self.now + timedelta(minutes=10),
            rule_patterns=["postgres.temperature.stale"],
        )
        alerts = self.engine.evaluate_source(source.source_id, source.poll({}), now=self.now)
        self.assertEqual(alerts["postgres.temperature.stale"].state, kanary.AlertState.FIRING)

        silence = self.engine.create_silence(
            operator="bob",
            reason="maintenance",
            start_at=self.now - timedelta(minutes=1),
            end_at=self.now + timedelta(minutes=10),
            rule_patterns=["postgres.temperature.stale"],
        )
        alerts = self.engine.evaluate_source(source.source_id, source.poll({}), now=self.now)
        self.assertEqual(alerts["postgres.temperature.stale"].state, kanary.AlertState.SILENCED)
        self.assertIn(silence.silence_id, alerts["postgres.temperature.stale"].active_silence_ids)

    def test_cancelled_silence_no_longer_applies(self) -> None:
        source = self.engine.sources["postgres"]
        silence = self.engine.create_silence(
            operator="bob",
            reason="maintenance",
            start_at=self.now - timedelta(minutes=1),
            end_at=self.now + timedelta(minutes=10),
            tags=["infra"],
        )
        self.engine.cancel_silence(silence.silence_id, operator="bob")
        alerts = self.engine.evaluate_source(source.source_id, source.poll({}), now=self.now)
        self.assertEqual(alerts["postgres.temperature.stale"].state, kanary.AlertState.FIRING)

    def test_custom_rule_can_read_multiple_measurements(self) -> None:
        source = self.engine.sources["postgres"]
        alerts = self.engine.evaluate_source(source.source_id, source.poll({}), now=self.now)
        alert = alerts["postgres.temperature_humidity.balance"]
        self.assertEqual(alert.state, kanary.AlertState.FIRING)
        self.assertEqual(
            alert.message,
            "humidity=45 inconsistent with temperature=123 (expected 61.5, delta -16.5)",
        )
        self.assertEqual(alert.payload["expected_humidity"], 61.5)

    def test_rate_rule_uses_current_and_previous_samples(self) -> None:
        source = self.engine.sources["postgres"]
        source.now = self.now - timedelta(seconds=240)
        self.engine.evaluate_source(source.source_id, source.poll({}), now=self.now)
        source.now = self.now - timedelta(seconds=120)
        source.temperature = 0
        alerts = self.engine.evaluate_source(source.source_id, source.poll({}), now=self.now)
        alert = alerts["postgres.temperature.rate"]
        self.assertEqual(alert.state, kanary.AlertState.FIRING)
        self.assertAlmostEqual(alert.payload["rate"], -61.5)
        self.assertAlmostEqual(alert.payload["rate_per_second"], -1.025)
        self.assertEqual(alert.payload["rate_delta_seconds"], 120.0)
        self.assertEqual(
            alert.message,
            "channels.temperature.value rate=-61.5 / 1 min out of range [-1.0, 0.5]",
        )


class BufferedSourceTest(unittest.TestCase):
    def test_buffered_source_keeps_history_and_computes_aggregates(self) -> None:
        source = BufferedTemperatureSource()
        source.init({})
        try:
            source.poll({})
            source.poll({})
            source.poll({})
            history = source.history("temperature")
            self.assertEqual(len(history), 3)
            self.assertEqual(source.average_value("temperature"), 22.0)
            self.assertEqual(source.min_value("temperature"), 10.0)
            self.assertEqual(source.max_value("temperature"), 34.0)
            self.assertEqual(source.count("temperature"), 3)
            self.assertEqual(source.rate("temperature", per_seconds=60.0), 0.4)
        finally:
            source.terminate({})


class RuleDirectoryLoaderTest(unittest.TestCase):
    def test_loads_python_files_from_rule_directory(self) -> None:
        with TemporaryDirectory() as tmp:
            rule_file = Path(tmp) / "rules.py"
            rule_file.write_text(
                textwrap.dedent(
                    """
                    import kanary

                    @kanary.source(source_id="example.source", interval=1.5)
                    class ExampleSource:

                        def poll(self, ctx):
                            return kanary.SourceResult(
                                measurements=[
                                    kanary.Measurement(
                                        name="a",
                                        value=1,
                                        timestamp=ctx["now"],
                                    )
                                ]
                            )

                    @kanary.rule(
                        rule_id="example.source.a.stale",
                        source="example.source",
                        severity=kanary.ERROR,
                        tags=["example"],
                    )
                    class ExampleRule(kanary.StaleRule):
                        measurement = "a"
                        timeout = 10

                    @kanary.output(output_id="example.output")
                    class ExampleOutput:

                        def emit(self, event, ctx):
                            return None
                    """
                )
            )

            loader = kanary.RuleDirectoryLoader(tmp)
            snapshot = loader.load()

        self.assertIn("example.source", snapshot.sources)
        self.assertIn("example.source.a.stale", snapshot.rules)
        self.assertIn("example.output", snapshot.outputs)

    def test_loads_python_files_from_multiple_rule_directories(self) -> None:
        with TemporaryDirectory() as tmp1, TemporaryDirectory() as tmp2:
            (Path(tmp1) / "source.py").write_text(
                textwrap.dedent(
                    """
                    import kanary

                    @kanary.source(source_id="example.source")
                    class ExampleSource:
                        def poll(self, ctx):
                            return kanary.SourceResult()
                    """
                )
            )
            (Path(tmp2) / "rule.py").write_text(
                textwrap.dedent(
                    """
                    import kanary

                    @kanary.rule(
                        rule_id="example.rule",
                        source="example.source",
                        severity=kanary.ERROR,
                        tags=["example"],
                    )
                    class ExampleRule:
                        def evaluate(self, payload, ctx):
                            return kanary.Evaluation(state=kanary.OK, payload=payload)
                    """
                )
            )
            loader = kanary.RuleDirectoryLoader([tmp1, tmp2])
            snapshot = loader.load()

        self.assertIn("example.source", snapshot.sources)
        self.assertIn("example.rule", snapshot.rules)

    def test_inspect_reports_rule_warnings_and_errors(self) -> None:
        with TemporaryDirectory() as tmp:
            rule_file = Path(tmp) / "rules.py"
            rule_file.write_text(
                textwrap.dedent(
                    """
                    import kanary

                    @kanary.source
                    class ExampleSource(kanary.Source):
                        source_id = "example.source"

                        def poll(self, ctx):
                            return kanary.SourceResult()

                    @kanary.rule
                    class WarningRule:
                        rule_id = "example.warning"
                        source = "example.source"
                        severity = kanary.ERROR
                        tags = []

                        def evaluate(self, payload, ctx):
                            return kanary.Evaluation(state=kanary.OK, payload=payload)

                    @kanary.output(
                        output_id="example.output",
                        include_states=["ACKED"],
                        exclude_states=["OK", "FIRING", "ACKED", "SUPPRESSED", "RESOLVED"],
                    )
                    class ExampleOutput:
                        def emit(self, event, ctx):
                            return None
                    """
                )
            )
            loader = kanary.RuleDirectoryLoader(tmp)
            _, report = loader.inspect()
        self.assertEqual(report.errors, [])
        self.assertEqual(
            report.warnings,
            [
                "rule 'example.warning' has no tags",
                "rule 'example.warning' has no owner",
                "rule 'example.warning' has no matching output",
            ],
        )

    def test_inspect_warns_when_owner_missing(self) -> None:
        with TemporaryDirectory() as tmp:
            rule_file = Path(tmp) / "rules.py"
            rule_file.write_text(
                textwrap.dedent(
                    """
                    import kanary

                    @kanary.source(source_id="example.source", interval=5.0)
                    class ExampleSource:
                        def poll(self, ctx):
                            return kanary.SourceResult()

                    @kanary.rule(
                        rule_id="example.rule",
                        source="example.source",
                        severity=kanary.ERROR,
                        tags=["example"],
                    )
                    class ExampleRule:
                        def evaluate(self, payload, ctx):
                            return kanary.Evaluation(state=kanary.OK, payload=payload)
                    """
                )
            )
            loader = kanary.RuleDirectoryLoader(tmp)
            _, report = loader.inspect()

        self.assertEqual(report.errors, [])
        self.assertIn("rule 'example.rule' has no owner", report.warnings)

    def test_inspect_rejects_rule_interval_shorter_than_source_interval(self) -> None:
        with TemporaryDirectory() as tmp:
            rule_file = Path(tmp) / "rules.py"
            rule_file.write_text(
                textwrap.dedent(
                    """
                    import kanary

                    @kanary.source(source_id="example.source", interval=10.0)
                    class ExampleSource:
                        def poll(self, ctx):
                            return kanary.SourceResult()

                    @kanary.rule(
                        rule_id="example.rule",
                        source="example.source",
                        severity=kanary.ERROR,
                        tags=["example"],
                        owner="expert",
                    )
                    class ExampleRule:
                        interval = 5.0

                        def evaluate(self, payload, ctx):
                            return kanary.Evaluation(state=kanary.OK, payload=payload)
                    """
                )
            )
            loader = kanary.RuleDirectoryLoader(tmp)
            _, report = loader.inspect()

        self.assertIn(
            "rule 'example.rule' interval 5s is shorter than source 'example.source' interval 10s",
            report.errors,
        )

    def test_inspect_warns_when_rule_interval_is_not_multiple_of_source_interval(self) -> None:
        with TemporaryDirectory() as tmp:
            rule_file = Path(tmp) / "rules.py"
            rule_file.write_text(
                textwrap.dedent(
                    """
                    import kanary

                    @kanary.source(source_id="example.source", interval=5.0)
                    class ExampleSource:
                        def poll(self, ctx):
                            return kanary.SourceResult()

                    @kanary.rule(
                        rule_id="example.rule",
                        source="example.source",
                        severity=kanary.ERROR,
                        tags=["example"],
                        owner="expert",
                    )
                    class ExampleRule:
                        interval = 12.0

                        def evaluate(self, payload, ctx):
                            return kanary.Evaluation(state=kanary.OK, payload=payload)
                    """
                )
            )
            loader = kanary.RuleDirectoryLoader(tmp)
            _, report = loader.inspect()

        self.assertEqual(report.errors, [])
        self.assertIn(
            "rule 'example.rule' interval 12s is not a multiple of source 'example.source' interval 5s",
            report.warnings,
        )

    def test_inspect_sets_matched_outputs_on_rule_classes(self) -> None:
        with TemporaryDirectory() as tmp:
            rule_file = Path(tmp) / "rules.py"
            rule_file.write_text(
                textwrap.dedent(
                    """
                    import kanary

                    @kanary.source(source_id="example.source")
                    class ExampleSource:
                        def poll(self, ctx):
                            return kanary.SourceResult()

                    @kanary.rule(
                        rule_id="example.rule",
                        source="example.source",
                        severity=kanary.ERROR,
                        tags=["sqlite", "infra"],
                        owner="expert",
                    )
                    class ExampleRule:
                        def evaluate(self, payload, ctx):
                            return kanary.Evaluation(state=kanary.OK, payload=payload)

                    @kanary.output(output_id="match-by-tag", include_tags=["infra"])
                    class MatchByTag:
                        def emit(self, event, ctx):
                            return None

                    @kanary.output(output_id="match-by-state", include_states=["ACKED"])
                    class MatchByState:
                        def emit(self, event, ctx):
                            return None

                    @kanary.output(output_id="match-all")
                    class MatchAll:
                        def emit(self, event, ctx):
                            return None
                    """
                )
            )
            loader = kanary.RuleDirectoryLoader(tmp)
            snapshot, report = loader.inspect()
        self.assertEqual(report.errors, [])
        self.assertEqual(report.warnings, [])
        self.assertEqual(snapshot.rules["example.rule"].matched_outputs, ["match-by-tag", "match-by-state", "match-all"])

    def test_inspect_rejects_plugin_id_collisions_across_types(self) -> None:
        with TemporaryDirectory() as tmp:
            rule_file = Path(tmp) / "rules.py"
            rule_file.write_text(
                textwrap.dedent(
                    """
                    import kanary

                    @kanary.source(source_id="shared.plugin")
                    class ExampleSource:
                        def poll(self, ctx):
                            return kanary.SourceResult()

                    @kanary.rule(
                        rule_id="shared.plugin",
                        source="shared.plugin",
                        severity=kanary.ERROR,
                        tags=["example"],
                    )
                    class ExampleRule:
                        def evaluate(self, payload, ctx):
                            return kanary.Evaluation(state=kanary.OK, payload=payload)
                    """
                )
            )
            loader = kanary.RuleDirectoryLoader(tmp)
            _, report = loader.inspect()

        self.assertIn(
            "plugin id 'shared.plugin' must be unique across rule/source/output (used by source, rule)",
            report.errors,
        )

    def test_inspect_rejects_duplicate_source_and_output_ids(self) -> None:
        with TemporaryDirectory() as tmp:
            rule_file = Path(tmp) / "plugins.py"
            rule_file.write_text(
                textwrap.dedent(
                    """
                    import kanary

                    @kanary.source(source_id="dup.source")
                    class ExampleSource1:
                        def poll(self, ctx):
                            return kanary.SourceResult()

                    @kanary.source(source_id="dup.source")
                    class ExampleSource2:
                        def poll(self, ctx):
                            return kanary.SourceResult()

                    @kanary.output(output_id="dup.output")
                    class ExampleOutput1:
                        def emit(self, event, ctx):
                            return None

                    @kanary.output(output_id="dup.output")
                    class ExampleOutput2:
                        def emit(self, event, ctx):
                            return None
                    """
                )
            )
            loader = kanary.RuleDirectoryLoader(tmp)
            _, report = loader.inspect()

        self.assertEqual(len(report.errors), 2)
        self.assertIn("duplicate source_id 'dup.source' defined by", report.errors[0])
        self.assertIn("ExampleSource1", report.errors[0])
        self.assertIn("ExampleSource2", report.errors[0])
        self.assertIn("duplicate output_id 'dup.output' defined by", report.errors[1])
        self.assertIn("ExampleOutput1", report.errors[1])
        self.assertIn("ExampleOutput2", report.errors[1])


class RuntimeExcludeTest(unittest.TestCase):
    def test_exclude_can_remove_source_and_dependent_rules_and_outputs(self) -> None:
        with TemporaryDirectory() as tmp:
            rules_file = Path(tmp) / "rules.py"
            rules_file.write_text(
                textwrap.dedent(
                    """
                    import kanary

                    @kanary.source(source_id="keep.source")
                    class KeepSource:
                        def poll(self, ctx):
                            return kanary.SourceResult()

                    @kanary.source(source_id="drop.source")
                    class DropSource:
                        def poll(self, ctx):
                            return kanary.SourceResult()

                    @kanary.rule(
                        rule_id="keep.rule",
                        source="keep.source",
                        severity=kanary.ERROR,
                        tags=["keep"],
                    )
                    class KeepRule:
                        def evaluate(self, payload, ctx):
                            return kanary.Evaluation(state=kanary.OK, payload=payload)

                    @kanary.rule(
                        rule_id="drop.rule",
                        source="drop.source",
                        severity=kanary.ERROR,
                        tags=["drop"],
                    )
                    class DropRule:
                        def evaluate(self, payload, ctx):
                            return kanary.Evaluation(state=kanary.OK, payload=payload)

                    @kanary.output(output_id="keep.output")
                    class KeepOutput:
                        def emit(self, event, ctx):
                            return None

                    @kanary.output(output_id="drop.output")
                    class DropOutput:
                        def emit(self, event, ctx):
                            return None
                    """
                )
            )
            runtime = EngineRuntime(
                RuntimeConfig(
                    rule_directories=[Path(tmp)],
                    api_port=0,
                    exclude_plugins=["drop.*"],
                )
            )
            snapshot = runtime._apply_excludes(runtime.loader.load())

        self.assertEqual(set(snapshot.sources), {"keep.source"})
        self.assertEqual(set(snapshot.rules), {"keep.rule"})
        self.assertEqual(set(snapshot.outputs), {"keep.output"})

    def test_exclude_recomputes_matched_outputs(self) -> None:
        with TemporaryDirectory() as tmp:
            rules_file = Path(tmp) / "rules.py"
            rules_file.write_text(
                textwrap.dedent(
                    """
                    import kanary

                    @kanary.source(source_id="example.source")
                    class ExampleSource:
                        def poll(self, ctx):
                            return kanary.SourceResult()

                    @kanary.rule(
                        rule_id="example.rule",
                        source="example.source",
                        severity=kanary.ERROR,
                        tags=["example"],
                    )
                    class ExampleRule:
                        def evaluate(self, payload, ctx):
                            return kanary.Evaluation(state=kanary.OK, payload=payload)

                    @kanary.output(output_id="console")
                    class ConsoleOutput:
                        def emit(self, event, ctx):
                            return None

                    @kanary.output(output_id="discord")
                    class DiscordOutput:
                        def emit(self, event, ctx):
                            return None
                    """
                )
            )
            runtime = EngineRuntime(
                RuntimeConfig(
                    rule_directories=[Path(tmp)],
                    api_port=0,
                    exclude_plugins=["console"],
                )
            )
            snapshot = runtime.loader.load(exclude_patterns=runtime.config.exclude_plugins)

        self.assertEqual(set(snapshot.outputs), {"discord"})
        self.assertEqual(snapshot.rules["example.rule"].matched_outputs, ["discord"])


class ControlAPITest(unittest.TestCase):
    def test_alerts_and_plugins_include_definition_file(self) -> None:
        engine = kanary.Engine(output_registry={})
        engine.start()
        api = kanary.ControlAPI(
            engine_getter=lambda: engine,
            reload_callback=lambda: True,
            host="127.0.0.1",
            port=0,
        )
        thread = threading.Thread(target=api.start, daemon=True)
        thread.start()
        try:
            source = engine.sources["postgres"]
            engine.evaluate_source(source.source_id, source.poll({}), now=datetime(2026, 3, 17, 0, 20, tzinfo=timezone.utc))
            port = api._server.server_address[1]
            alerts_payload = fetch_json(f"http://127.0.0.1:{port}/alerts")
            plugins_payload = fetch_json(f"http://127.0.0.1:{port}/plugins")
        finally:
            api.shutdown()
            thread.join(timeout=2.0)
            engine.shutdown()

        alert = next(item for item in alerts_payload["alerts"] if item["rule_id"] == "postgres.temperature.stale")
        self.assertTrue(str(alert["definition_file"]).endswith("tests/test_engine.py"))
        self.assertIn("matched_outputs", alert)

        source_plugin = next(
            item
            for item in plugins_payload["plugins"]
            if item["type"] == "source" and item["plugin_id"] == "postgres"
        )
        rule_plugin = next(
            item
            for item in plugins_payload["plugins"]
            if item["type"] == "rule" and item["plugin_id"] == "postgres.temperature.stale"
        )
        self.assertTrue(str(source_plugin["definition_file"]).endswith("tests/test_engine.py"))
        self.assertTrue(str(rule_plugin["definition_file"]).endswith("tests/test_engine.py"))

    def test_viewer_assets_are_served(self) -> None:
        engine = kanary.Engine(output_registry={})
        engine.start()
        api = kanary.ControlAPI(
            engine_getter=lambda: engine,
            reload_callback=lambda: True,
            host="127.0.0.1",
            port=0,
        )
        thread = threading.Thread(target=api.start, daemon=True)
        thread.start()
        try:
            port = api._server.server_address[1]
            with urlopen(f"http://127.0.0.1:{port}/viewer") as response:
                body = response.read().decode()
            self.assertIn("KANARY Viewer", body)

            with urlopen(f"http://127.0.0.1:{port}/viewer/app.js") as response:
                javascript = response.read().decode()
            self.assertIn("DEFAULT_REFRESH_MS", javascript)
            self.assertIn("Dashboard", body)
        finally:
            api.shutdown()
            thread.join(timeout=2.0)
            engine.shutdown()

    def test_peer_status_is_served(self) -> None:
        engine = kanary.Engine(output_registry={}, node_id="node-a")
        engine.start()
        api = kanary.ControlAPI(
            engine_getter=lambda: engine,
            reload_callback=lambda: True,
            host="127.0.0.1",
            port=0,
        )
        thread = threading.Thread(target=api.start, daemon=True)
        thread.start()
        try:
            port = api._server.server_address[1]
            payload = fetch_json(f"http://127.0.0.1:{port}/peer-status")
        finally:
            api.shutdown()
            thread.join(timeout=2.0)
            engine.shutdown()

        self.assertEqual(payload["status"], "ok")
        self.assertIn("generated_at", payload)
        self.assertIn("started_at", payload)
        self.assertIn("uptime_seconds", payload)
        self.assertIn("counts", payload)
        self.assertIn("alert_states", payload)
        self.assertIn("failed_plugins", payload["counts"])
        self.assertEqual(payload["node_id"], "node-a")

    def test_alerts_endpoint_includes_tags_and_owner(self) -> None:
        engine = kanary.Engine(output_registry={})
        engine.start()
        api = kanary.ControlAPI(
            engine_getter=lambda: engine,
            reload_callback=lambda: True,
            host="127.0.0.1",
            port=0,
        )
        thread = threading.Thread(target=api.start, daemon=True)
        thread.start()
        try:
            source = engine.sources["postgres"]
            now = datetime(2026, 3, 17, 0, 20, tzinfo=timezone.utc)
            engine.evaluate_source(source.source_id, source.poll({}), now=now)
            port = api._server.server_address[1]
            payload = fetch_json(f"http://127.0.0.1:{port}/alerts")
        finally:
            api.shutdown()
            thread.join(timeout=2.0)
            engine.shutdown()

        alert_row = next(row for row in payload["alerts"] if row["rule_id"] == "postgres.temperature.stale")
        self.assertEqual(alert_row["owner"], "expert_db")
        self.assertEqual(alert_row["tags"], ["infra", "postgres"])

    def test_export_alerts_endpoint_includes_origin_metadata(self) -> None:
        engine = kanary.Engine(output_registry={}, node_id="node-a")
        engine.start()
        api = kanary.ControlAPI(
            engine_getter=lambda: engine,
            reload_callback=lambda: True,
            host="127.0.0.1",
            port=0,
        )
        thread = threading.Thread(target=api.start, daemon=True)
        thread.start()
        try:
            source = engine.sources["postgres"]
            now = datetime(2026, 3, 17, 0, 20, tzinfo=timezone.utc)
            engine.evaluate_source(source.source_id, source.poll({}), now=now)
            port = api._server.server_address[1]
            payload = fetch_json(f"http://127.0.0.1:{port}/export-alerts")
        finally:
            api.shutdown()
            thread.join(timeout=2.0)
            engine.shutdown()

        self.assertEqual(payload["node_id"], "node-a")
        alert_row = next(row for row in payload["alerts"] if row["rule_id"] == "postgres.temperature.stale")
        self.assertEqual(alert_row["origin_node_id"], "node-a")
        self.assertEqual(alert_row["origin_rule_id"], "postgres.temperature.stale")
        self.assertEqual(alert_row["mirror_path"], ["node-a"])
        self.assertFalse(alert_row["is_mirrored"])

    def test_plugin_source_endpoint_returns_class_source(self) -> None:
        engine = kanary.Engine(output_registry={})
        engine.start()
        api = kanary.ControlAPI(
            engine_getter=lambda: engine,
            reload_callback=lambda: True,
            host="127.0.0.1",
            port=0,
        )
        thread = threading.Thread(target=api.start, daemon=True)
        thread.start()
        try:
            port = api._server.server_address[1]
            payload = fetch_json(
                f"http://127.0.0.1:{port}/plugins/rule/postgres.temperature.stale/source"
            )
        finally:
            api.shutdown()
            thread.join(timeout=2.0)
            engine.shutdown()

        self.assertEqual(payload["plugin_id"], "postgres.temperature.stale")
        self.assertEqual(payload["type"], "rule")
        self.assertEqual(payload["symbol_name"], "SlowPostgresStale")
        self.assertEqual(payload["mode"], "class")
        self.assertGreaterEqual(payload["start_line"], 1)
        self.assertIn("class SlowPostgresStale", payload["source_text"])

    def test_ack_and_silence_api(self) -> None:
        engine = kanary.Engine(output_registry={})
        engine.start()
        api = kanary.ControlAPI(
            engine_getter=lambda: engine,
            reload_callback=lambda: True,
            host="127.0.0.1",
            port=0,
        )
        thread = threading.Thread(target=api.start, daemon=True)
        thread.start()
        try:
            source = engine.sources["postgres"]
            now = datetime(2026, 3, 17, 0, 20, tzinfo=timezone.utc)
            engine.evaluate_source(source.source_id, source.poll({}), now=now)
            port = api._server.server_address[1]

            ack_payload = fetch_json(
                f"http://127.0.0.1:{port}/alerts/postgres.temperature.stale/ack",
                method="POST",
                body={"operator": "alice", "reason": "checking"},
            )
            self.assertEqual(ack_payload["status"], "acked")

            unack_payload = fetch_json(
                f"http://127.0.0.1:{port}/alerts/postgres.temperature.stale/unack",
                method="POST",
                body={"operator": "alice", "reason": "re-open"},
            )
            self.assertEqual(unack_payload["status"], "unacked")

            silence_payload = fetch_json(
                f"http://127.0.0.1:{port}/silences/duration",
                method="POST",
                body={
                    "operator": "alice",
                    "reason": "maint",
                    "duration_minutes": 10,
                    "rule_patterns": ["postgres.temperature.stale"],
                },
            )
            self.assertIn("silence_id", silence_payload)
            self.assertEqual(silence_payload.get("warnings"), [])

            silences_payload = fetch_json(f"http://127.0.0.1:{port}/silences")
            self.assertEqual(len(silences_payload["silences"]), 1)
            self.assertTrue(silences_payload["silences"][0]["active"])
        finally:
            api.shutdown()
            thread.join(timeout=2.0)
            engine.shutdown()

    def test_silence_api_returns_broad_target_warning(self) -> None:
        engine = kanary.Engine(output_registry={})
        engine.start()
        api = kanary.ControlAPI(
            engine_getter=lambda: engine,
            reload_callback=lambda: True,
            host="127.0.0.1",
            port=0,
        )
        thread = threading.Thread(target=api.start, daemon=True)
        thread.start()
        try:
            port = api._server.server_address[1]
            silence_payload = fetch_json(
                f"http://127.0.0.1:{port}/silences/duration",
                method="POST",
                body={
                    "operator": "alice",
                    "reason": "broad",
                    "duration_minutes": 10,
                    "rule_patterns": ["*"],
                },
            )
        finally:
            api.shutdown()
            thread.join(timeout=2.0)
            engine.shutdown()

        self.assertIn("silence_id", silence_payload)
        self.assertIn("silence target uses a very broad wildcard pattern", silence_payload["warnings"])

    def test_remote_source_and_rule_can_mirror_alerts(self) -> None:
        remote_engine = kanary.Engine(output_registry={}, node_id="remote-a")
        remote_engine.start()
        remote_api = kanary.ControlAPI(
            engine_getter=lambda: remote_engine,
            reload_callback=lambda: True,
            host="127.0.0.1",
            port=0,
        )
        remote_thread = threading.Thread(target=remote_api.start, daemon=True)
        remote_thread.start()
        local_engine = None
        try:
            remote_source = remote_engine.sources["postgres"]
            now = datetime(2026, 3, 17, 0, 20, tzinfo=timezone.utc)
            remote_engine.evaluate_source(remote_source.source_id, remote_source.poll({}), now=now)
            RemoteAPISource.url = f"http://127.0.0.1:{remote_api._server.server_address[1]}"
            local_engine = kanary.Engine(
                now_fn=lambda: now,
                source_registry={"remote-api": RemoteAPISource},
                rule_registry={"mirror.postgres.temperature.stale": MirroredTemperatureStale},
                output_registry={},
                node_id="local-a",
            )
            local_engine.start()
            local_source = local_engine.sources["remote-api"]
            alerts = local_engine.evaluate_source(
                local_source.source_id,
                local_source.poll({"engine": local_engine}),
                now=now,
            )
        finally:
            remote_api.shutdown()
            remote_thread.join(timeout=2.0)
            remote_engine.shutdown()
            if local_engine is not None:
                local_engine.shutdown()

        mirrored = alerts["mirror.postgres.temperature.stale"]
        self.assertEqual(mirrored.state, kanary.AlertState.FIRING)
        self.assertEqual(mirrored.severity, kanary.ERROR)
        self.assertIn("remote_alarm", mirrored.payload)
        self.assertEqual(mirrored.payload["remote_alarm"]["origin_node_id"], "remote-a")
        self.assertEqual(mirrored.payload["remote_alarm"]["mirror_path"], ["remote-a"])

    def test_remote_source_skips_alerts_that_already_include_local_node(self) -> None:
        remote_engine = kanary.Engine(output_registry={}, node_id="shared-node")
        remote_engine.start()
        remote_api = kanary.ControlAPI(
            engine_getter=lambda: remote_engine,
            reload_callback=lambda: True,
            host="127.0.0.1",
            port=0,
        )
        remote_thread = threading.Thread(target=remote_api.start, daemon=True)
        remote_thread.start()
        local_engine = None
        try:
            remote_source = remote_engine.sources["postgres"]
            now = datetime(2026, 3, 17, 0, 20, tzinfo=timezone.utc)
            remote_engine.evaluate_source(remote_source.source_id, remote_source.poll({}), now=now)
            RemoteAPISource.url = f"http://127.0.0.1:{remote_api._server.server_address[1]}"
            local_engine = kanary.Engine(
                now_fn=lambda: now,
                source_registry={"remote-api": RemoteAPISource},
                rule_registry={"mirror.postgres.temperature.stale": MirroredTemperatureStale},
                output_registry={},
                node_id="shared-node",
            )
            local_engine.start()
            local_source = local_engine.sources["remote-api"]
            alerts = local_engine.evaluate_source(
                local_source.source_id,
                local_source.poll({"engine": local_engine}),
                now=now,
            )
        finally:
            remote_api.shutdown()
            remote_thread.join(timeout=2.0)
            remote_engine.shutdown()
            if local_engine is not None:
                local_engine.shutdown()

        self.assertEqual(alerts, {})

    def test_remote_alarm_can_propagate_ack_and_silence(self) -> None:
        remote_engine = kanary.Engine(output_registry={})
        remote_engine.start()
        remote_api = kanary.ControlAPI(
            engine_getter=lambda: remote_engine,
            reload_callback=lambda: True,
            host="127.0.0.1",
            port=0,
        )
        remote_thread = threading.Thread(target=remote_api.start, daemon=True)
        remote_thread.start()
        local_engine = None
        try:
            remote_source = remote_engine.sources["postgres"]
            now = datetime(2026, 3, 17, 0, 20, tzinfo=timezone.utc)
            remote_engine.evaluate_source(remote_source.source_id, remote_source.poll({}), now=now)
            RemoteAPISource.url = f"http://127.0.0.1:{remote_api._server.server_address[1]}"
            local_engine = kanary.Engine(
                now_fn=lambda: now,
                source_registry={"remote-api": RemoteAPISource},
                rule_registry={"mirror.postgres.temperature.stale": MirroredTemperatureStale},
                output_registry={},
            )
            local_engine.start()
            local_source = local_engine.sources["remote-api"]
            local_engine.evaluate_source(
                local_source.source_id,
                local_source.poll({"engine": local_engine}),
                now=now,
            )

            local_engine.acknowledge(
                "mirror.postgres.temperature.stale",
                operator="operator_name",
                reason="checking",
            )
            self.assertEqual(
                remote_engine.alerts["postgres.temperature.stale"].state,
                kanary.AlertState.ACKED,
            )

            silence = local_engine.create_silence(
                operator="operator_name",
                reason="maintenance",
                start_at=now,
                end_at=now + timedelta(minutes=10),
                rule_patterns=["mirror.postgres.temperature.stale"],
            )
            self.assertEqual(len(remote_engine.list_silences()), 1)
            self.assertEqual(len(silence.remote_silence_refs), 1)

            local_engine.cancel_silence(
                silence.silence_id,
                operator="operator_name",
                reason="done",
            )
            remote_silence = remote_engine.list_silences()[0]
            self.assertIsNotNone(remote_silence.cancelled_at)
        finally:
            remote_api.shutdown()
            remote_thread.join(timeout=2.0)
            remote_engine.shutdown()
            if local_engine is not None:
                local_engine.shutdown()

    def test_remote_alarm_can_unack_remote_acknowledgement(self) -> None:
        remote_engine = kanary.Engine(output_registry={})
        remote_engine.start()
        remote_api = kanary.ControlAPI(
            engine_getter=lambda: remote_engine,
            reload_callback=lambda: True,
            host="127.0.0.1",
            port=0,
        )
        remote_thread = threading.Thread(target=remote_api.start, daemon=True)
        remote_thread.start()
        local_engine = None
        try:
            remote_source = remote_engine.sources["postgres"]
            now = datetime(2026, 3, 17, 0, 20, tzinfo=timezone.utc)
            remote_engine.evaluate_source(remote_source.source_id, remote_source.poll({}), now=now)
            remote_engine.acknowledge(
                "postgres.temperature.stale",
                operator="operator_name",
                reason="remote ack",
            )
            RemoteAPISource.url = f"http://127.0.0.1:{remote_api._server.server_address[1]}"
            local_engine = kanary.Engine(
                now_fn=lambda: now,
                source_registry={"remote-api": RemoteAPISource},
                rule_registry={"mirror.postgres.temperature.stale": MirroredTemperatureStale},
                output_registry={},
            )
            local_engine.start()
            local_source = local_engine.sources["remote-api"]
            local_engine.evaluate_source(
                local_source.source_id,
                local_source.poll({"engine": local_engine}),
                now=now,
            )
            self.assertEqual(
                local_engine.alerts["mirror.postgres.temperature.stale"].state,
                kanary.AlertState.ACKED,
            )

            local_engine.unacknowledge(
                "mirror.postgres.temperature.stale",
                operator="operator_name",
                reason="re-open",
            )
            self.assertEqual(
                remote_engine.alerts["postgres.temperature.stale"].state,
                kanary.AlertState.FIRING,
            )
        finally:
            remote_api.shutdown()
            remote_thread.join(timeout=2.0)
            remote_engine.shutdown()
            if local_engine is not None:
                local_engine.shutdown()


class SQLiteStoreTest(unittest.TestCase):
    def test_store_restores_acknowledgements_and_silences(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "kanary.db"
            now = datetime(2026, 3, 17, 0, 20, tzinfo=timezone.utc)

            engine = kanary.Engine(
                now_fn=lambda: now,
                source_registry={"postgres": SlowPostgresSource},
                rule_registry={
                    "postgres.temperature.stale": SlowPostgresStale,
                    "postgres.temperature.range": SlowPostgresHighValue,
                    "postgres.humidity.range": SlowPostgresExclusiveRange,
                    "postgres.humidity.suppressed_range": SuppressedByTemperatureRange,
                    "postgres.temperature.rate": TemperatureRate,
                    "postgres.temperature_humidity.balance": TemperatureHumidityBalance,
                },
                output_registry={},
                store=kanary.SQLiteStore(db_path),
            )
            engine.start()
            try:
                source = engine.sources["postgres"]
                engine.evaluate_source(source.source_id, source.poll({}), now=now)
                engine.acknowledge("postgres.temperature.stale", operator="alice", reason="investigating")
                engine.create_silence(
                    operator="alice",
                    reason="maintenance",
                    start_at=now - timedelta(minutes=1),
                    end_at=now + timedelta(minutes=10),
                    rule_patterns=["postgres.temperature.stale"],
                )
            finally:
                engine.shutdown()

            restored = kanary.Engine(
                now_fn=lambda: now,
                source_registry={"postgres": SlowPostgresSource},
                rule_registry={
                    "postgres.temperature.stale": SlowPostgresStale,
                    "postgres.temperature.range": SlowPostgresHighValue,
                    "postgres.humidity.range": SlowPostgresExclusiveRange,
                    "postgres.humidity.suppressed_range": SuppressedByTemperatureRange,
                    "postgres.temperature.rate": TemperatureRate,
                    "postgres.temperature_humidity.balance": TemperatureHumidityBalance,
                },
                output_registry={},
                store=kanary.SQLiteStore(db_path),
            )
            restored.start()
            try:
                self.assertIn("postgres.temperature.stale", restored.acknowledgements)
                self.assertEqual(restored.acknowledgements["postgres.temperature.stale"].operator, "alice")
                self.assertEqual(len(restored.silences), 1)

                source = restored.sources["postgres"]
                alerts = restored.evaluate_source(source.source_id, source.poll({}), now=now)
                self.assertEqual(alerts["postgres.temperature.stale"].state, kanary.AlertState.SILENCED)
            finally:
                restored.shutdown()

    def test_history_api_reads_from_sqlite_store(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "kanary.db"
            now = datetime(2026, 3, 17, 0, 20, tzinfo=timezone.utc)

            engine = kanary.Engine(
                now_fn=lambda: now,
                source_registry={"postgres": SlowPostgresSource},
                rule_registry={
                    "postgres.temperature.stale": SlowPostgresStale,
                    "postgres.temperature.range": SlowPostgresHighValue,
                    "postgres.humidity.range": SlowPostgresExclusiveRange,
                    "postgres.humidity.suppressed_range": SuppressedByTemperatureRange,
                    "postgres.temperature.rate": TemperatureRate,
                    "postgres.temperature_humidity.balance": TemperatureHumidityBalance,
                },
                output_registry={},
                store=kanary.SQLiteStore(db_path),
            )
            engine.start()
            api = kanary.ControlAPI(
                engine_getter=lambda: engine,
                reload_callback=lambda: True,
                host="127.0.0.1",
                port=0,
            )
            thread = threading.Thread(target=api.start, daemon=True)
            thread.start()
            try:
                source = engine.sources["postgres"]
                engine.evaluate_source(source.source_id, source.poll({}), now=now)
                engine.acknowledge("postgres.temperature.stale", operator="alice", reason="checking")
                engine.unacknowledge("postgres.temperature.stale", operator="alice", reason="re-open")
                port = api._server.server_address[1]
                payload = fetch_json(f"http://127.0.0.1:{port}/history/postgres.temperature.stale")
                self.assertTrue(payload["enabled"])
                self.assertGreaterEqual(len(payload["alert_events"]), 2)
                self.assertEqual(payload["operator_actions"][0]["action_type"], "unack")
                self.assertEqual(payload["operator_actions"][0]["operator"], "alice")
            finally:
                api.shutdown()
                thread.join(timeout=2.0)
                engine.shutdown()


class RecordingOutput(kanary.Output):
    output_id = "recording"
    events = []

    def emit(self, event, ctx):
        self.events.append(event)


class BrokenInitOutput(kanary.Output):
    output_id = "broken-init"

    def init(self, ctx):
        raise RuntimeError("webhook is not set")


class BrokenEmitOutput(kanary.Output):
    output_id = "broken-emit"

    def emit(self, event, ctx):
        raise RuntimeError("send failed")


class OutputTest(unittest.TestCase):
    def setUp(self) -> None:
        RecordingOutput.events = []
        self.now = datetime(2026, 3, 17, 0, 20, tzinfo=timezone.utc)
        self.engine = kanary.Engine(
            now_fn=lambda: self.now,
            output_registry={"recording": RecordingOutput},
        )
        self.engine.start()

    def tearDown(self) -> None:
        self.engine.shutdown()

    def test_output_plugin_receives_alert_event_on_state_change(self) -> None:
        source = self.engine.sources["postgres"]
        self.engine.evaluate_source(source.source_id, source.poll({}), now=self.now)
        self.assertEqual(len(RecordingOutput.events), 0)

        source.now = self.now - timedelta(seconds=10)
        self.engine.evaluate_source(source.source_id, source.poll({}), now=self.now)

        self.assertGreaterEqual(len(RecordingOutput.events), 1)
        self.assertEqual(RecordingOutput.events[0].rule_id, "postgres.temperature.stale")

    def test_reload_suppresses_first_notification_after_reload(self) -> None:
        source = self.engine.sources["postgres"]
        self.engine.evaluate_source(source.source_id, source.poll({}), now=self.now)
        self.assertEqual(len(RecordingOutput.events), 0)

        self.engine.reload(
            source_registry=kanary.get_source_registry(),
            rule_registry=kanary.get_rule_registry(),
            output_registry={"recording": RecordingOutput},
        )
        source = self.engine.sources["postgres"]
        source.now = self.now - timedelta(seconds=10)
        self.engine.evaluate_source(source.source_id, source.poll({}), now=self.now)

        self.assertEqual(len(RecordingOutput.events), 0)

    def test_output_plugin_does_not_emit_on_message_only_change(self) -> None:
        source = self.engine.sources["postgres"]
        self.engine.evaluate_source(source.source_id, source.poll({}), now=self.now)
        first_count = len(RecordingOutput.events)

        self.now = self.now + timedelta(seconds=5)
        self.engine.evaluate_source(source.source_id, source.poll({}), now=self.now)

        self.assertEqual(len(RecordingOutput.events), first_count)

    def test_output_init_failure_is_recorded_without_crashing_engine(self) -> None:
        engine = kanary.Engine(
            now_fn=lambda: self.now,
            output_registry={"broken-init": BrokenInitOutput},
        )
        engine.start()
        try:
            status = engine.plugin_states["output:broken-init"]
            self.assertEqual(status.state, "failed")
            self.assertFalse(status.init_ok)
            self.assertEqual(status.last_error, "webhook is not set")
            self.assertIsNotNone(status.last_updated_at)
        finally:
            engine.shutdown()

    def test_output_emit_failure_is_recorded(self) -> None:
        engine = kanary.Engine(
            now_fn=lambda: self.now,
            output_registry={"broken-emit": BrokenEmitOutput},
        )
        engine.start()
        try:
            source = engine.sources["postgres"]
            engine.evaluate_source(source.source_id, source.poll({}), now=self.now)
            source.now = self.now - timedelta(seconds=10)
            engine.evaluate_source(source.source_id, source.poll({}), now=self.now)
            status = engine.plugin_states["output:broken-emit"]
            self.assertEqual(status.state, "failed")
            self.assertTrue(status.init_ok)
            self.assertEqual(status.last_error, "send failed")
            self.assertIsNotNone(status.last_failure_at)
            self.assertIsNotNone(status.last_updated_at)
        finally:
            engine.shutdown()


class RemoteAlarmFactoryTest(unittest.TestCase):
    def test_factory_can_generate_prefixed_remote_alarm_rules(self) -> None:
        remote_engine = kanary.Engine(output_registry={})
        remote_engine.start()
        remote_api = kanary.ControlAPI(
            engine_getter=lambda: remote_engine,
            reload_callback=lambda: True,
            host="127.0.0.1",
            port=0,
        )
        remote_thread = threading.Thread(target=remote_api.start, daemon=True)
        remote_thread.start()
        try:
            source = remote_engine.sources["postgres"]
            now = datetime(2026, 3, 17, 0, 20, tzinfo=timezone.utc)
            remote_engine.evaluate_source(source.source_id, source.poll({}), now=now)
            RemoteAPISource.url = f"http://127.0.0.1:{remote_api._server.server_address[1]}"
            generated = kanary.import_remote_alarms(
                source="remote-api",
                prefix="imported",
                add_tags=["remote"],
                include_rule_ids=["postgres.temperature.*"],
                exclude_rule_ids=["*.rate"],
            )
        finally:
            remote_api.shutdown()
            remote_thread.join(timeout=2.0)
            remote_engine.shutdown()

        generated_ids = {cls.rule_id for cls in generated}
        self.assertIn("imported.postgres.temperature.stale", generated_ids)
        self.assertIn("imported.postgres.temperature.range", generated_ids)
        self.assertNotIn("imported.postgres.temperature.rate", generated_ids)
        generated_rule = next(cls for cls in generated if cls.rule_id == "imported.postgres.temperature.stale")
        self.assertIn("remote", generated_rule.tags)
        self.assertIn("postgres", generated_rule.tags)


class ControlAPITest(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 3, 17, 0, 20, tzinfo=timezone.utc)
        self.engine = kanary.Engine(now_fn=lambda: self.now, output_registry={})
        self.engine.start()
        source = self.engine.sources["postgres"]
        self.engine.evaluate_source(source.source_id, source.poll({}), now=self.now)
        self.api = kanary.ControlAPI(
            engine_getter=lambda: self.engine,
            reload_callback=lambda: True,
            port=18080,
        )
        self.thread = threading.Thread(target=self.api.start, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.api.shutdown()
        self.thread.join(timeout=2.0)
        self.engine.shutdown()

    def test_health_endpoint_returns_ok(self) -> None:
        with urlopen("http://127.0.0.1:18080/health") as response:
            body = json.loads(response.read().decode())
        self.assertEqual(body["status"], "ok")
        self.assertIn("postgres", body["sources"])

    def test_alerts_endpoint_returns_alerts(self) -> None:
        with urlopen("http://127.0.0.1:18080/alerts") as response:
            body = json.loads(response.read().decode())
        self.assertEqual(len(body["alerts"]), 8)

    def test_reload_endpoint_returns_reloaded(self) -> None:
        request = Request("http://127.0.0.1:18080/reload", method="POST")
        with urlopen(request) as response:
            body = json.loads(response.read().decode())
        self.assertEqual(body["status"], "reloaded")

    def test_plugins_endpoint_returns_output_status(self) -> None:
        with urlopen("http://127.0.0.1:18080/plugins") as response:
            body = json.loads(response.read().decode())
        self.assertEqual(len(body["plugins"]), len(self.engine.plugin_states))
        self.assertIn("source", {plugin["type"] for plugin in body["plugins"]})
        self.assertIn("rule", {plugin["type"] for plugin in body["plugins"]})
        plugin_ids = {plugin["plugin_id"] for plugin in body["plugins"]}
        self.assertIn("postgres", plugin_ids)
        self.assertIn("postgres.temperature.stale", plugin_ids)
        self.assertTrue(all("last_updated_at" in plugin for plugin in body["plugins"]))


if __name__ == "__main__":
    unittest.main()
