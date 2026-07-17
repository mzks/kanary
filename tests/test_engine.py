import argparse
from datetime import datetime, timedelta, timezone
import importlib
import json
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import textwrap
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import kanary
from kanary import ctl as kanaryctl
from kanary import __main__ as kanary_main
import kanaryctl as kanaryctl_module
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
    description = "Expose synthetic PostgreSQL temperature and humidity inputs for API tests."

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
    inputs="postgres:temperature",
    severity=kanary.ERROR,
    tags=["infra", "postgres"],
    owner="expert_db",
)
class SlowPostgresStale(kanary.StaleRule):
    description = "Alert when the synthetic PostgreSQL temperature input stops updating."
    runbook = "https://example.invalid/runbooks/postgres-temperature-stale"

    timeout = 10 * kanary.minute


@kanary.rule(
    rule_id="postgres.temperature.range",
    inputs="postgres:temperature",
    severity=kanary.WARN,
    tags=["infra", "postgres"],
    owner="expert_db",
)
class SlowPostgresHighValue(kanary.RangeRule):
    high = 100
    hysteresis = 5.0


@kanary.rule(
    rule_id="postgres.humidity.range",
    inputs="postgres:humidity",
    severity=kanary.WARN,
    tags=["infra", "postgres"],
    owner="expert_db",
)
class SlowPostgresExclusiveRange(kanary.RangeRule):
    low = 45
    high = 50
    lower_inclusive = False
    upper_inclusive = False


@kanary.rule(
    rule_id="postgres.humidity.suppressed_range",
    inputs="postgres:humidity",
    severity=kanary.WARN,
    tags=["infra", "postgres"],
    owner="expert_db",
)
class SuppressedByTemperatureRange(kanary.RangeRule):
    low = 40
    high = 50
    suppressed_by = ["postgres.temperature.range"]


@kanary.rule(
    rule_id="postgres.temperature.rate",
    inputs="postgres:temperature",
    severity=kanary.WARN,
    tags=["infra", "postgres"],
    owner="expert_db",
)
class TemperatureRate(kanary.RateRule):
    low = -1.0
    high = 0.5
    per_seconds = 1 * kanary.minute


@kanary.rule(
    rule_id="postgres.temperature.threshold",
    inputs="postgres:temperature",
    severity=kanary.WARN,
    tags=["infra", "postgres", "threshold"],
    owner="expert_db",
)
class TemperatureThreshold(kanary.ThresholdRule):
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

    def test_removed_rule_is_recorded_and_removed_on_reload(self) -> None:
        source = self.engine.sources["postgres"]
        self.engine.evaluate_source(source.source_id, source.poll({}), now=self.now)
        self.engine.reload(rule_registry={})
        self.assertNotIn("postgres.temperature.stale", self.engine.alerts)

    def test_range_rule_fires_when_value_is_high(self) -> None:
        source = self.engine.sources["postgres"]
        alerts = self.engine.evaluate_source(source.source_id, source.poll({}), now=self.now)
        alert = alerts["postgres.temperature.range"]
        self.assertEqual(alert.state, kanary.AlertState.FIRING)
        self.assertEqual(
            alert.message,
            "temperature=123 out of range [-inf, 100]",
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
            first_state.current.payload["channels"]["temperature"]["value"],
            123,
        )
        self.assertEqual(first_state.previous.payload, {})

        source.now = self.now - timedelta(seconds=5)
        self.engine.evaluate_source(source.source_id, source.poll({}), now=self.now)
        second_state = self.engine.source_states["postgres"]
        self.assertEqual(
            second_state.previous.payload["channels"]["temperature"]["value"],
            123,
        )
        self.assertEqual(
            second_state.current.payload["channels"]["humidity"]["value"],
            45,
        )

    def test_no_data_evaluates_rules_without_marking_them_failed(self) -> None:
        alerts = self.engine.evaluate_source("postgres", kanary.no_data(reason="no rows"), now=self.now)
        self.assertEqual(alerts["postgres.temperature.range"].state, kanary.AlertState.OK)
        self.assertEqual(alerts["postgres.temperature.stale"].state, kanary.AlertState.FIRING)
        self.assertEqual(
            self.engine.plugin_states["rule:postgres.temperature.range"].state,
            "READY",
        )
        source_state = self.engine.source_states["postgres"]
        self.assertEqual(source_state.current.payload["status"], "empty")
        self.assertEqual(source_state.current.payload["channels"], {})

    def test_no_update_keeps_snapshot_and_re_evaluates_rules(self) -> None:
        source = self.engine.sources["postgres"]
        source.now = self.now
        self.engine.evaluate_source(source.source_id, source.poll({}), now=self.now)
        before = self.engine.source_states["postgres"]
        later = self.now + timedelta(minutes=20)

        alerts = self.engine.evaluate_source("postgres", kanary.no_update(reason="latest row not advanced"), now=later)

        after = self.engine.source_states["postgres"]
        self.assertEqual(after.current.payload, before.current.payload)
        self.assertEqual(after.previous.payload, before.previous.payload)
        self.assertEqual(after.poll_count, before.poll_count)
        self.assertEqual(alerts["postgres.temperature.stale"].state, kanary.AlertState.FIRING)
        self.assertEqual(
            alerts["postgres.temperature.stale"].message,
            "stale for 20 min (> 10 min)",
        )

    def test_skip_keeps_snapshot_and_does_not_re_evaluate_rules(self) -> None:
        source = self.engine.sources["postgres"]
        source.now = self.now
        initial_alerts = self.engine.evaluate_source(source.source_id, source.poll({}), now=self.now)
        before_state = self.engine.source_states["postgres"]
        before_alert = initial_alerts["postgres.temperature.stale"]
        later = self.now + timedelta(minutes=20)

        alerts = self.engine.evaluate_source("postgres", kanary.skip(reason="warming up"), now=later)

        after_state = self.engine.source_states["postgres"]
        after_alert = alerts["postgres.temperature.stale"]
        self.assertEqual(after_state.current.payload, before_state.current.payload)
        self.assertEqual(after_state.previous.payload, before_state.previous.payload)
        self.assertEqual(after_state.poll_count, before_state.poll_count)
        self.assertEqual(after_alert.state, before_alert.state)
        self.assertEqual(after_alert.message, before_alert.message)
        self.assertEqual(after_alert.last_evaluated_at, before_alert.last_evaluated_at)

    def test_rule_context_input_accessors_work_for_current_and_previous(self) -> None:
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

    def test_rule_context_input_accessors_support_dotted_names(self) -> None:
        source_state = kanary.SourceState(
            source_id="postgres",
            current=kanary.SourceSnapshot(
                payload={
                    "channels": {
                        "kernel_machine_room.oxygen_concentration": {
                            "value": 20.9,
                            "timestamp": self.now,
                            "metadata": {"source_name": "raw.channel"},
                        }
                    }
                },
                observed_at=self.now,
            ),
        )
        ctx = kanary.RuleContext(
            now=self.now,
            source_id="postgres",
            source_state=source_state,
        )
        self.assertEqual(ctx.value("kernel_machine_room.oxygen_concentration"), 20.9)
        self.assertEqual(ctx.timestamp("kernel_machine_room.oxygen_concentration"), self.now)
        self.assertEqual(
            ctx.metadata("kernel_machine_room.oxygen_concentration"),
            {"source_name": "raw.channel"},
        )

    def test_rule_context_inputs_are_sorted_and_deduplicated(self) -> None:
        ctx = kanary.RuleContext(
            now=self.now,
            source_states={
                "secondary": kanary.SourceState(
                    source_id="secondary",
                    current=kanary.SourceSnapshot(
                        payload={
                            "channels": {
                                "temperature": {"value": 25, "timestamp": self.now, "metadata": {}}
                            }
                        },
                        observed_at=self.now,
                    ),
                ),
                "primary": kanary.SourceState(
                    source_id="primary",
                    current=kanary.SourceSnapshot(
                        payload={
                            "channels": {
                                "temperature": {"value": 20, "timestamp": self.now, "metadata": {}}
                            }
                        },
                        observed_at=self.now,
                    ),
                ),
            },
            declared_inputs=("primary:*", "*:temperature"),
            resolved_sources=("secondary", "primary"),
        )

        self.assertEqual(
            [item.name for item in ctx.inputs()],
            ["primary:temperature", "secondary:temperature"],
        )

    def test_rule_context_single_input_sugar_supports_previous_values(self) -> None:
        ctx = kanary.RuleContext(
            now=self.now,
            source_states={
                "primary": kanary.SourceState(
                    source_id="primary",
                    current=kanary.SourceSnapshot(
                        payload={
                            "channels": {
                                "temperature": {
                                    "value": 20,
                                    "timestamp": self.now,
                                    "metadata": {"unit": "C"},
                                }
                            }
                        },
                        observed_at=self.now,
                    ),
                    previous=kanary.SourceSnapshot(
                        payload={
                            "channels": {
                                "temperature": {
                                    "value": 19,
                                    "timestamp": self.now - timedelta(minutes=1),
                                    "metadata": {"unit": "C"},
                                }
                            }
                        },
                        observed_at=self.now - timedelta(minutes=1),
                    ),
                ),
            },
            declared_inputs=("primary:temperature",),
            resolved_sources=("primary",),
        )

        self.assertEqual(ctx.value(), 20)
        self.assertEqual(ctx.prev_value(), 19)
        self.assertEqual(ctx.names(), ["primary:temperature"])
        self.assertEqual(ctx.values(), [20])
        self.assertEqual(ctx.timestamps(), [self.now])
        self.assertEqual(ctx.metadatas(), [{"unit": "C"}])

    def test_rule_context_short_name_raises_when_ambiguous(self) -> None:
        ctx = kanary.RuleContext(
            now=self.now,
            source_states={
                "primary": kanary.SourceState(
                    source_id="primary",
                    current=kanary.SourceSnapshot(
                        payload={
                            "channels": {
                                "temperature": {"value": 20, "timestamp": self.now, "metadata": {}}
                            }
                        },
                        observed_at=self.now,
                    ),
                ),
                "secondary": kanary.SourceState(
                    source_id="secondary",
                    current=kanary.SourceSnapshot(
                        payload={
                            "channels": {
                                "temperature": {"value": 25, "timestamp": self.now, "metadata": {}}
                            }
                        },
                        observed_at=self.now,
                    ),
                ),
            },
            declared_inputs=("primary:temperature", "secondary:temperature"),
            resolved_sources=("primary", "secondary"),
        )

        with self.assertRaisesRegex(ValueError, "ambiguous"):
            ctx.value("temperature")

    def test_helper_rules_support_dotted_input_names(self) -> None:
        source_state = kanary.SourceState(
            source_id="postgres",
            current=kanary.SourceSnapshot(
                payload={
                    "channels": {
                        "kernel_machine_room.oxygen_concentration": {
                            "value": 20.9,
                            "timestamp": self.now,
                            "metadata": {},
                        }
                    }
                },
                observed_at=self.now,
            ),
        )
        ctx = kanary.RuleContext(
            now=self.now,
            source_id="postgres",
            source_state=source_state,
        )

        class DottedStale(kanary.StaleRule):
            rule_id = "test.dotted.stale"
            inputs = "postgres:kernel_machine_room.oxygen_concentration"
            severity = kanary.ERROR
            tags = ["test"]
            timeout = 60.0

        class DottedRange(kanary.RangeRule):
            rule_id = "test.dotted.range"
            inputs = "postgres:kernel_machine_room.oxygen_concentration"
            severity = kanary.WARN
            tags = ["test"]
            high = 21.0

        stale_alert = DottedStale().evaluate(ctx)
        range_alert = DottedRange().evaluate(ctx)

        self.assertEqual(stale_alert.state, kanary.AlertState.OK)
        self.assertEqual(range_alert.state, kanary.AlertState.OK)

    def test_source_inputs_helper_uses_outer_timestamp_and_metadata(self) -> None:
        payload = self.engine._normalize_source_result(
            kanary.inputs(
                [
                    ("value1", 10),
                    ("value2", 12, self.now, {"unit": "degC"}),
                    ("value3", 13, None, {"unit": "pct"}),
                ],
                timestamp=self.now,
                metadata={"table": "demo"},
            ),
            now=self.now,
        )
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["metadata"], {"table": "demo"})
        self.assertEqual(payload["channels"]["value1"]["timestamp"], self.now)
        self.assertEqual(payload["channels"]["value2"]["metadata"], {"unit": "degC"})
        self.assertEqual(payload["channels"]["value3"]["timestamp"], self.now)
        self.assertEqual(payload["channels"]["value3"]["metadata"], {"unit": "pct"})

    def test_source_inputs_helper_accepts_mapping_style(self) -> None:
        payload = self.engine._normalize_source_result(
            kanary.inputs(
                {
                    "temperature": (23.4, None, {"unit": "C"}),
                    "humidity": 40.0,
                },
                timestamp=self.now,
            ),
            now=self.now,
        )
        self.assertEqual(payload["channels"]["temperature"]["value"], 23.4)
        self.assertEqual(payload["channels"]["temperature"]["metadata"], {"unit": "C"})
        self.assertEqual(payload["channels"]["humidity"]["timestamp"], self.now)

    def test_source_inputs_helper_accepts_single_named_input_form(self) -> None:
        payload = self.engine._normalize_source_result(
            kanary.inputs("temperature", 23.4, self.now),
            now=self.now,
        )
        self.assertEqual(payload["channels"]["temperature"]["value"], 23.4)
        self.assertEqual(payload["channels"]["temperature"]["timestamp"], self.now)

    def test_no_data_helper_sets_empty_status_and_reason(self) -> None:
        payload = self.engine._normalize_source_result(
            kanary.no_data(reason="no rows", metadata={"table": "demo"}),
            now=self.now,
        )
        self.assertEqual(payload["status"], "empty")
        self.assertEqual(payload["reason"], "no rows")
        self.assertEqual(payload["metadata"], {"table": "demo"})

    def test_no_update_helper_sets_status_and_reason(self) -> None:
        payload = self.engine._normalize_source_result(
            kanary.no_update(reason="latest row not advanced", metadata={"table": "demo"}),
            now=self.now,
        )
        self.assertEqual(payload["status"], "no_update")
        self.assertEqual(payload["reason"], "latest row not advanced")
        self.assertEqual(payload["metadata"], {"table": "demo"})

    def test_skip_helper_sets_status_and_reason(self) -> None:
        payload = self.engine._normalize_source_result(
            kanary.skip(reason="warmup", metadata={"phase": "startup"}),
            now=self.now,
        )
        self.assertEqual(payload["status"], "skip")
        self.assertEqual(payload["reason"], "warmup")
        self.assertEqual(payload["metadata"], {"phase": "startup"})

    def test_rule_helpers_inherit_payload_and_merge_extra(self) -> None:
        rule = self.engine.rules["postgres.temperature.custom_threshold"]
        source_payload = {
            "channels": {
                "temperature": {"value": 42, "timestamp": self.now, "metadata": {}},
            },
            "status": "ok",
        }
        evaluation = rule.normalize_evaluation(
            kanary.error("too hot", extra={"delta": 2}),
            source_payload,
        )
        self.assertEqual(evaluation.state, kanary.FIRING)
        self.assertEqual(evaluation.severity, kanary.ERROR)
        self.assertEqual(evaluation.message, "too hot")
        self.assertEqual(evaluation.payload["status"], "ok")
        self.assertEqual(evaluation.payload["delta"], 2)

    def test_rule_normalize_accepts_none_bool_and_tuple_forms(self) -> None:
        rule = self.engine.rules["postgres.temperature.custom_threshold"]
        source_payload = {
            "channels": {
                "temperature": {"value": 10, "timestamp": self.now, "metadata": {}},
            },
            "status": "ok",
        }
        self.assertEqual(rule.normalize_evaluation(None, source_payload).state, kanary.OK)
        self.assertEqual(rule.normalize_evaluation(True, source_payload).state, kanary.FIRING)
        self.assertEqual(rule.normalize_evaluation(False, source_payload).state, kanary.OK)
        tuple_eval = rule.normalize_evaluation(("WARN", "watch it"), source_payload)
        self.assertEqual(tuple_eval.state, kanary.FIRING)
        self.assertEqual(tuple_eval.severity, kanary.WARN)
        self.assertEqual(tuple_eval.message, "watch it")
        tuple_eval = rule.normalize_evaluation((kanary.ERROR, "too hot"), source_payload)
        self.assertEqual(tuple_eval.state, kanary.FIRING)
        self.assertEqual(tuple_eval.severity, kanary.ERROR)
        self.assertEqual(tuple_eval.message, "too hot")

    def test_error_if_returns_evaluation_or_none(self) -> None:
        self.assertIsNone(kanary.error_if(False, "nope"))
        result = kanary.error_if(True, "boom")
        self.assertIsInstance(result, kanary.Evaluation)
        assert result is not None
        self.assertEqual(result.state, kanary.FIRING)
        self.assertEqual(result.severity, kanary.ERROR)
        self.assertEqual(result.message, "boom")

    def test_stale_rule_missing_input_message_shows_candidates(self) -> None:
        source_state = kanary.SourceState(
            source_id="postgres",
            current=kanary.SourceSnapshot(
                payload={
                    "channels": {
                        "det1.radon_conc": {"value": 12.3, "timestamp": self.now, "metadata": {}},
                        "det2.radon_conc": {"value": 45.6, "timestamp": self.now, "metadata": {}},
                    }
                },
                observed_at=self.now,
            ),
        )
        ctx = kanary.RuleContext(now=self.now, source_id="postgres", source_state=source_state)

        class MissingMeasurementStale(kanary.StaleRule):
            rule_id = "test.missing_measurement.stale"
            inputs = "postgres:det1.radon_conc.Bq_m3"
            severity = kanary.ERROR
            tags = ["test"]
            timeout = 60.0

        alert = MissingMeasurementStale().evaluate(ctx)
        self.assertEqual(alert.state, kanary.AlertState.FIRING)
        self.assertIn("input 'det1.radon_conc.Bq_m3' is missing", alert.message)
        self.assertIn("closest available input: postgres:det1.radon_conc", alert.message)
        self.assertIn("available inputs: postgres:det1.radon_conc, postgres:det2.radon_conc", alert.message)

    def test_threshold_rule_reports_missing_value_inside_existing_measurement(self) -> None:
        source_state = kanary.SourceState(
            source_id="postgres",
            current=kanary.SourceSnapshot(
                payload={
                    "channels": {
                        "temperature": {"timestamp": self.now, "metadata": {}},
                    }
                },
                observed_at=self.now,
            ),
        )
        ctx = kanary.RuleContext(now=self.now, source_id="postgres", source_state=source_state)

        class MissingValueThreshold(kanary.ThresholdRule):
            rule_id = "test.missing_value.threshold"
            inputs = "postgres:temperature"
            severity = kanary.WARN
            tags = ["test"]
            direction = "high"
            thresholds = [(10.0, kanary.WARN)]

        alert = MissingValueThreshold().evaluate(ctx)
        self.assertEqual(alert.state, kanary.AlertState.OK)
        self.assertEqual(alert.message, "input 'temperature' is present but value is missing")

    def test_threshold_rule_low_direction_ok_message_reads_naturally(self) -> None:
        source_state = kanary.SourceState(
            source_id="slowcontrol_postgres",
            current=kanary.SourceSnapshot(
                payload={
                    "channels": {
                        "kernel_cleanroom.oxygen": {"value": 21.0, "timestamp": self.now, "metadata": {}},
                        "kernel_laboratory.oxygen": {"value": 20.8, "timestamp": self.now, "metadata": {}},
                    }
                },
                observed_at=self.now,
            ),
        )
        ctx = kanary.RuleContext(now=self.now, source_id="slowcontrol_postgres", source_state=source_state)

        class KernelOxygenLevel(kanary.ThresholdRule):
            rule_id = "scdb.oxygen.level"
            inputs = "slowcontrol_postgres:kernel_*.oxygen"
            severity = kanary.WARN
            tags = ["kernel"]
            direction = "low"
            thresholds = [
                (20.5, kanary.WARN),
                (20.0, kanary.ERROR),
                (19.5, kanary.CRITICAL),
            ]

        alert = KernelOxygenLevel().evaluate(ctx)
        self.assertEqual(alert.state, kanary.OK)
        self.assertEqual(
            alert.message,
            "all inputs are above configured low thresholds low [20.5->WARN, 20->ERROR, 19.5->CRITICAL]",
        )

    def test_multi_input_helper_rule_fires_when_any_input_matches(self) -> None:
        test_now = self.now

        class PrimarySource(kanary.Source):
            source_id = "primary"
            interval = 5.0

            def poll(self, ctx):
                return kanary.SourceResult(
                    measurements=[
                        kanary.Measurement(name="temperature", value=20, timestamp=test_now),
                    ]
                )

        class SecondarySource(kanary.Source):
            source_id = "secondary"
            interval = 5.0

            def poll(self, ctx):
                return kanary.SourceResult(
                    measurements=[
                        kanary.Measurement(name="temperature", value=80, timestamp=test_now),
                    ]
                )

        class DualTemperatureRange(kanary.RangeRule):
            rule_id = "pair.temperature.range"
            inputs = ["primary:temperature", "secondary:temperature"]
            severity = kanary.WARN
            tags = ["pair"]
            owner = "expert_pair"
            high = 50

        engine = kanary.Engine(
            now_fn=lambda: test_now,
            source_registry={
                "primary": PrimarySource,
                "secondary": SecondarySource,
            },
            rule_registry={"pair.temperature.range": DualTemperatureRange},
            output_registry={},
        )
        engine.start()
        try:
            engine.evaluate_source("primary", engine.sources["primary"].poll({}), now=test_now)
            engine.evaluate_source("secondary", engine.sources["secondary"].poll({}), now=test_now)
            alert = engine.alerts["pair.temperature.range"]
        finally:
            engine.shutdown()

        self.assertEqual(alert.state, kanary.AlertState.FIRING)
        self.assertEqual(
            alert.payload["matched_inputs"],
            [{"name": "secondary:temperature", "value": 80}],
        )

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
            "humidity=45 out of range (45, 50)",
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
            "temperature rate=-61.5 / 1 min out of range [-1.0, 0.5]",
        )


