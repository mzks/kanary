import os
import sqlite3
from datetime import datetime

import kanary


@kanary.source(source_id="sqlite", interval=5.0)
class SqliteSource:

    def init(self, ctx):
        db_path = os.environ.get("KANARY_SQLITE_PATH", "dev_data.db")
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row

    def poll(self, ctx):
        try:
            cur = self.conn.cursor()
            cur.execute(
                """
                SELECT name, ts, value
                FROM (
                    SELECT
                        name,
                        ts,
                        value,
                        ROW_NUMBER() OVER (PARTITION BY name ORDER BY ts DESC) AS rn
                    FROM dev_samples
                    WHERE name IN ('value1', 'value2', 'value3')
                )
                WHERE rn = 1
                """
            )
            rows = cur.fetchall()
        except Exception as exc:
            return kanary.SourceResult(status="error", error=str(exc))

        measurements = []
        for row in rows:
            measurements.append(
                kanary.Measurement(
                    name=row["name"],
                    value=row["value"],
                    timestamp=datetime.fromisoformat(row["ts"]),
                )
            )
        return kanary.SourceResult(measurements=measurements, status="ok" if rows else "empty")

    def terminate(self, ctx):
        if hasattr(self, "conn"):
            self.conn.close()


@kanary.rule(
    rule_id="sqlite.connection.failed",
    source="sqlite",
    severity=kanary.ERROR,
    tags=["infra", "sqlite"],
    owner="expert_db",
)
class SqliteConnectionFailed:

    def evaluate(self, payload, ctx):
        if payload.get("status") == "ok":
            return kanary.Evaluation(state=kanary.AlertState.OK, payload=payload, message="sqlite query ok")
        return kanary.Evaluation(
            state=kanary.AlertState.FIRING,
            payload=payload,
            message=payload.get("error") or f"source status={payload.get('status')}",
        )


@kanary.rule(
    rule_id="sqlite.value1.stale",
    source="sqlite",
    severity=kanary.ERROR,
    tags=["sqlite", "value1"],
    owner="expert_dev",
)
class Value1Stale:
    owner = "expert_dev"
    suppressed_by = ["sqlite.connection.failed"]
    timeout = 1 * kanary.minute

    def evaluate(self, payload, ctx):
        timestamp = ctx.timestamp("value1")
        if timestamp is None:
            return kanary.Evaluation(
                state=kanary.AlertState.FIRING,
                payload=payload,
                message="value1 timestamp is missing",
            )

        age_seconds = (ctx.now - timestamp).total_seconds()
        result_payload = dict(payload)
        result_payload["age_seconds"] = age_seconds
        if age_seconds > self.timeout:
            return kanary.Evaluation(
                state=kanary.AlertState.FIRING,
                payload=result_payload,
                message=f"value1 stale for {kanary.format_time(age_seconds)} (> {kanary.format_time(self.timeout)})",
            )

        return kanary.Evaluation(
            state=kanary.AlertState.OK,
            payload=result_payload,
            message=f"value1 age {kanary.format_time(age_seconds)}",
        )


@kanary.rule(
    rule_id="sqlite.value2.stale",
    source="sqlite",
    severity=kanary.ERROR,
    tags=["sqlite", "value2"],
    owner="expert_dev",
)
class Value2Stale(kanary.StaleRule):
    measurement = "value2"
    timeout = 1 * kanary.minute
    suppressed_by = ["sqlite.connection.failed"]


@kanary.rule(
    rule_id="sqlite.value3.stale",
    source="sqlite",
    severity=kanary.ERROR,
    tags=["sqlite", "value3"],
    owner="expert_dev",
)
class Value3Stale(kanary.StaleRule):
    measurement = "value3"
    timeout = 1 * kanary.minute
    suppressed_by = ["sqlite.connection.failed"]


@kanary.rule(
    rule_id="sqlite.value1.range",
    source="sqlite",
    severity=kanary.WARN,
    tags=["sqlite", "value1"],
    owner="expert_dev",
)
class Value1Range(kanary.RangeRule):
    measurement = "value1"
    low = 10.0
    lower_inclusive = False
    high = 20.0
    hysteresis = 1.0
    suppressed_by = ["sqlite.connection.failed"]


@kanary.rule(
    rule_id="sqlite.value2.range",
    source="sqlite",
    severity=kanary.WARN,
    tags=["sqlite", "value2"],
    owner="expert_dev",
)
class Value2Range:
    suppressed_by = ["sqlite.connection.failed"]
    low = 90.0
    high = 110.0

    def evaluate(self, payload, ctx):
        value2 = ctx.value("value2")
        if value2 is None:
            return kanary.Evaluation(
                state=kanary.AlertState.OK,
                payload=payload,
                message="value2 is missing",
            )
        if value2 < self.low or value2 > self.high:
            return kanary.Evaluation(
                state=kanary.AlertState.FIRING,
                payload=payload,
                message=f"value2={value2} out of range [{self.low}, {self.high}]",
            )
        return kanary.Evaluation(
            state=kanary.AlertState.OK,
            payload=payload,
            message=f"value2={value2} within range [{self.low}, {self.high}]",
        )


