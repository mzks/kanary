import sqlite3
from datetime import datetime

import kanary

# SQLite example that focuses on measurement-level monitoring.
# Query failures are treated as source plugin failures so Kanary's runtime
# retry/reinit logic can recover them. Pair this example with
# examples/self_plugin_monitoring.py if you want alerts about failed plugins.

@kanary.source(source_id="sqlite", interval=5.0)
class SqliteSource:

    def init(self):
        config = kanary.load_toml(filename="sqlite_monitoring_config.toml")
        db_path = str(config.get("db_path", "dev_data.db"))
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row

    def poll(self):
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

        if not rows:
            return kanary.no_data(reason="dev_samples has no latest rows")
        return kanary.inputs(
            [
                (row["name"], row["value"], datetime.fromisoformat(row["ts"]))
                for row in rows
            ]
        )

    def terminate(self):
        if hasattr(self, "conn"):
            self.conn.close()


@kanary.rule(
    rule_id="sqlite.value1.stale",
    source="sqlite",
    severity=kanary.ERROR,
    tags=["sqlite", "value1"],
    owner="expert_dev",
)
class Value1Stale:
    owner = "expert_dev"
    timeout = 1 * kanary.minute

    def evaluate(self, ctx):
        timestamp = ctx.timestamp("value1")
        if timestamp is None:
            return kanary.error("value1 timestamp is missing")

        age_seconds = (ctx.now - timestamp).total_seconds()
        result_payload = ctx.source_payload()
        result_payload["age_seconds"] = age_seconds
        if age_seconds > self.timeout:
            return kanary.error(
                f"value1 stale for {kanary.format_time(age_seconds)} (> {kanary.format_time(self.timeout)})",
                extra=result_payload,
            )

        return kanary.ok(
            f"value1 age {kanary.format_time(age_seconds)}",
            extra=result_payload,
        )


@kanary.rule(
    rule_id="sqlite.value2.stale",
    inputs="sqlite:value2",
    severity=kanary.ERROR,
    tags=["sqlite", "value2"],
    owner="expert_dev",
)
class Value2Stale(kanary.StaleRule):
    timeout = 1 * kanary.minute


@kanary.rule(
    rule_id="sqlite.value3.stale",
    inputs="sqlite:value3",
    severity=kanary.ERROR,
    tags=["sqlite", "value3"],
    owner="expert_dev",
)
class Value3Stale(kanary.StaleRule):
    timeout = 1 * kanary.minute


@kanary.rule(
    rule_id="sqlite.value1.range",
    inputs="sqlite:value1",
    severity=kanary.WARN,
    tags=["sqlite", "value1"],
    owner="expert_dev",
)
class Value1Range(kanary.RangeRule):
    low = 10.0
    lower_inclusive = False
    high = 20.0
    hysteresis = 1.0


@kanary.rule(
    rule_id="sqlite.value2.range",
    source="sqlite",
    severity=kanary.WARN,
    tags=["sqlite", "value2"],
    owner="expert_dev",
)
class Value2Range:
    low = 90.0
    high = 110.0

    def evaluate(self, ctx):
        value2 = ctx.value("value2")
        if value2 is None:
            return kanary.ok("value2 is missing")
        return kanary.error_if(
            value2 < self.low or value2 > self.high,
            f"value2={value2} out of range [{self.low}, {self.high}]",
        ) or kanary.ok(f"value2={value2} within range [{self.low}, {self.high}]")


@kanary.rule(
    rule_id="sqlite.value3.range",
    source="sqlite",
    severity=kanary.WARN,
    tags=["sqlite", "value3"],
    owner="expert_dev",
)
class Value3Range:
    low = 0.2
    high = 0.8
    lower_inclusive = True
    upper_inclusive = True

    def evaluate(self, ctx):
        value = ctx.value("value3")
        if value is None:
            return kanary.ok("value3 is missing")

        in_lower = value >= self.low if self.lower_inclusive else value > self.low
        in_upper = value <= self.high if self.upper_inclusive else value < self.high
        range_text = f"{'[' if self.lower_inclusive else '('}{self.low}, {self.high}{']' if self.upper_inclusive else ')'}"

        if in_lower and in_upper:
            return kanary.ok(f"value3={value} within range {range_text}")

        return kanary.error(f"value3={value} out of range {range_text}")


@kanary.rule(
    rule_id="sqlite.values.balance",
    source="sqlite",
    severity=kanary.ERROR,
    tags=["sqlite", "composite"],
    owner="expert_dev",
)
class ValuesBalance:
    def evaluate(self, ctx):
        value1 = ctx.value("value1")
        value2 = ctx.value("value2")
        value3 = ctx.value("value3")

        if value1 is None or value2 is None or value3 is None:
            return kanary.ok("one of value1/value2/value3 is missing")

        expected_value2 = value1 * (4.0 + value3)
        delta = value2 - expected_value2
        result_payload = ctx.source_payload()
        result_payload["expected_value2"] = expected_value2
        result_payload["delta"] = delta

        if abs(delta) <= 10.0:
            return kanary.ok(
                (
                    f"value2={value2} consistent with value1={value1} "
                    f"and value3={value3} (expected {expected_value2:.2f})"
                ),
                extra=result_payload,
            )

        return kanary.error(
            (
                f"value2={value2} inconsistent with value1={value1} "
                f"and value3={value3} (expected {expected_value2:.2f}, delta {delta:.2f})"
            ),
            extra=result_payload,
        )


@kanary.rule(
    rule_id="sqlite.value1.temperature_levels",
    inputs="sqlite:value1",
    severity=kanary.WARN,
    tags=["sqlite", "value1", "threshold"],
    owner="expert_dev",
)
class Value1TemperatureLevels(kanary.ThresholdRule):
    direction = "high"
    hysteresis = 1.0
    thresholds = [
        (20.0, kanary.WARN),
        (24.0, kanary.ERROR),
        (28.0, kanary.CRITICAL),
    ]


@kanary.rule(
    rule_id="sqlite.values.balance.levels",
    source="sqlite",
    severity=kanary.WARN,
    tags=["sqlite", "composite", "threshold"],
    owner="expert_dev",
)
class ValuesBalanceLevels:
    def evaluate(self, ctx):
        value1 = ctx.value("value1")
        value2 = ctx.value("value2")
        value3 = ctx.value("value3")

        if value1 is None or value2 is None or value3 is None:
            return kanary.ok("one of value1/value2/value3 is missing")

        expected_value2 = value1 * (4.0 + value3)
        delta = value2 - expected_value2
        absolute_delta = abs(delta)
        result_payload = ctx.source_payload()
        result_payload["expected_value2"] = expected_value2
        result_payload["delta"] = delta

        if absolute_delta < 10.0:
            return kanary.ok(
                f"balance delta {delta:.2f} within nominal range",
                extra=result_payload,
            )
        if absolute_delta < 20.0:
            severity = kanary.WARN
        elif absolute_delta < 30.0:
            severity = kanary.ERROR
        else:
            severity = kanary.CRITICAL

        return kanary.firing(
            f"balance delta {delta:.2f} exceeded level threshold",
            severity=severity,
            extra=result_payload,
        )