class BufferedSourceTest(unittest.TestCase):
    def test_buffered_source_keeps_history_and_computes_aggregates(self) -> None:
        source = BufferedTemperatureSource()
        source.init()
        try:
            source.poll()
            source.poll()
            source.poll()
            history = source.history("temperature")
            self.assertEqual(len(history), 3)
            self.assertEqual(source.average_value("temperature"), 22.0)
            self.assertEqual(source.min_value("temperature"), 10.0)
            self.assertEqual(source.max_value("temperature"), 34.0)
            self.assertEqual(source.count("temperature"), 3)
            self.assertEqual(source.rate("temperature", per_seconds=60.0), 0.4)
        finally:
            source.terminate()


class SourceScheduleTest(unittest.TestCase):
    def test_parse_cron_schedule_supports_five_fields_and_macros(self) -> None:
        from kanary.schedule import parse_schedule

        five_field = parse_schedule("*/5 * * * *")
        macro = parse_schedule("@hourly")

        self.assertTrue(five_field.matches(datetime(2026, 3, 23, 10, 15, tzinfo=timezone.utc)))
        self.assertFalse(five_field.matches(datetime(2026, 3, 23, 10, 16, tzinfo=timezone.utc)))
        self.assertTrue(macro.matches(datetime(2026, 3, 23, 10, 0, tzinfo=timezone.utc)))
        self.assertFalse(macro.matches(datetime(2026, 3, 23, 10, 5, tzinfo=timezone.utc)))

    def test_source_requires_exactly_one_of_interval_or_schedule(self) -> None:
        class MissingTimingSource(kanary.Source):
            source_id = "missing.timing"

            def poll(self, ctx):
                return kanary.SourceResult()

        class ConflictingTimingSource(kanary.Source):
            source_id = "conflicting.timing"
            interval = 10.0
            schedule = "*/5 * * * *"

            def poll(self, ctx):
                return kanary.SourceResult()

        cls = kanary.register_source(MissingTimingSource)
        self.assertEqual(cls.interval, 60.0)
        with self.assertRaisesRegex(ValueError, "must not define both interval and schedule"):
            kanary.register_source(ConflictingTimingSource)

    def test_schedule_source_validation_rejects_invalid_cron(self) -> None:
        with TemporaryDirectory() as tmp:
            rule_file = Path(tmp) / "plugins.py"
            rule_file.write_text(
                textwrap.dedent(
                    """
                    import kanary

                    @kanary.source(source_id="example.source", schedule="bad cron")
                    class ExampleSource:
                        def poll(self):
                            return kanary.SourceResult()
                    """
                )
            )
            loader = kanary.RuleDirectoryLoader(tmp)
            with self.assertRaisesRegex(RuntimeError, "source 'example.source' schedule is invalid"):
                loader.inspect()

    def test_initial_schedule_run_allows_current_matching_minute(self) -> None:
        from kanary.runtime import _initial_schedule_run_at
        from kanary.schedule import parse_schedule

        schedule = parse_schedule("*/5 * * * *")
        now = datetime(2026, 3, 23, 10, 15, 30, tzinfo=timezone.utc)
        self.assertEqual(_initial_schedule_run_at(schedule, now), now)

        later = datetime(2026, 3, 23, 10, 16, 30, tzinfo=timezone.utc)
        self.assertEqual(
            _initial_schedule_run_at(schedule, later),
            datetime(2026, 3, 23, 10, 20, tzinfo=timezone.utc),
        )


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
                        inputs="example.source:a",
                        severity=kanary.ERROR,
                        tags=["example"],
                    )
                    class ExampleRule(kanary.StaleRule):
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

                    @kanary.source(source_id="example.source", interval=60.0)
                    class ExampleSource:
                        def poll(self):
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
                        def evaluate(self, ctx):
                            return kanary.Evaluation(state=kanary.OK, payload=ctx.source_payload())
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
                        interval = 60.0

                        def poll(self):
                            return kanary.SourceResult()

                    @kanary.rule
                    class WarningRule:
                        rule_id = "example.warning"
                        source = "example.source"
                        severity = kanary.ERROR
                        tags = []

                        def evaluate(self, ctx):
                            return kanary.Evaluation(state=kanary.OK, payload=ctx.source_payload())

                    @kanary.output(
                        output_id="example.output",
                        exclude_states=["OK", "FIRING", "ACKED", "SUPPRESSED", "SILENCED"],
                    )
                    class ExampleOutput:
                        def emit(self, event):
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
                "rule 'example.warning' has no matching output",
            ],
        )

    def test_inspect_allows_missing_owner(self) -> None:
        with TemporaryDirectory() as tmp:
            rule_file = Path(tmp) / "rules.py"
            rule_file.write_text(
                textwrap.dedent(
                    """
                    import kanary

                    @kanary.source(source_id="example.source", interval=5.0)
                    class ExampleSource:
                        def poll(self):
                            return kanary.SourceResult()

                    @kanary.rule(
                        rule_id="example.rule",
                        source="example.source",
                        severity=kanary.ERROR,
                        tags=["example"],
                    )
                    class ExampleRule:
                        def evaluate(self, ctx):
                            return kanary.Evaluation(state=kanary.OK, payload=ctx.source_payload())
                    """
                )
            )
            loader = kanary.RuleDirectoryLoader(tmp)
            _, report = loader.inspect()

        self.assertEqual(report.errors, [])
        self.assertNotIn("rule 'example.rule' has no owner", report.warnings)

    def test_inspect_warns_for_legacy_plugin_signatures(self) -> None:
        with TemporaryDirectory() as tmp:
            rule_file = Path(tmp) / "rules.py"
            rule_file.write_text(
                textwrap.dedent(
                    """
                    import kanary

                    @kanary.source(source_id="example.source", interval=5.0)
                    class ExampleSource:
                        def init(self, ctx):
                            return None

                        def poll(self, ctx):
                            return kanary.SourceResult()

                        def terminate(self, ctx):
                            return None

                    @kanary.rule(
                        rule_id="example.rule",
                        inputs="example.source:value",
                        severity=kanary.ERROR,
                        tags=["example"],
                    )
                    class ExampleRule:
                        def evaluate(self, payload, ctx):
                            return kanary.Evaluation(state=kanary.OK, payload=payload)

                    @kanary.output(output_id="example.output")
                    class ExampleOutput:
                        def init(self, ctx):
                            return None

                        def emit(self, event, ctx):
                            return None

                        def terminate(self, ctx):
                            return None
                    """
                )
            )
            loader = kanary.RuleDirectoryLoader(tmp)
            _, report = loader.inspect()

        self.assertEqual(report.errors, [])
        self.assertIn("source 'example.source' uses legacy init(ctx); prefer init()", report.warnings)
        self.assertIn("source 'example.source' uses legacy poll(ctx); prefer poll()", report.warnings)
        self.assertIn(
            "source 'example.source' uses legacy terminate(ctx); prefer terminate()",
            report.warnings,
        )
        self.assertIn(
            "rule 'example.rule' uses legacy evaluate(payload, ctx); prefer evaluate(ctx)",
            report.warnings,
        )
        self.assertIn("output 'example.output' uses legacy init(ctx); prefer init()", report.warnings)
        self.assertIn(
            "output 'example.output' uses legacy emit(event, ctx); prefer emit(event)",
            report.warnings,
        )
        self.assertIn(
            "output 'example.output' uses legacy terminate(ctx); prefer terminate()",
            report.warnings,
        )

    def test_inspect_warns_for_broad_and_duplicate_input_selectors(self) -> None:
        with TemporaryDirectory() as tmp:
            rule_file = Path(tmp) / "rules.py"
            rule_file.write_text(
                textwrap.dedent(
                    """
                    import kanary

                    @kanary.source(source_id="alpha", interval=5.0)
                    class AlphaSource:
                        def poll(self):
                            return kanary.SourceResult()

                    @kanary.source(source_id="beta", interval=5.0)
                    class BetaSource:
                        def poll(self, ctx):
                            return kanary.SourceResult()

                    @kanary.output(output_id="example.output")
                    class ExampleOutput:
                        def emit(self, event):
                            return None

                    @kanary.rule(
                        rule_id="example.rule",
                        inputs=["temperature", "*:*", "*:*", "alpha:*"],
                        severity=kanary.ERROR,
                        tags=["example"],
                        owner="owner",
                    )
                    class ExampleRule:
                        def evaluate(self, ctx):
                            return kanary.Evaluation(state=kanary.OK, payload=ctx.source_payload())
                    """
                )
            )
            loader = kanary.RuleDirectoryLoader(tmp)
            _, report = loader.inspect()

        self.assertEqual(report.errors, [])
        self.assertIn(
            "rule 'example.rule' input selector 'temperature' omits source pattern; prefer fully-qualified 'source:input'",
            report.warnings,
        )
        self.assertIn(
            "rule 'example.rule' uses very broad input selector '*:*'; prefer narrower source:input patterns",
            report.warnings,
        )
        self.assertIn(
            "rule 'example.rule' declares duplicate input selector '*:*'",
            report.warnings,
        )

    def test_inspect_warns_for_cwd_relative_open_calls(self) -> None:
        with TemporaryDirectory() as tmp:
            rule_file = Path(tmp) / "rules.py"
            rule_file.write_text(
                textwrap.dedent(
                    """
                    import kanary

                    @kanary.source(source_id="example.source", interval=5.0)
                    class ExampleSource:
                        def init(self, ctx):
                            with open("config.toml", "rb") as handle:
                                self.config = handle.read()

                        def poll(self, ctx):
                            return kanary.SourceResult()
                    """
                )
            )
            loader = kanary.RuleDirectoryLoader(tmp)
            _, report = loader.inspect()

        self.assertEqual(report.errors, [])
        self.assertIn(
            "file 'rules.py' uses open('config.toml') with a cwd-relative path; prefer kanary.plugin_dir() / ... or Path(__file__).with_name(...)",
            report.warnings,
        )

    def test_plugin_file_helpers_resolve_relative_files_and_nested_keys(self) -> None:
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            (tmp_path / "config.toml").write_text(
                textwrap.dedent(
                    """
                    dsn = "postgresql://example"

                    [mention]
                    role_id = 42
                    """
                ),
                encoding="utf-8",
            )
            (tmp_path / "config.json").write_text(
                json.dumps({"webhook": {"url": "https://example.invalid/hook"}}),
                encoding="utf-8",
            )
            plugin_file = tmp_path / "plugin.py"
            plugin_file.write_text(
                textwrap.dedent(
                    """
                    import kanary

                    def read_config():
                        return {
                            "plugin_dir": kanary.plugin_dir().name,
                            "dsn": kanary.load_toml("dsn"),
                            "role_id": kanary.load_toml("mention.role_id"),
                            "all_toml": kanary.load_toml(),
                            "json_url": kanary.load_json("webhook.url"),
                        }
                    """
                ),
                encoding="utf-8",
            )

            spec = importlib.util.spec_from_file_location("temp_plugin_helpers", plugin_file)
            self.assertIsNotNone(spec)
            self.assertIsNotNone(spec.loader)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            result = module.read_config()

        self.assertEqual(result["plugin_dir"], tmp_path.name)
        self.assertEqual(result["dsn"], "postgresql://example")
        self.assertEqual(result["role_id"], 42)
        self.assertEqual(result["all_toml"]["mention"]["role_id"], 42)
        self.assertEqual(result["json_url"], "https://example.invalid/hook")

    def test_plugin_file_helpers_raise_runtime_error_for_missing_file_and_key(self) -> None:
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            (tmp_path / "config.toml").write_text("dsn = 'postgresql://example'\n", encoding="utf-8")
            plugin_file = tmp_path / "plugin.py"
            plugin_file.write_text(
                textwrap.dedent(
                    """
                    import kanary

                    def missing_file():
                        return kanary.load_json(filename="config.json")

                    def missing_key():
                        return kanary.load_toml("mention.role_id")
                    """
                ),
                encoding="utf-8",
            )

            spec = importlib.util.spec_from_file_location("temp_plugin_helper_errors", plugin_file)
            self.assertIsNotNone(spec)
            self.assertIsNotNone(spec.loader)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

            with self.assertRaisesRegex(RuntimeError, r"plugin config file not found: .*config\.json"):
                module.missing_file()
            with self.assertRaisesRegex(RuntimeError, r"config key 'mention\.role_id' not found in .*config\.toml"):
                module.missing_key()

    def test_loader_accepts_single_plugin_file_path(self) -> None:
        with TemporaryDirectory() as tmp:
            rule_file = Path(tmp) / "single_plugin.py"
            rule_file.write_text(
                textwrap.dedent(
                    """
                    import kanary

                    @kanary.source(source_id="example.source", interval=5.0)
                    class ExampleSource:
                        def poll(self, ctx):
                            return kanary.SourceResult()
                    """
                )
            )
            loader = kanary.RuleDirectoryLoader(rule_file)
            snapshot = loader.load()

        self.assertIn("example.source", snapshot.sources)

    def test_loader_error_includes_plugin_file_path(self) -> None:
        with TemporaryDirectory() as tmp:
            rule_file = Path(tmp) / "broken_plugin.py"
            rule_file.write_text(
                textwrap.dedent(
                    """
                    import kanary

                    @kanary.output(output_id="example.output")
                    class ExampleOutput:
                        output_path = Path("kanary_outputs.jsonl")
                    """
                )
            )
            loader = kanary.RuleDirectoryLoader(rule_file)

            with self.assertRaises(RuntimeError) as captured:
                loader.load()

        message = str(captured.exception)
        self.assertIn("failed to load plugin file", message)
        self.assertIn("broken_plugin.py", message)
        self.assertIn("NameError: name 'Path' is not defined", message)

    def test_inspect_rejects_legacy_measurement_attribute(self) -> None:
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
                        owner="owner",
                    )
                    class ExampleRule(kanary.RangeRule):
                        measurement = "temperature"
                        high = 10.0
                    """
                )
            )
            loader = kanary.RuleDirectoryLoader(tmp)
            with self.assertRaisesRegex(
                RuntimeError,
                "rule 'example.rule' measurement is no longer supported; use inputs='source_id:input_name'",
            ):
                loader.inspect()

    def test_inspect_rejects_non_positive_stale_timeout(self) -> None:
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
                        inputs="example.source:value",
                        severity=kanary.ERROR,
                        tags=["example"],
                        owner="expert",
                    )
                    class ExampleRule(kanary.StaleRule):
                        timeout = 0.0
                    """
                )
            )
            loader = kanary.RuleDirectoryLoader(tmp)
            _, report = loader.inspect()

        self.assertIn(
            "rule 'example.rule' timeout must be a positive number",
            report.errors,
        )

    def test_inspect_rejects_non_numeric_stale_timeout(self) -> None:
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
                        inputs="example.source:value",
                        severity=kanary.ERROR,
                        tags=["example"],
                        owner="expert",
                    )
                    class ExampleRule(kanary.StaleRule):
                        timeout = "slow"
                    """
                )
            )
            loader = kanary.RuleDirectoryLoader(tmp)
            _, report = loader.inspect()

        self.assertIn(
            "rule 'example.rule' timeout must be a positive number",
            report.errors,
        )

    def test_inspect_sets_matched_outputs_on_rule_classes(self) -> None:
        with TemporaryDirectory() as tmp:
            rule_file = Path(tmp) / "rules.py"
            rule_file.write_text(
                textwrap.dedent(
                    """
                    import kanary

                    @kanary.source(source_id="example.source", interval=60.0)
                    class ExampleSource:
                        def poll(self):
                            return kanary.SourceResult()

                    @kanary.rule(
                        rule_id="example.rule",
                        source="example.source",
                        severity=kanary.ERROR,
                        tags=["sqlite", "infra"],
                        owner="expert",
                    )
                    class ExampleRule:
                        def evaluate(self, ctx):
                            return kanary.Evaluation(state=kanary.OK, payload=ctx.source_payload())

                    @kanary.output(output_id="match-by-tag", include_tags=["infra"])
                    class MatchByTag:
                        def emit(self, event):
                            return None

                    @kanary.output(output_id="match-by-exclusion", exclude_states=["SUPPRESSED"])
                    class MatchByExclusion:
                        def emit(self, event):
                            return None

                    @kanary.output(output_id="match-all")
                    class MatchAll:
                        def emit(self, event):
                            return None
                    """
                )
            )
            loader = kanary.RuleDirectoryLoader(tmp)
            snapshot, report = loader.inspect()
        self.assertEqual(report.errors, [])
        self.assertEqual(report.warnings, [])
        self.assertEqual(snapshot.rules["example.rule"].matched_outputs, ["match-by-tag", "match-by-exclusion", "match-all"])

    def test_inspect_matches_outputs_with_glob_tag_patterns(self) -> None:
        with TemporaryDirectory() as tmp:
            rule_file = Path(tmp) / "rules.py"
            rule_file.write_text(
                textwrap.dedent(
                    """
                    import kanary

                    @kanary.source(source_id="example.source", interval=60.0)
                    class ExampleSource:
                        def poll(self, ctx):
                            return kanary.SourceResult()

                    @kanary.rule(
                        rule_id="example.rule",
                        source="example.source",
                        severity=kanary.ERROR,
                        tags=["expert_db", "infra"],
                        owner="expert",
                    )
                    class ExampleRule:
                        def evaluate(self, payload, ctx):
                            return kanary.Evaluation(state=kanary.OK, payload=payload)

                    @kanary.output(output_id="match-glob", include_tags=["expert_*"])
                    class MatchGlob:
                        def emit(self, event, ctx):
                            return None
                    """
                )
            )
            loader = kanary.RuleDirectoryLoader(tmp)
            snapshot, report = loader.inspect()
        self.assertEqual(report.errors, [])
        self.assertIn("match-glob", snapshot.rules["example.rule"].matched_outputs)

    def test_inspect_matches_outputs_with_minimum_severity_against_declared_rule_severity(self) -> None:
        with TemporaryDirectory() as tmp:
            rule_file = Path(tmp) / "rules.py"
            rule_file.write_text(
                textwrap.dedent(
                    """
                    import kanary

                    @kanary.source(source_id="example.source", interval=60.0)
                    class ExampleSource:
                        def poll(self, ctx):
                            return kanary.SourceResult()

                    @kanary.rule(
                        rule_id="example.rule",
                        source="example.source",
                        severity=kanary.WARN,
                        tags=["threshold"],
                        owner="expert",
                    )
                    class ExampleRule:
                        def evaluate(self, ctx):
                            return kanary.critical("runtime escalation")

                    @kanary.output(output_id="critical-only", include_tags=["threshold"], minimum_severity="CRITICAL")
                    class CriticalOnly:
                        def emit(self, event):
                            return None

                    @kanary.output(output_id="warn-and-up", include_tags=["threshold"], minimum_severity="WARN")
                    class WarnAndUp:
                        def emit(self, event):
                            return None
                    """
                )
            )
            loader = kanary.RuleDirectoryLoader(tmp)
            snapshot, report = loader.inspect()
        self.assertEqual(report.errors, [])
        self.assertEqual(snapshot.rules["example.rule"].matched_outputs, ["warn-and-up"])

    def test_inspect_rejects_plugin_id_collisions_across_types(self) -> None:
        with TemporaryDirectory() as tmp:
            rule_file = Path(tmp) / "rules.py"
            rule_file.write_text(
                textwrap.dedent(
                    """
                    import kanary

                    @kanary.source(source_id="shared.plugin", interval=60.0)
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

                    @kanary.source(source_id="dup.source", interval=60.0)
                    class ExampleSource1:
                        def poll(self, ctx):
                            return kanary.SourceResult()

                    @kanary.source(source_id="dup.source", interval=60.0)
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

                    @kanary.source(source_id="keep.source", interval=60.0)
                    class KeepSource:
                        def poll(self, ctx):
                            return kanary.SourceResult()

                    @kanary.source(source_id="drop.source", interval=60.0)
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

                    @kanary.source(source_id="example.source", interval=60.0)
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