@kanary.rule(
    rule_id="sqlite.value3.range",
    source="sqlite",
    severity=kanary.WARN,
    tags=["sqlite", "value3"],
    owner="expert_dev",
)
class Value3Range:
    measurement = "value3"
    suppressed_by = ["sqlite.connection.failed"]
    low = 0.2
    high = 0.8
    lower_inclusive = True
    upper_inclusive = True

    def evaluate(self, payload, ctx):
        value = ctx.value(self.measurement)
        if value is None:
            return kanary.Evaluation(
                state=kanary.AlertState.OK,
                payload=payload,
                message=f"{self.measurement} is missing",
            )

        in_lower = value >= self.low if self.lower_inclusive else value > self.low
        in_upper = value <= self.high if self.upper_inclusive else value < self.high
        range_text = f"{'[' if self.lower_inclusive else '('}{self.low}, {self.high}{']' if self.upper_inclusive else ')'}"

        if in_lower and in_upper:
            return kanary.Evaluation(
                state=kanary.AlertState.OK,
                payload=payload,
                message=f"{self.measurement}={value} within range {range_text}",
            )

        return kanary.Evaluation(
            state=kanary.AlertState.FIRING,
            payload=payload,
            message=f"{self.measurement}={value} out of range {range_text}",
        )


@kanary.rule(
    rule_id="sqlite.values.balance",
    source="sqlite",
    severity=kanary.ERROR,
    tags=["sqlite", "composite"],
    owner="expert_dev",
)
class ValuesBalance:
    suppressed_by = ["sqlite.connection.failed"]

    def evaluate(self, payload, ctx):
        value1 = ctx.value("value1")
        value2 = ctx.value("value2")
        value3 = ctx.value("value3")

        if value1 is None or value2 is None or value3 is None:
            return kanary.Evaluation(
                state=kanary.AlertState.OK,
                payload=payload,
                message="one of value1/value2/value3 is missing",
            )

        expected_value2 = value1 * (4.0 + value3)
        delta = value2 - expected_value2
        result_payload = dict(payload)
        result_payload["expected_value2"] = expected_value2
        result_payload["delta"] = delta

        if abs(delta) <= 10.0:
            return kanary.Evaluation(
                state=kanary.AlertState.OK,
                payload=result_payload,
                message=(
                    f"value2={value2} consistent with value1={value1} "
                    f"and value3={value3} (expected {expected_value2:.2f})"
                ),
            )

        return kanary.Evaluation(
            state=kanary.AlertState.FIRING,
            payload=result_payload,
            message=(
                f"value2={value2} inconsistent with value1={value1} "
                f"and value3={value3} (expected {expected_value2:.2f}, delta {delta:.2f})"
            ),
        )


@kanary.rule(
    rule_id="sqlite.value1.temperature_levels",
    source="sqlite",
    severity=kanary.WARN,
    tags=["sqlite", "value1", "threshold"],
    owner="expert_dev",
)
class Value1TemperatureLevels(kanary.ThresholdRule):
    measurement = "value1"
    direction = "high"
    hysteresis = 1.0
    thresholds = [
        (20.0, kanary.WARN),
        (24.0, kanary.ERROR),
        (28.0, kanary.CRITICAL),
    ]
    suppressed_by = ["sqlite.connection.failed"]


@kanary.rule(
    rule_id="sqlite.values.balance.levels",
    source="sqlite",
    severity=kanary.WARN,
    tags=["sqlite", "composite", "threshold"],
    owner="expert_dev",
)
class ValuesBalanceLevels:
    suppressed_by = ["sqlite.connection.failed"]

    def evaluate(self, payload, ctx):
        value1 = ctx.value("value1")
        value2 = ctx.value("value2")
        value3 = ctx.value("value3")

        if value1 is None or value2 is None or value3 is None:
            return kanary.Evaluation(
                state=kanary.AlertState.OK,
                payload=payload,
                message="one of value1/value2/value3 is missing",
            )

        expected_value2 = value1 * (4.0 + value3)
        delta = value2 - expected_value2
        absolute_delta = abs(delta)
        result_payload = dict(payload)
        result_payload["expected_value2"] = expected_value2
        result_payload["delta"] = delta

        if absolute_delta < 10.0:
            return kanary.Evaluation(
                state=kanary.AlertState.OK,
                payload=result_payload,
                message=f"balance delta {delta:.2f} within nominal range",
            )
        if absolute_delta < 20.0:
            severity = kanary.WARN
        elif absolute_delta < 30.0:
            severity = kanary.ERROR
        else:
            severity = kanary.CRITICAL

        return kanary.Evaluation(
            state=kanary.AlertState.FIRING,
            payload=result_payload,
            message=f"balance delta {delta:.2f} exceeded level threshold",
            severity=severity,
        )