class RuntimeTargetedReloadTest(unittest.TestCase):
    def _write_plugins(self, directory: Path, *, rule_threshold: int = 10, output_label: str = "v1") -> None:
        path = directory / "plugins.py"
        path.write_text(
            textwrap.dedent(
                f"""
                import kanary

                @kanary.source(source_id="example.source", interval=60.0)
                class ExampleSource:
                    version = "{output_label}"

                    def poll(self, ctx):
                        return kanary.SourceResult(
                            measurements=[
                                kanary.Measurement(
                                    name="value",
                                    value=15,
                                    timestamp=ctx["now"],
                                )
                            ]
                        )

                @kanary.rule(
                    rule_id="example.rule",
                    inputs="example.source:value",
                    severity=kanary.ERROR,
                    tags=["example"],
                    owner="expert",
                )
                class ExampleRule(kanary.RangeRule):
                    high = {rule_threshold}

                @kanary.output(output_id="example.output")
                class ExampleOutput:
                    label = "{output_label}"

                    def emit(self, event, ctx):
                        return None
                """
            )
        )
        next_tick = getattr(self, "_write_tick", 1_700_000_000)
        self._write_tick = next_tick + 2
        os.utime(path, (self._write_tick, self._write_tick))

    def _bootstrap_runtime(
        self,
        directory: Path,
        *,
        output_emit_enabled: bool = True,
    ) -> EngineRuntime:
        runtime = EngineRuntime(
            RuntimeConfig(
                rule_directories=[directory],
                api_port=0,
                output_emit_enabled=output_emit_enabled,
            )
        )
        snapshot = runtime.loader.load(exclude_patterns=runtime.config.exclude_plugins)
        runtime._signature = runtime.loader.snapshot_signature()
        runtime._discovered_snapshot = snapshot
        runtime._discovered_metadata = runtime._collect_plugin_metadata(snapshot)
        runtime._loaded_metadata = dict(runtime._discovered_metadata)
        runtime.engine = kanary.Engine(
            source_registry=snapshot.sources,
            rule_registry=snapshot.rules,
            output_registry=snapshot.outputs,
            output_emit_enabled=runtime.config.output_emit_enabled,
        )
        runtime.engine.start()
        now = datetime(2026, 6, 4, 12, 0, tzinfo=timezone.utc)
        for source_id, source in runtime.engine.sources.items():
            payload = runtime.engine._call_source_poll(source, now=now)
            runtime.engine.evaluate_source(source_id, payload, now=now)
        runtime._publish_runtime_plugin_overlay()
        return runtime

    def _write_dynamic_discovery_plugins(self, directory: Path) -> None:
        path = directory / "plugins.py"
        path.write_text(
            textwrap.dedent(
                """
                import json
                from pathlib import Path

                import kanary

                CONFIG = json.loads(Path(__file__).with_name("remote_rules.json").read_text(encoding="utf-8"))

                @kanary.source(source_id="example.source", interval=60.0)
                class ExampleSource:
                    def poll(self, ctx):
                        return kanary.SourceResult(
                            measurements=[
                                kanary.Measurement(
                                    name="value",
                                    value=1,
                                    timestamp=ctx["now"],
                                )
                            ]
                        )

                for index, rule_id in enumerate(CONFIG["rule_ids"], start=1):
                    attrs = {
                        "__module__": __name__,
                        "rule_id": rule_id,
                        "inputs": "example.source:value",
                        "severity": kanary.ERROR,
                        "tags": ["dynamic"],
                        "high": index,
                    }
                    kanary.register_rule(type(f"GeneratedRule{index}", (kanary.RangeRule,), attrs))
                """
            )
        )

    def _write_full_restart_plugins(self, directory: Path) -> None:
        path = directory / "plugins.py"
        path.write_text(
            textwrap.dedent(
                """
                from pathlib import Path

                import kanary

                COUNT_PATH = Path(__file__).with_name("init_count.txt")
                FAIL_PATH = Path(__file__).with_name("fail_flag.txt")

                @kanary.source(source_id="restart.source", interval=60.0)
                class RestartSource:
                    def init(self):
                        if FAIL_PATH.exists() and FAIL_PATH.read_text(encoding="utf-8").strip() == "1":
                            raise RuntimeError("restart init failed")
                        current = int(COUNT_PATH.read_text(encoding="utf-8").strip() or "0") if COUNT_PATH.exists() else 0
                        COUNT_PATH.write_text(str(current + 1), encoding="utf-8")

                    def poll(self):
                        return kanary.SourceResult(
                            measurements=[
                                kanary.Measurement(
                                    name="value",
                                    value=1,
                                    timestamp=self._kanary_poll_now,
                                )
                            ]
                        )

                @kanary.rule(
                    rule_id="restart.rule",
                    inputs="restart.source:value",
                    severity=kanary.ERROR,
                    tags=["restart"],
                )
                class RestartRule(kanary.RangeRule):
                    high = 5
                """
            )
        )

    def test_reload_dirty_loads_discovered_rule(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_plugins(root)
            runtime = self._bootstrap_runtime(root)
            try:
                (root / "new_rule.py").write_text(
                    textwrap.dedent(
                        """
                        import kanary

                        @kanary.rule(
                            rule_id="example.extra",
                            inputs="example.source:value",
                            severity=kanary.ERROR,
                            tags=["example"],
                            owner="expert",
                        )
                        class ExtraRule(kanary.RangeRule):
                            high = 20
                        """
                    )
                )

                runtime.reload_now_if_changed()

                discovered = runtime.engine._plugin_status("rule", "example.extra")
                self.assertEqual(discovered.state, "DISCOVERED")
                self.assertFalse(discovered.loaded)
                self.assertNotIn("example.extra", runtime.engine.rules)

                summary = runtime.reload_now({"dirty": True})

                loaded = runtime.engine._plugin_status("rule", "example.extra")
                self.assertEqual(summary["rules"]["reloaded"], ["example.extra"])
                self.assertIn("example.extra", runtime.engine.rules)
                self.assertTrue(loaded.loaded)
                self.assertIsNone(loaded.dirty_reason)
                source = runtime.engine.sources["example.source"]
                now = datetime(2026, 6, 4, 12, 1, tzinfo=timezone.utc)
                runtime.engine.evaluate_source("example.source", source.poll({"now": now}), now=now)
                self.assertEqual(runtime.engine._plugin_status("rule", "example.extra").state, "READY")
            finally:
                runtime.api._server.server_close()
                runtime.engine.shutdown()

    def test_reload_dirty_loads_discovered_source_before_rule(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_plugins(root)
            runtime = self._bootstrap_runtime(root)
            try:
                (root / "new_source.py").write_text(
                    textwrap.dedent(
                        """
                        import kanary

                        @kanary.source(source_id="example.extra_source", interval=60.0)
                        class ExtraSource:
                            def poll(self, ctx):
                                return kanary.SourceResult(
                                    measurements=[
                                        kanary.Measurement(
                                            name="value",
                                            value=7,
                                            timestamp=ctx["now"],
                                        )
                                    ]
                                )
                        """
                    )
                )
                (root / "new_rule.py").write_text(
                    textwrap.dedent(
                        """
                        import kanary

                        @kanary.rule(
                            rule_id="example.extra_rule",
                            inputs="example.extra_source:value",
                            severity=kanary.ERROR,
                            tags=["example"],
                            owner="expert",
                        )
                        class ExtraRule(kanary.RangeRule):
                            high = 5
                        """
                    )
                )

                runtime.reload_now_if_changed()

                self.assertEqual(
                    runtime.engine._plugin_status("source", "example.extra_source").state,
                    "DISCOVERED",
                )
                self.assertEqual(
                    runtime.engine._plugin_status("rule", "example.extra_rule").state,
                    "DISCOVERED",
                )

                summary = runtime.reload_now({"dirty": True})

                self.assertEqual(summary["sources"]["reloaded"], ["example.extra_source"])
                self.assertEqual(summary["rules"]["reloaded"], ["example.extra_rule"])
                self.assertIn("example.extra_source", runtime.engine.sources)
                self.assertIn("example.extra_rule", runtime.engine.rules)
                self.assertEqual(
                    runtime.engine.rules["example.extra_rule"].resolved_sources,
                    ["example.extra_source"],
                )
                self.assertNotEqual(
                    runtime.engine._plugin_status("rule", "example.extra_rule").last_error,
                    "rule resolved zero sources or inputs",
                )
            finally:
                runtime.api._server.server_close()
                runtime.engine.shutdown()

    def test_reload_rule_replaces_only_matching_rule(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_plugins(root, rule_threshold=10)
            runtime = self._bootstrap_runtime(root)
            try:
                old_rule = runtime.engine.rules["example.rule"]
                old_source = runtime.engine.sources["example.source"]
                old_output = runtime.engine.outputs["example.output"]

                self._write_plugins(root, rule_threshold=30)
                runtime.reload_now_if_changed()

                dirty = runtime.engine._plugin_status("rule", "example.rule")
                self.assertEqual(dirty.state, "DIRTY")
                self.assertEqual(dirty.dirty_reason, "definition_changed")

                summary = runtime.reload_now({"rule": "example.*"})

                new_rule = runtime.engine.rules["example.rule"]
                self.assertEqual(summary["rules"]["reloaded"], ["example.rule"])
                self.assertIsNot(new_rule, old_rule)
                self.assertIs(runtime.engine.sources["example.source"], old_source)
                self.assertIs(runtime.engine.outputs["example.output"], old_output)
                self.assertEqual(new_rule.high, 30)
                self.assertEqual(runtime.engine._plugin_status("rule", "example.rule").state, "READY")
            finally:
                runtime.api._server.server_close()
                runtime.engine.shutdown()

    def test_reload_all_forces_full_discovery_without_python_file_changes(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_dynamic_discovery_plugins(root)
            (root / "remote_rules.json").write_text(
                json.dumps({"rule_ids": ["dynamic.one"]}),
                encoding="utf-8",
            )
            runtime = self._bootstrap_runtime(root)
            try:
                self.assertIn("dynamic.one", runtime.engine.rules)
                self.assertNotIn("dynamic.two", runtime.engine.rules)

                (root / "remote_rules.json").write_text(
                    json.dumps({"rule_ids": ["dynamic.one", "dynamic.two"]}),
                    encoding="utf-8",
                )

                summary = runtime.reload_now({"all": True})

                self.assertEqual(summary["status"], "reloaded")
                self.assertIn("dynamic.one", runtime.engine.rules)
                self.assertIn("dynamic.two", runtime.engine.rules)
            finally:
                runtime.api._server.server_close()
                runtime.engine.shutdown()

    def test_reload_full_restarts_engine_and_reinitializes_sources(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_full_restart_plugins(root)
            with self.assertLogs("kanary.engine", level="WARNING"):
                runtime = self._bootstrap_runtime(root, output_emit_enabled=False)
            try:
                count_path = root / "init_count.txt"
                self.assertEqual(count_path.read_text(encoding="utf-8").strip(), "1")
                old_engine = runtime.engine

                with self.assertLogs("kanary.engine", level="WARNING"):
                    summary = runtime.reload_now({"full": True})

                self.assertEqual(summary["status"], "reloaded")
                self.assertEqual(summary["target"], "full")
                self.assertEqual(count_path.read_text(encoding="utf-8").strip(), "2")
                self.assertIsNot(runtime.engine, old_engine)
                self.assertIn("restart.rule", runtime.engine.rules)
                self.assertFalse(runtime.engine.output_emit_enabled)
            finally:
                runtime.api._server.server_close()
                runtime.engine.shutdown()

    def test_reload_full_keeps_old_engine_when_restart_fails(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_full_restart_plugins(root)
            runtime = self._bootstrap_runtime(root)
            try:
                count_path = root / "init_count.txt"
                fail_path = root / "fail_flag.txt"
                old_engine = runtime.engine
                fail_path.write_text("1", encoding="utf-8")

                summary = runtime.reload_now({"full": True})

                self.assertEqual(summary["status"], "reload_failed")
                self.assertEqual(summary["target"], "full")
                self.assertIs(runtime.engine, old_engine)
                self.assertEqual(count_path.read_text(encoding="utf-8").strip(), "1")
                self.assertIn("restart.rule", runtime.engine.rules)
            finally:
                runtime.api._server.server_close()
                runtime.engine.shutdown()

    def test_reload_source_preserves_source_state(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_plugins(root, output_label="v1")
            runtime = self._bootstrap_runtime(root)
            try:
                now = datetime(2026, 6, 4, 12, 0, tzinfo=timezone.utc)
                source = runtime.engine.sources["example.source"]
                runtime.engine.evaluate_source("example.source", source.poll({"now": now}), now=now)
                before = runtime.engine.source_states["example.source"]
                old_source = source

                self._write_plugins(root, output_label="v2")
                plugin_text = (root / "plugins.py").read_text()
                (root / "plugins.py").write_text(plugin_text.replace('version = "v2"', 'version = "reloaded"'))
                runtime.reload_now_if_changed()

                summary = runtime.reload_now({"source": "example.*"})

                after = runtime.engine.source_states["example.source"]
                new_source = runtime.engine.sources["example.source"]
                self.assertEqual(summary["sources"]["reloaded"], ["example.source"])
                self.assertIsNot(new_source, old_source)
                self.assertEqual(getattr(new_source, "version"), "reloaded")
                self.assertEqual(after.current.payload, before.current.payload)
                self.assertEqual(after.previous.payload, before.previous.payload)
                self.assertEqual(after.poll_count, before.poll_count)
            finally:
                runtime.api._server.server_close()
                runtime.engine.shutdown()

    def test_dirty_source_state_survives_normal_evaluation_until_reload(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_plugins(root, output_label="v1")
            runtime = self._bootstrap_runtime(root)
            try:
                self._write_plugins(root, output_label="changed")
                plugin_text = (root / "plugins.py").read_text()
                (root / "plugins.py").write_text(plugin_text.replace('version = "changed"', 'version = "dirty"'))
                next_tick = getattr(self, "_write_tick", 1_700_000_010)
                self._write_tick = next_tick + 2
                os.utime(root / "plugins.py", (self._write_tick, self._write_tick))
                runtime.reload_now_if_changed()

                status = runtime.engine._plugin_status("source", "example.source")
                self.assertEqual(status.state, "DIRTY")

                source = runtime.engine.sources["example.source"]
                now = datetime(2026, 6, 4, 12, 2, tzinfo=timezone.utc)
                runtime.engine.evaluate_source("example.source", source.poll({"now": now}), now=now)

                self.assertEqual(runtime.engine._plugin_status("source", "example.source").state, "DIRTY")
            finally:
                runtime.api._server.server_close()
                runtime.engine.shutdown()

    def test_reload_output_replaces_only_matching_output(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_plugins(root, output_label="v1")
            runtime = self._bootstrap_runtime(root)
            try:
                old_output = runtime.engine.outputs["example.output"]
                old_rule = runtime.engine.rules["example.rule"]
                old_source = runtime.engine.sources["example.source"]

                self._write_plugins(root, output_label="v2")
                runtime.reload_now_if_changed()

                dirty = runtime.engine._plugin_status("output", "example.output")
                self.assertEqual(dirty.state, "DIRTY")

                summary = runtime.reload_now({"output": "example.*"})

                new_output = runtime.engine.outputs["example.output"]
                self.assertEqual(summary["outputs"]["reloaded"], ["example.output"])
                self.assertIsNot(new_output, old_output)
                self.assertEqual(getattr(new_output, "label"), "v2")
                self.assertIs(runtime.engine.rules["example.rule"], old_rule)
                self.assertIs(runtime.engine.sources["example.source"], old_source)
            finally:
                runtime.api._server.server_close()
                runtime.engine.shutdown()

    def test_reload_dirty_unloads_pending_removed_rule(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_plugins(root)
            runtime = self._bootstrap_runtime(root)
            try:
                (root / "plugins.py").write_text(
                    textwrap.dedent(
                        """
                        import kanary

                        @kanary.source(source_id="example.source", interval=60.0)
                        class ExampleSource:
                            def poll(self, ctx):
                                return kanary.SourceResult(
                                    measurements=[
                                        kanary.Measurement(
                                            name="value",
                                            value=15,
                                            timestamp=ctx["now"],
                                        )
                                    ]
                                )

                        @kanary.output(output_id="example.output")
                        class ExampleOutput:
                            def emit(self, event, ctx):
                                return None
                        """
                    )
                )
                runtime.reload_now_if_changed()

                pending = runtime.engine._plugin_status("rule", "example.rule")
                self.assertEqual(pending.state, "PENDING_REMOVE")
                self.assertTrue(pending.loaded)

                summary = runtime.reload_now({"dirty": True})

                self.assertEqual(summary["rules"]["removed"], ["example.rule"])
                self.assertNotIn("example.rule", runtime.engine.rules)
                self.assertNotIn(runtime.engine._plugin_key("rule", "example.rule"), runtime.engine.plugin_states)
            finally:
                runtime.api._server.server_close()
                runtime.engine.shutdown()


class RuntimeRecoveryTest(unittest.TestCase):
    def test_runtime_does_not_start_threads_for_sources_with_failed_init(self) -> None:
        class BrokenSource(kanary.Source):
            source_id = "broken.source"
            interval = 60.0

            def init(self, ctx):
                raise RuntimeError("SC_POSTGRES_DSN is not set")

            def poll(self, ctx):
                return kanary.SourceResult()

        class HealthySource(kanary.Source):
            source_id = "healthy.source"
            interval = 60.0

            def poll(self, ctx):
                return kanary.SourceResult()

        runtime = EngineRuntime(RuntimeConfig(rule_directories=[], api_port=0))
        engine = kanary.Engine(
            source_registry={
                "broken.source": BrokenSource,
                "healthy.source": HealthySource,
            },
            rule_registry={},
            output_registry={},
        )
        engine.start()
        runtime.engine = engine
        try:
            runtime._sync_source_threads()
            self.assertNotIn("broken.source", runtime._source_threads)
            self.assertIn("healthy.source", runtime._source_threads)
        finally:
            runtime._stop_source_threads(set(runtime._source_threads))
            runtime.api._server.server_close()
            engine.shutdown()

    def test_source_poll_failure_recovers_with_retry_then_reinit(self) -> None:
        attempts: list[str] = []

        @kanary.source(source_id="recover.source", interval=60.0, max_retry=1, max_reinit=1)
        class RecoverSource:
            def __init__(self) -> None:
                self.poll_calls = 0
                self.init_calls = 0

            def init(self, ctx):
                self.init_calls += 1
                attempts.append("init")
                self.poll_calls = 0

            def poll(self, ctx):
                self.poll_calls += 1
                attempts.append(f"poll-{self.init_calls}-{self.poll_calls}")
                if self.init_calls == 1:
                    raise RuntimeError(f"boom-{self.init_calls}-{self.poll_calls}")
                return kanary.SourceResult()

            def terminate(self, ctx):
                attempts.append("terminate")

        class DummyStopEvent:
            def wait(self, seconds):
                attempts.append(f"wait-{seconds}")
                return False

            def is_set(self):
                return False

        runtime = EngineRuntime(RuntimeConfig(rule_directories=[], api_port=0))
        engine = kanary.Engine(
            source_registry={"recover.source": RecoverSource},
            rule_registry={},
            output_registry={},
        )
        engine.start()
        runtime.engine = engine
        source = engine.sources["recover.source"]
        try:
            attempts.clear()
            source.init_calls = 1
            source.poll_calls = 0
            result = runtime._poll_source_with_recovery(
                "recover.source",
                source,
                datetime(2026, 6, 4, 12, 0, tzinfo=timezone.utc),
                DummyStopEvent(),
            )
        finally:
            runtime.api._server.server_close()
            engine.shutdown()

        self.assertIsInstance(result, kanary.SourceResult)
        self.assertEqual(attempts, ["poll-1-1", "wait-1", "poll-1-2", "wait-4", "terminate", "init", "poll-2-1", "terminate"])


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

    def test_plugins_api_includes_error_detail(self) -> None:
        engine = kanary.Engine(
            output_registry={"broken-init": BrokenInitOutput},
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
            port = api._server.server_address[1]
            plugins_payload = fetch_json(f"http://127.0.0.1:{port}/plugins")
        finally:
            api.shutdown()
            thread.join(timeout=2.0)
            engine.shutdown()

        output_plugin = next(
            item
            for item in plugins_payload["plugins"]
            if item["type"] == "output" and item["plugin_id"] == "broken-init"
        )
        self.assertEqual(output_plugin["last_error"], "webhook is not set")
        self.assertIn("RuntimeError: webhook is not set", output_plugin["last_error_detail"])

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
            self.assertIn("output-emit-disabled-banner", body)
            self.assertIn("emit_skipped_outputs", javascript)
            self.assertIn("Dashboard", body)
        finally:
            api.shutdown()
            thread.join(timeout=2.0)
            engine.shutdown()

    def test_viewer_can_be_disabled(self) -> None:
        engine = kanary.Engine(output_registry={})
        engine.start()
        api = kanary.ControlAPI(
            engine_getter=lambda: engine,
            reload_callback=lambda: True,
            host="127.0.0.1",
            port=0,
            enable_default_viewer=False,
        )
        thread = threading.Thread(target=api.start, daemon=True)
        thread.start()
        try:
            port = api._server.server_address[1]
            with self.assertRaises(HTTPError) as viewer_error:
                urlopen(f"http://127.0.0.1:{port}/viewer")
            self.assertEqual(viewer_error.exception.code, 404)

            with self.assertRaises(HTTPError) as asset_error:
                urlopen(f"http://127.0.0.1:{port}/viewer/app.js")
            self.assertEqual(asset_error.exception.code, 404)

            with urlopen(f"http://127.0.0.1:{port}/health") as response:
                self.assertEqual(response.status, 200)
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

    def test_meta_endpoint_reports_repository_information(self) -> None:
        engine = kanary.Engine(output_registry={}, output_emit_enabled=False)
        with self.assertLogs("kanary.engine", level="WARNING") as captured:
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
            payload = fetch_json(f"http://127.0.0.1:{port}/meta")
            health = fetch_json(f"http://127.0.0.1:{port}/health")
        finally:
            api.shutdown()
            thread.join(timeout=2.0)
            engine.shutdown()

        self.assertEqual(payload["package_name"], "kanary")
        self.assertTrue(payload["version"])
        self.assertIn("git_commit", payload)
        self.assertEqual(payload["repository_url"], "https://github.com/mzks/kanary")
        self.assertIn("state_db_enabled", payload)
        self.assertIn("state_db_schema_version", payload)
        self.assertIn("state_db_target_schema_version", payload)
        self.assertFalse(payload["output_emit_enabled"])
        self.assertFalse(health["output_emit_enabled"])
        self.assertTrue(any("output emit is disabled" in line for line in captured.output))

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
        self.assertIn("description", alert_row)
        self.assertIn("runbook", alert_row)

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
        self.assertEqual(
            alert_row["description"],
            "Alert when the synthetic PostgreSQL temperature input stops updating.",
        )
        self.assertEqual(
            alert_row["runbook"],
            "https://example.invalid/runbooks/postgres-temperature-stale",
        )

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
    def test_new_sqlite_store_sets_user_version(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "kanary.db"
            store = kanary.SQLiteStore(db_path)
            store.initialize()
            try:
                conn = sqlite3.connect(db_path)
                try:
                    version = conn.execute("PRAGMA user_version").fetchone()[0]
                finally:
                    conn.close()
            finally:
                store.close()

        self.assertEqual(version, kanary.SQLiteStore.SCHEMA_VERSION)

    def test_legacy_schema_version_zero_is_rejected(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "kanary.db"
            conn = sqlite3.connect(db_path)
            try:
                conn.execute("CREATE TABLE legacy_table (id INTEGER PRIMARY KEY)")
                conn.commit()
            finally:
                conn.close()

            store = kanary.SQLiteStore(db_path)
            with self.assertRaisesRegex(RuntimeError, "unsupported legacy state DB schema"):
                store.initialize()

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
            RecordingOutput.events = []

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
                output_registry={"recording": RecordingOutput},
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
                source.now = now
                engine.evaluate_source(source.source_id, source.poll({}), now=now)
                source.now = now - timedelta(minutes=20)
                engine.evaluate_source(source.source_id, source.poll({}), now=now)
                engine.acknowledge("postgres.temperature.stale", operator="alice", reason="checking")
                engine.unacknowledge("postgres.temperature.stale", operator="alice", reason="re-open")
                port = api._server.server_address[1]
                payload = fetch_json(f"http://127.0.0.1:{port}/history/postgres.temperature.stale")
                self.assertTrue(payload["enabled"])
                self.assertGreaterEqual(len(payload["alert_events"]), 2)
                self.assertGreaterEqual(len(payload["output_dispatches"]), 3)
                self.assertTrue(all(row["delivered_outputs"] == ["recording"] for row in payload["output_dispatches"]))
                self.assertEqual(payload["operator_actions"][0]["action_type"], "unack")
                self.assertEqual(payload["operator_actions"][0]["operator"], "alice")
                self.assertEqual(payload["alert_events"][0]["transition"], "UNACK")
                self.assertEqual(payload["alert_events"][0]["previous_severity"], int(kanary.ERROR))
                self.assertEqual(payload["alert_events"][0]["current_severity"], int(kanary.ERROR))
            finally:
                api.shutdown()
                thread.join(timeout=2.0)
                engine.shutdown()

    def test_history_filters_include_output_dispatches(self) -> None:
        payload = {
            "enabled": True,
            "alert_events": [],
            "output_dispatches": [
                {"occurred_at": "2026-03-17T00:20:00+00:00"},
                {"occurred_at": "2026-03-16T00:20:00+00:00"},
            ],
            "operator_actions": [],
        }

        filtered = kanaryctl.apply_history_filters(
            payload,
            since="2026-03-17T00:00:00+00:00",
            limit=1,
        )

        self.assertEqual(len(filtered["output_dispatches"]), 1)
        self.assertEqual(filtered["output_dispatches"][0]["occurred_at"], "2026-03-17T00:20:00+00:00")

    def test_rule_removed_is_persisted_in_history(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "kanary.db"
            now = datetime(2026, 3, 17, 0, 20, tzinfo=timezone.utc)

            engine = kanary.Engine(
                now_fn=lambda: now,
                source_registry={"postgres": SlowPostgresSource},
                rule_registry={"postgres.temperature.stale": SlowPostgresStale},
                output_registry={},
                store=kanary.SQLiteStore(db_path),
            )
            engine.start()
            try:
                source = engine.sources["postgres"]
                engine.evaluate_source(source.source_id, source.poll({}), now=now)
                engine.reload(rule_registry={})
                history = engine.get_rule_history("postgres.temperature.stale")
                self.assertEqual(history["operator_actions"][0]["action_type"], "rule_removed")
                self.assertEqual(history["operator_actions"][0]["details"]["previous_state"], "FIRING")
                self.assertEqual(history["operator_actions"][0]["details"]["previous_severity"], int(kanary.ERROR))
            finally:
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


class BrokenInitSource(kanary.Source):
    source_id = "broken-init"
    interval = 60.0

    def init(self, ctx):
        raise RuntimeError("SC_POSTGRES_DSN is not set")

    def poll(self, ctx):
        return kanary.SourceResult()


class BrokenEmitOutput(kanary.Output):
    output_id = "broken-emit"

    def emit(self, event, ctx):
        raise RuntimeError("send failed")


class RecoveringEmitOutput(kanary.Output):
    output_id = "recovering-emit"
    max_retry = 1
    max_reinit = 1
    attempts = []

    def __init__(self) -> None:
        self.emit_calls = 0
        self.init_calls = 0

    def init(self, ctx):
        self.init_calls += 1
        type(self).attempts.append("init")
        self.emit_calls = 0

    def terminate(self, ctx):
        type(self).attempts.append("terminate")

    def emit(self, event, ctx):
        self.emit_calls += 1
        type(self).attempts.append(f"emit-{self.init_calls}-{self.emit_calls}")
        if self.init_calls == 1:
            raise RuntimeError(f"send failed #{self.init_calls}-{self.emit_calls}")


class FakeSMTP:
    sent_messages = []
    started_tls = False
    login_args = None

    def __init__(self, host, port, timeout=None):
        self.host = host
        self.port = port
        self.timeout = timeout

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def starttls(self):
        type(self).started_tls = True

    def login(self, username, password):
        type(self).login_args = (username, password)

    def send_message(self, message):
        type(self).sent_messages.append(message)


class TestMailOutput(kanary.MailOutput):
    output_id = "mail"
    smtp_host = "smtp.example.invalid"
    smtp_port = 2525
    sender = "kanary@example.invalid"
    recipients = ["operator@example.invalid"]


class OutputTest(unittest.TestCase):
    def setUp(self) -> None:
        RecordingOutput.events = []
        RecoveringEmitOutput.attempts = []
        FakeSMTP.sent_messages = []
        FakeSMTP.started_tls = False
        FakeSMTP.login_args = None
        self.now = datetime(2026, 3, 17, 0, 20, tzinfo=timezone.utc)
        self.engine = kanary.Engine(
            now_fn=lambda: self.now,
            output_registry={"recording": RecordingOutput},
        )
        self.engine.start()
        self.output_module = importlib.import_module("kanary.output")
        self.original_smtp = self.output_module.smtplib.SMTP
        self.output_module.smtplib.SMTP = FakeSMTP
        self.engine_module = importlib.import_module("kanary.engine")
        self.original_sleep = self.engine_module.time.sleep
        self.engine_module.time.sleep = lambda _seconds: None

    def tearDown(self) -> None:
        self.output_module.smtplib.SMTP = self.original_smtp
        self.engine_module.time.sleep = self.original_sleep
        self.engine.shutdown()

    def test_output_plugin_receives_alert_event_on_state_change(self) -> None:
        source = self.engine.sources["postgres"]
        self.engine.evaluate_source(source.source_id, source.poll({}), now=self.now)
        self.assertEqual(len(RecordingOutput.events), 0)

        source.now = self.now - timedelta(seconds=10)
        with self.assertLogs("kanary.engine", level="INFO") as captured:
            self.engine.evaluate_source(source.source_id, source.poll({}), now=self.now)

        self.assertGreaterEqual(len(RecordingOutput.events), 1)
        self.assertEqual(RecordingOutput.events[0].rule_id, "postgres.temperature.stale")
        self.assertTrue(
            any(
                "alert dispatch summary:" in line
                and "rule=postgres.temperature.stale" in line
                and "delivered=recording" in line
                for line in captured.output
            )
        )

    def test_output_plugin_receives_escalation_event(self) -> None:
        source = self.engine.sources["postgres"]
        source.temperature = 21
        self.engine.evaluate_source(source.source_id, source.poll({}), now=self.now)
        threshold_events = [event for event in RecordingOutput.events if event.rule_id == "postgres.temperature.threshold"]
        self.assertEqual(len(threshold_events), 0)

        self.now = self.now + timedelta(seconds=5)
        source.temperature = 25
        self.engine.evaluate_source(source.source_id, source.poll({}), now=self.now)

        threshold_events = [event for event in RecordingOutput.events if event.rule_id == "postgres.temperature.threshold"]
        self.assertEqual(len(threshold_events), 1)
        self.assertEqual(threshold_events[0].transition, kanary.ESCALATED)
        self.assertEqual(threshold_events[0].previous_severity, kanary.WARN)
        self.assertEqual(threshold_events[0].current_severity, kanary.ERROR)

    def test_output_plugin_receives_deescalation_by_default(self) -> None:
        source = self.engine.sources["postgres"]
        source.temperature = 29
        self.engine.evaluate_source(source.source_id, source.poll({}), now=self.now)

        self.now = self.now + timedelta(seconds=5)
        source.temperature = 26.5
        self.engine.evaluate_source(source.source_id, source.poll({}), now=self.now)

        threshold_events = [event for event in RecordingOutput.events if event.rule_id == "postgres.temperature.threshold"]
        self.assertEqual(len(threshold_events), 1)
        self.assertEqual(threshold_events[0].transition, kanary.DEESCALATED)

    def test_output_excludes_silenced_and_suppressed_states_by_default(self) -> None:
        output = self.engine.outputs["recording"]
        self.assertEqual(output.exclude_states, ["SUPPRESSED", "SILENCED"])

        for state in (kanary.SILENCED, kanary.SUPPRESSED):
            alert = kanary.Alert(
                rule_id="example.alert",
                state=state,
                severity=kanary.WARN,
            )
            event = kanary.AlertEvent(
                rule_id=alert.rule_id,
                previous_state=kanary.OK,
                current_state=state,
                previous_severity=kanary.WARN,
                current_severity=kanary.WARN,
                transition=None,
                alert=alert,
                occurred_at=self.now,
            )
            self.assertFalse(output.matches(event))

    def test_output_can_replace_or_extend_default_state_exclusions(self) -> None:
        class PlainOutput:
            output_id = "plain"

            def emit(self, event):
                return None

        class AllStateOutput(kanary.Output):
            output_id = "all-state"
            exclude_states = []

            def emit(self, event):
                return None

        class NoRecoveryOutput(kanary.Output):
            output_id = "no-recovery"
            exclude_states = kanary.Output.exclude_states + ["OK"]

            def emit(self, event):
                return None

        silenced_alert = kanary.Alert(
            rule_id="example.alert",
            state=kanary.SILENCED,
            severity=kanary.WARN,
        )
        silenced_event = kanary.AlertEvent(
            rule_id=silenced_alert.rule_id,
            previous_state=kanary.OK,
            current_state=kanary.SILENCED,
            previous_severity=kanary.WARN,
            current_severity=kanary.WARN,
            transition=None,
            alert=silenced_alert,
            occurred_at=self.now,
        )
        ok_alert = kanary.Alert(
            rule_id="example.alert",
            state=kanary.OK,
            severity=kanary.WARN,
        )
        ok_event = kanary.AlertEvent(
            rule_id=ok_alert.rule_id,
            previous_state=kanary.FIRING,
            current_state=kanary.OK,
            previous_severity=kanary.WARN,
            current_severity=kanary.WARN,
            transition=None,
            alert=ok_alert,
            occurred_at=self.now,
        )

        prepared_plain_output = self.output_module.prepare_output_class(PlainOutput)
        self.assertEqual(
            prepared_plain_output.exclude_states,
            ["SUPPRESSED", "SILENCED"],
        )
        self.assertTrue(AllStateOutput().matches(silenced_event))
        self.assertEqual(
            NoRecoveryOutput.exclude_states,
            ["SUPPRESSED", "SILENCED", "OK"],
        )
        self.assertTrue(self.engine.outputs["recording"].matches(ok_event))
        self.assertFalse(NoRecoveryOutput().matches(ok_event))

    def test_output_delivers_firing_after_silence_is_cancelled(self) -> None:
        source = self.engine.sources["postgres"]
        self.engine.evaluate_source(source.source_id, source.poll({}), now=self.now)
        silence = self.engine.create_silence(
            operator="alice",
            reason="maintenance",
            start_at=self.now - timedelta(minutes=1),
            end_at=self.now + timedelta(minutes=10),
            rule_patterns=["postgres.temperature.stale"],
        )

        self.engine.evaluate_source(source.source_id, source.poll({}), now=self.now)
        self.assertEqual(RecordingOutput.events, [])

        self.engine.cancel_silence(silence.silence_id, operator="alice")
        self.engine.evaluate_source(source.source_id, source.poll({}), now=self.now)

        stale_events = [
            event
            for event in RecordingOutput.events
            if event.rule_id == "postgres.temperature.stale"
        ]
        self.assertEqual(len(stale_events), 1)
        self.assertEqual(stale_events[0].previous_state, kanary.SILENCED)
        self.assertEqual(stale_events[0].current_state, kanary.FIRING)

    def test_output_emit_can_be_disabled_without_disabling_init_or_routing(self) -> None:
        RecordingOutput.events = []
        with TemporaryDirectory() as tmp:
            engine = kanary.Engine(
                now_fn=lambda: self.now,
                output_registry={"recording": RecordingOutput},
                store=kanary.SQLiteStore(Path(tmp) / "shadow.db"),
                output_emit_enabled=False,
            )
            with self.assertLogs("kanary.engine", level="WARNING"):
                engine.start()
            try:
                summary = engine.test_fire(
                    "postgres.temperature.range",
                    state=kanary.FIRING,
                    reason="shadow routing test",
                    now=self.now,
                )
                status = engine.plugin_states["output:recording"]
                history = engine.get_rule_history("postgres.temperature.range")
            finally:
                engine.shutdown()

        self.assertTrue(status.init_ok)
        self.assertEqual(status.run_count, 0)
        self.assertEqual(summary["matched_outputs"], ["recording"])
        self.assertEqual(summary["delivered_outputs"], [])
        self.assertEqual(summary["emit_skipped_outputs"], ["recording"])
        self.assertEqual(RecordingOutput.events, [])
        self.assertEqual(history["output_dispatches"][0]["emit_skipped_outputs"], ["recording"])

    def test_ack_and_unack_emit_events(self) -> None:
        source = self.engine.sources["postgres"]
        self.engine.evaluate_source(source.source_id, source.poll({}), now=self.now)
        self.assertEqual(len(RecordingOutput.events), 0)

        self.now = self.now + timedelta(seconds=1)
        self.engine.acknowledge(
            "postgres.temperature.stale",
            operator="alice",
            reason="investigating",
        )
        self.assertEqual(len(RecordingOutput.events), 1)
        self.assertEqual(RecordingOutput.events[-1].current_state, kanary.ACKED)
        self.assertIsNone(RecordingOutput.events[-1].transition)

        self.now = self.now + timedelta(seconds=1)
        self.engine.unacknowledge(
            "postgres.temperature.stale",
            operator="alice",
            reason="re-open",
        )
        self.assertEqual(len(RecordingOutput.events), 2)
        self.assertEqual(RecordingOutput.events[-1].current_state, kanary.FIRING)
        self.assertEqual(RecordingOutput.events[-1].transition, kanary.UNACK)

    def test_reload_keeps_first_notification_after_reload(self) -> None:
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

        self.assertEqual(len(RecordingOutput.events), 1)
        self.assertEqual(RecordingOutput.events[0].rule_id, "postgres.temperature.stale")

    def test_reload_does_not_drop_delayed_first_state_change(self) -> None:
        source = self.engine.sources["postgres"]
        source.now = self.now
        self.engine.evaluate_source(source.source_id, source.poll({}), now=self.now)
        self.assertEqual(self.engine.alerts["postgres.temperature.stale"].state, kanary.AlertState.OK)

        self.engine.reload(
            source_registry=kanary.get_source_registry(),
            rule_registry=kanary.get_rule_registry(),
            output_registry={"recording": RecordingOutput},
        )
        self.assertEqual(len(RecordingOutput.events), 0)

        self.now = self.now + timedelta(days=30)
        source = self.engine.sources["postgres"]
        source.now = self.now - timedelta(minutes=20)
        self.engine.evaluate_source(source.source_id, source.poll({}), now=self.now)

        self.assertEqual(len(RecordingOutput.events), 1)
        self.assertEqual(RecordingOutput.events[0].previous_state, kanary.AlertState.OK)
        self.assertEqual(RecordingOutput.events[0].current_state, kanary.AlertState.FIRING)

    def test_reload_logs_summary(self) -> None:
        with self.assertLogs("kanary.engine", level="INFO") as captured:
            self.engine.reload(
                source_registry=kanary.get_source_registry(),
                rule_registry=kanary.get_rule_registry(),
                output_registry={"recording": RecordingOutput},
            )
        self.assertTrue(any("reload applied:" in line for line in captured.output))

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
            self.assertEqual(status.state, "FAILED")
            self.assertFalse(status.init_ok)
            self.assertEqual(status.last_error, "webhook is not set")
            self.assertIn("RuntimeError: webhook is not set", status.last_error_detail or "")
            self.assertIsNotNone(status.last_updated_at)
        finally:
            engine.shutdown()

    def test_source_init_failure_is_recorded_without_crashing_engine_startup(self) -> None:
        engine = kanary.Engine(
            now_fn=lambda: self.now,
            source_registry={
                "broken-init": BrokenInitSource,
                "postgres": SlowPostgresSource,
            },
            output_registry={},
        )
        engine.start()
        try:
            broken_status = engine.plugin_states["source:broken-init"]
            healthy_status = engine.plugin_states["source:postgres"]
            self.assertEqual(broken_status.state, "FAILED")
            self.assertFalse(broken_status.init_ok)
            self.assertEqual(broken_status.last_error, "SC_POSTGRES_DSN is not set")
            self.assertIn("RuntimeError: SC_POSTGRES_DSN is not set", broken_status.last_error_detail or "")
            self.assertEqual(healthy_status.state, "READY")
            self.assertTrue(healthy_status.init_ok)
        finally:
            engine.shutdown()

    def test_output_skip_due_to_init_failure_is_logged(self) -> None:
        engine = kanary.Engine(
            now_fn=lambda: self.now,
            output_registry={"broken-init": BrokenInitOutput},
        )
        engine.start()
        try:
            source = engine.sources["postgres"]
            with self.assertLogs("kanary.engine", level="WARNING") as captured:
                engine.evaluate_source(source.source_id, source.poll({}), now=self.now)
                source.now = self.now - timedelta(seconds=10)
                engine.evaluate_source(source.source_id, source.poll({}), now=self.now)
            self.assertTrue(
                any("had matching outputs but none were initialized" in line for line in captured.output)
            )
        finally:
            engine.shutdown()

    def test_output_dispatch_summary_logs_uninitialized_outputs(self) -> None:
        engine = kanary.Engine(
            now_fn=lambda: self.now,
            output_registry={"broken-init": BrokenInitOutput},
        )
        engine.start()
        try:
            source = engine.sources["postgres"]
            engine.evaluate_source(source.source_id, source.poll({}), now=self.now)
            source.now = self.now - timedelta(seconds=10)
            with self.assertLogs("kanary.engine", level="INFO") as captured:
                engine.evaluate_source(source.source_id, source.poll({}), now=self.now)
            self.assertTrue(
                any(
                    "alert dispatch summary:" in line
                    and "rule=postgres.temperature.stale" in line
                    and "matched=broken-init" in line
                    and "uninitialized=broken-init" in line
                    for line in captured.output
                )
            )
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
            self.assertEqual(status.state, "FAILED")
            self.assertTrue(status.init_ok)
            self.assertEqual(status.last_error, "send failed")
            self.assertIn("RuntimeError: send failed", status.last_error_detail or "")
            self.assertIsNotNone(status.last_failure_at)
            self.assertIsNotNone(status.last_updated_at)
        finally:
            engine.shutdown()

    def test_output_emit_can_recover_with_reinit(self) -> None:
        engine = kanary.Engine(
            now_fn=lambda: self.now,
            output_registry={"recovering-emit": RecoveringEmitOutput},
        )
        engine.start()
        try:
            source = engine.sources["postgres"]
            engine.evaluate_source(source.source_id, source.poll({}), now=self.now)
            source.now = self.now - timedelta(seconds=10)
            engine.evaluate_source(source.source_id, source.poll({}), now=self.now)
            status = engine.plugin_states["output:recovering-emit"]
            self.assertEqual(status.state, "READY")
            self.assertIsNone(status.last_error)
        finally:
            engine.shutdown()

        self.assertEqual(RecoveringEmitOutput.attempts[:6], ["init", "emit-1-1", "emit-1-2", "terminate", "init", "emit-2-1"])

    def test_mail_output_sends_message(self) -> None:
        engine = kanary.Engine(
            now_fn=lambda: self.now,
            output_registry={"mail": TestMailOutput},
        )
        engine.start()
        try:
            source = engine.sources["postgres"]
            engine.evaluate_source(source.source_id, source.poll({}), now=self.now)
            source.now = self.now - timedelta(seconds=10)
            engine.evaluate_source(source.source_id, source.poll({}), now=self.now)
        finally:
            engine.shutdown()

        self.assertEqual(len(FakeSMTP.sent_messages), 1)
        message = FakeSMTP.sent_messages[0]
        self.assertEqual(message["From"], "kanary@example.invalid")
        self.assertEqual(message["To"], "operator@example.invalid")
        self.assertIn("postgres.temperature.stale", message["Subject"])
        self.assertTrue(FakeSMTP.started_tls)

    def test_output_can_exclude_unack_transition(self) -> None:
        class NoUnackOutput(kanary.Output):
            output_id = "no-unack"
            events = []
            exclude_transitions = ["UNACK"]

            def emit(self, event, ctx):
                self.events.append(event)

        NoUnackOutput.events = []
        engine = kanary.Engine(
            now_fn=lambda: self.now,
            output_registry={"no-unack": NoUnackOutput},
        )
        engine.start()
        try:
            source = engine.sources["postgres"]
            engine.evaluate_source(source.source_id, source.poll({}), now=self.now)
            engine.acknowledge("postgres.temperature.stale", operator="alice", reason="checking")
            engine.unacknowledge("postgres.temperature.stale", operator="alice", reason="re-open")
        finally:
            engine.shutdown()

        self.assertEqual([event.current_state for event in NoUnackOutput.events], [kanary.ACKED])

    def test_output_can_exclude_deescalation_transition(self) -> None:
        class NoDeescalationOutput(kanary.Output):
            output_id = "no-deescalation"
            events = []
            include_tags = ["threshold"]
            exclude_transitions = ["DEESCALATED"]

            def emit(self, event, ctx):
                self.events.append(event)

        NoDeescalationOutput.events = []
        engine = kanary.Engine(
            now_fn=lambda: self.now,
            output_registry={"no-deescalation": NoDeescalationOutput},
        )
        engine.start()
        try:
            source = engine.sources["postgres"]
            source.temperature = 29
            engine.evaluate_source(source.source_id, source.poll({}), now=self.now)

            self.now = self.now + timedelta(seconds=5)
            source.temperature = 26.5
            engine.evaluate_source(source.source_id, source.poll({}), now=self.now)
        finally:
            engine.shutdown()

        self.assertEqual(NoDeescalationOutput.events, [])

    def test_output_minimum_severity_keeps_error_recovery_notification(self) -> None:
        class ErrorOnlyOutput(kanary.Output):
            output_id = "error-only"
            events = []
            include_tags = ["threshold"]
            minimum_severity = "ERROR"

            def emit(self, event, ctx):
                self.events.append(event)

        ErrorOnlyOutput.events = []
        engine = kanary.Engine(
            now_fn=lambda: self.now,
            output_registry={"error-only": ErrorOnlyOutput},
        )
        engine.start()
        try:
            source = engine.sources["postgres"]
            source.temperature = 21
            engine.evaluate_source(source.source_id, source.poll({}), now=self.now)

            self.now = self.now + timedelta(seconds=5)
            source.temperature = 25
            engine.evaluate_source(source.source_id, source.poll({}), now=self.now)

            self.now = self.now + timedelta(seconds=5)
            source.temperature = 10
            engine.evaluate_source(source.source_id, source.poll({}), now=self.now)
        finally:
            engine.shutdown()

        self.assertEqual(
            [(event.transition, event.current_state) for event in ErrorOnlyOutput.events],
            [
                (kanary.ESCALATED, kanary.FIRING),
                (None, kanary.OK),
            ],
        )


class RemoteAlarmFactoryTest(unittest.TestCase):
    def test_factory_can_generate_prefixed_remote_alarm_rules(self) -> None:
        generated = kanary.import_remote_alarms(
            source="remote-api",
            remote_alarm_ids=[
                "postgres.temperature.stale",
                "postgres.temperature.range",
                "postgres.temperature.rate",
            ],
            prefix="imported",
            add_tags=["remote"],
            include_rule_ids=["postgres.temperature.*"],
            exclude_rule_ids=["*.rate"],
        )

        generated_ids = {cls.rule_id for cls in generated}
        self.assertIn("imported.postgres.temperature.stale", generated_ids)
        self.assertIn("imported.postgres.temperature.range", generated_ids)
        self.assertNotIn("imported.postgres.temperature.rate", generated_ids)
        generated_rule = next(cls for cls in generated if cls.rule_id == "imported.postgres.temperature.stale")
        self.assertIn("remote", generated_rule.tags)

    def test_factory_can_filter_remote_alarm_tags_with_glob(self) -> None:
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
            RemoteAPISource.base_url = None
            RemoteAPISource.url = f"http://127.0.0.1:{remote_api._server.server_address[1]}"
            generated = kanary.import_remote_alarms(
                source="remote-api",
                add_tags=["remote"],
                include_tags=["*gres"],
                exclude_tags=["compo*"],
            )
        finally:
            remote_api.shutdown()
            remote_thread.join(timeout=2.0)
            remote_engine.shutdown()

        generated_ids = {cls.rule_id for cls in generated}
        self.assertIn("postgres.temperature.stale", generated_ids)
        self.assertIn("postgres.temperature.range", generated_ids)
        self.assertIn("postgres.temperature.rate", generated_ids)
        self.assertNotIn("postgres.temperature_humidity.balance", generated_ids)
        generated_rule = next(cls for cls in generated if cls.rule_id == "postgres.temperature.stale")
        self.assertEqual(
            generated_rule.description,
            "Alert when the synthetic PostgreSQL temperature input stops updating.",
        )
        self.assertEqual(
            generated_rule.runbook,
            "https://example.invalid/runbooks/postgres-temperature-stale",
        )


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
            host="127.0.0.1",
            port=0,
        )
        self.thread = threading.Thread(target=self.api.start, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.api._server.server_address[1]}"

    def tearDown(self) -> None:
        self.api.shutdown()
        self.thread.join(timeout=2.0)
        self.engine.shutdown()

    def test_health_endpoint_returns_ok(self) -> None:
        with urlopen(f"{self.base_url}/health") as response:
            body = json.loads(response.read().decode())
        self.assertEqual(body["status"], "ok")
        self.assertIn("postgres", body["sources"])

    def test_alerts_endpoint_returns_alerts(self) -> None:
        with urlopen(f"{self.base_url}/alerts") as response:
            body = json.loads(response.read().decode())
        self.assertEqual(len(body["alerts"]), 8)

    def test_alerts_endpoint_uses_declared_rule_severity_for_matched_outputs(self) -> None:
        class CriticalOnlyOutput(kanary.Output):
            output_id = "critical-only"
            include_tags = ["threshold"]
            minimum_severity = "CRITICAL"

            def emit(self, event, ctx):
                return None

        engine = kanary.Engine(
            now_fn=lambda: self.now,
            output_registry={"critical-only": CriticalOnlyOutput},
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
            source.temperature = 25
            engine.evaluate_source(source.source_id, source.poll({}), now=self.now)
            with urlopen(f"http://127.0.0.1:{api._server.server_address[1]}/alerts") as response:
                body = json.loads(response.read().decode())
        finally:
            api.shutdown()
            thread.join(timeout=2.0)
            engine.shutdown()

        alert = next(item for item in body["alerts"] if item["rule_id"] == "postgres.temperature.threshold")
        self.assertEqual(alert["severity"], kanary.ERROR.value)
        self.assertEqual(alert["matched_outputs"], [])

    def test_reload_endpoint_returns_reloaded(self) -> None:
        request = Request(f"{self.base_url}/reload", method="POST")
        with urlopen(request) as response:
            body = json.loads(response.read().decode())
        self.assertEqual(body["status"], "reloaded")

    def test_reload_endpoint_empty_body_keeps_legacy_all_behavior(self) -> None:
        captured: dict[str, object] = {}
        api = kanary.ControlAPI(
            engine_getter=lambda: self.engine,
            reload_callback=lambda payload: captured.update(payload) or {"status": "reloaded"},
            host="127.0.0.1",
            port=0,
        )
        thread = threading.Thread(target=api.start, daemon=True)
        thread.start()
        try:
            request = Request(
                f"http://127.0.0.1:{api._server.server_address[1]}/reload",
                method="POST",
            )
            with urlopen(request) as response:
                body = json.loads(response.read().decode())
        finally:
            api.shutdown()
            thread.join(timeout=2.0)
        self.assertEqual(body["status"], "reloaded")
        self.assertEqual(captured, {})

    def test_reload_endpoint_forwards_target_payload(self) -> None:
        captured: dict[str, object] = {}
        api = kanary.ControlAPI(
            engine_getter=lambda: self.engine,
            reload_callback=lambda payload: captured.update(payload) or {"status": "reloaded"},
            host="127.0.0.1",
            port=0,
        )
        thread = threading.Thread(target=api.start, daemon=True)
        thread.start()
        try:
            request = Request(
                f"http://127.0.0.1:{api._server.server_address[1]}/reload",
                method="POST",
                data=json.dumps({"rule": "postgres.*"}).encode(),
                headers={"Content-Type": "application/json"},
            )
            with urlopen(request) as response:
                body = json.loads(response.read().decode())
        finally:
            api.shutdown()
            thread.join(timeout=2.0)
        self.assertEqual(body["status"], "reloaded")
        self.assertEqual(captured, {"rule": "postgres.*"})

    def test_reload_endpoint_forwards_full_payload(self) -> None:
        captured: dict[str, object] = {}
        api = kanary.ControlAPI(
            engine_getter=lambda: self.engine,
            reload_callback=lambda payload: captured.update(payload) or {"status": "reloaded"},
            host="127.0.0.1",
            port=0,
        )
        thread = threading.Thread(target=api.start, daemon=True)
        thread.start()
        try:
            request = Request(
                f"http://127.0.0.1:{api._server.server_address[1]}/reload",
                method="POST",
                data=json.dumps({"full": True}).encode(),
                headers={"Content-Type": "application/json"},
            )
            with urlopen(request) as response:
                body = json.loads(response.read().decode())
        finally:
            api.shutdown()
            thread.join(timeout=2.0)
        self.assertEqual(body["status"], "reloaded")
        self.assertEqual(captured, {"full": True})

    def test_plugins_endpoint_returns_output_status(self) -> None:
        with urlopen(f"{self.base_url}/plugins") as response:
            body = json.loads(response.read().decode())
        self.assertEqual(len(body["plugins"]), len(self.engine.plugin_states))
        self.assertIn("source", {plugin["type"] for plugin in body["plugins"]})
        self.assertIn("rule", {plugin["type"] for plugin in body["plugins"]})
        plugin_ids = {plugin["plugin_id"] for plugin in body["plugins"]}
        self.assertIn("postgres", plugin_ids)
        self.assertIn("postgres.temperature.stale", plugin_ids)
        self.assertTrue(all("last_updated_at" in plugin for plugin in body["plugins"]))
        self.assertTrue(all("loaded" in plugin for plugin in body["plugins"]))
        stale_rule = next(
            plugin for plugin in body["plugins"]
            if plugin["type"] == "rule" and plugin["plugin_id"] == "postgres.temperature.stale"
        )
        self.assertEqual(stale_rule["inputs"], ["postgres:temperature"])
        self.assertEqual(stale_rule["resolved_sources"], ["postgres"])
        self.assertEqual(
            stale_rule["description"],
            "Alert when the synthetic PostgreSQL temperature input stops updating.",
        )
        self.assertEqual(
            stale_rule["runbook"],
            "https://example.invalid/runbooks/postgres-temperature-stale",
        )

    def test_plugins_endpoint_includes_source_and_output_metadata(self) -> None:
        class DescribedOutput(kanary.Output):
            output_id = "described-output"
            description = "Deliver threshold alerts to an external sink."
            include_tags = ["threshold"]
            exclude_tags = ["noise"]
            exclude_states = ["SUPPRESSED"]
            exclude_transitions = ["DEESCALATED"]
            minimum_severity = "ERROR"

            def emit(self, event):
                return None

        engine = kanary.Engine(
            now_fn=lambda: self.now,
            output_registry={"described-output": DescribedOutput},
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
            engine.evaluate_source(source.source_id, source.poll({}), now=self.now)
            with urlopen(f"http://127.0.0.1:{api._server.server_address[1]}/plugins") as response:
                body = json.loads(response.read().decode())
        finally:
            api.shutdown()
            thread.join(timeout=2.0)
            engine.shutdown()

        source_plugin = next(
            plugin for plugin in body["plugins"]
            if plugin["type"] == "source" and plugin["plugin_id"] == "postgres"
        )
        output_plugin = next(
            plugin for plugin in body["plugins"]
            if plugin["type"] == "output" and plugin["plugin_id"] == "described-output"
        )

        self.assertEqual(
            source_plugin["description"],
            "Expose synthetic PostgreSQL temperature and humidity inputs for API tests.",
        )
        self.assertEqual(output_plugin["description"], "Deliver threshold alerts to an external sink.")
        self.assertEqual(output_plugin["include_tags"], ["threshold"])
        self.assertEqual(output_plugin["exclude_tags"], ["noise"])
        self.assertEqual(output_plugin["exclude_states"], ["SUPPRESSED"])
        self.assertEqual(output_plugin["exclude_transitions"], ["DEESCALATED"])
        self.assertEqual(output_plugin["minimum_severity"], "ERROR")

    def test_test_poll_endpoint_returns_normalized_payload(self) -> None:
        request = Request(f"{self.base_url}/test-poll/postgres", method="POST")
        with urlopen(request) as response:
            body = json.loads(response.read().decode())
        self.assertEqual(body["status"], "ok")
        self.assertIn("channels", body)
        self.assertIn("temperature", body["channels"])

    def test_test_evaluate_endpoint_returns_evaluation(self) -> None:
        request = Request(
            f"{self.base_url}/test-evaluate/postgres.temperature.range",
            method="POST",
            data=json.dumps(
                {
                    "payload": {
                        "inputs": {
                            "postgres:temperature": {
                                "value": 150,
                                "timestamp": self.now.isoformat(),
                            }
                        },
                        "status": "ok",
                    },
                    "now": self.now.isoformat(),
                }
            ).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urlopen(request) as response:
            body = json.loads(response.read().decode())
        self.assertEqual(body["rule_id"], "postgres.temperature.range")
        self.assertEqual(body["state"], "FIRING")
        self.assertEqual(body["resolved_sources"], ["postgres"])
        self.assertIn("would_emit_outputs", body)

    def test_test_evaluate_template_endpoint_returns_inputs_payload_template(self) -> None:
        with urlopen(f"{self.base_url}/test-evaluate-template/postgres.temperature.range") as response:
            body = json.loads(response.read().decode())
        self.assertEqual(body["rule_id"], "postgres.temperature.range")
        self.assertEqual(body["inputs"], ["postgres:temperature"])
        self.assertEqual(body["resolved_sources"], ["postgres"])
        self.assertIn("payload", body)
        self.assertEqual(
            sorted(body["payload"]["inputs"]),
            ["postgres:temperature"],
        )
        self.assertIn("single_input_shorthand", body)

    def test_test_evaluate_endpoint_accepts_prev_input_fields(self) -> None:
        request = Request(
            f"{self.base_url}/test-evaluate/postgres.temperature.rate",
            method="POST",
            data=json.dumps(
                {
                    "payload": {
                        "inputs": {
                            "postgres:temperature": {
                                "value": 150,
                                "timestamp": self.now.isoformat(),
                                "prev_value": 120,
                                "prev_timestamp": (self.now - timedelta(minutes=1)).isoformat(),
                            }
                        },
                        "status": "ok",
                    },
                    "now": self.now.isoformat(),
                }
            ).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urlopen(request) as response:
            body = json.loads(response.read().decode())
        self.assertEqual(body["rule_id"], "postgres.temperature.rate")
        self.assertEqual(body["state"], "FIRING")
        self.assertIn("rate", body["payload"])

    def test_test_evaluate_endpoint_rejects_legacy_channels_payload(self) -> None:
        request = Request(
            f"{self.base_url}/test-evaluate/postgres.temperature.range",
            method="POST",
            data=json.dumps(
                {
                    "payload": {
                        "channels": {
                            "temperature": {
                                "value": 150,
                                "timestamp": self.now.isoformat(),
                            }
                        },
                        "status": "ok",
                    },
                    "now": self.now.isoformat(),
                }
            ).encode(),
            headers={"Content-Type": "application/json"},
        )
        with self.assertRaises(HTTPError) as ctx:
            urlopen(request)
        self.assertEqual(ctx.exception.code, 400)

    def test_test_fire_endpoint_returns_dispatch_summary(self) -> None:
        RecordingOutput.events = []
        engine = kanary.Engine(
            now_fn=lambda: self.now,
            output_registry={"recording": RecordingOutput},
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
            payload = fetch_json(
                f"http://127.0.0.1:{api._server.server_address[1]}/test-fire/postgres.temperature.range",
                method="POST",
                body={"state": "FIRING", "reason": "delivery test"},
            )
        finally:
            api.shutdown()
            thread.join(timeout=2.0)
            engine.shutdown()
        self.assertTrue(payload["synthetic"])
        self.assertEqual(payload["current_state"], "FIRING")
        self.assertEqual(payload["delivered_outputs"], ["recording"])
        self.assertEqual(payload["emit_skipped_outputs"], [])
        self.assertTrue(RecordingOutput.events)


class CLIHelpersTest(unittest.TestCase):
    def test_matches_row_filter_supports_substring_and_glob(self) -> None:
        self.assertTrue(kanaryctl.matches_row_filter(["postgres.temperature.stale"], "temperature"))
        self.assertTrue(kanaryctl.matches_row_filter(["expert_db"], "expert_*"))
        self.assertFalse(kanaryctl.matches_row_filter(["expert_db"], "viewer_*"))

    def test_load_payload_argument_supports_inline_json(self) -> None:
        args = argparse.Namespace(payload_json='{"status":"ok"}', payload_file=None, payload_stdin=False)
        self.assertEqual(kanaryctl.load_payload_argument(args), {"status": "ok"})

    def test_load_payload_argument_rejects_non_object_json(self) -> None:
        args = argparse.Namespace(payload_json='["bad"]', payload_file=None, payload_stdin=False)
        with self.assertRaises(ValueError):
            kanaryctl.load_payload_argument(args)

    def test_main_help_mentions_run_is_optional(self) -> None:
        parser = kanary_main.build_parser()
        help_text = parser.format_help()
        self.assertIn("Run is the default command, so you can omit it", help_text)
        self.assertIn("kanary ./plugins", help_text)
        self.assertIn("--api-port PORT", help_text)
        self.assertIn("kanary run --help", help_text)
        self.assertIn("kanary lint ./plugins", help_text)
        self.assertIn("--no-output-emit", help_text)
        self.assertIn("--version", help_text)

    def test_run_parser_accepts_no_output_emit(self) -> None:
        parser = kanary_main.build_parser()
        args = parser.parse_args(["run", "./plugins", "--no-output-emit"])
        self.assertTrue(args.no_output_emit)

    def test_cli_version_helpers_return_project_version(self) -> None:
        self.assertRegex(kanary_main._kanary_version(), r"^\d+\.\d+\.\d+$")
        self.assertRegex(kanaryctl._kanary_version(), r"^\d+\.\d+\.\d+$")

    def test_python_dash_m_kanaryctl_module_exposes_main(self) -> None:
        self.assertIs(kanaryctl_module.main, kanaryctl.main)


if __name__ == "__main__":
    unittest.main()
