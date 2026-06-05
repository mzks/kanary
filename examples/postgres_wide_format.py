# PostgreSQL example for a wide-format table.
#
# Best practices shown here:
# - keep the database connection in init()/terminate()
# - let poll() raise on connection/query failures so Kanary can retry/reinit
# - expose one row as multiple inputs
# - use source="..." as sugar for source-wide custom rules
#
# Pair this with examples/self_plugin_monitoring.py if you want alerts when the
# source plugin itself enters FAILED.

import os

import psycopg
from psycopg.rows import dict_row

import kanary


@kanary.source(source_id="postgres.wide", interval=30.0)
class WideEnvironmentSource:

    def init(self, ctx):
        self.conn = psycopg.connect(
            os.environ["KANARY_POSTGRES_DSN"],
            row_factory=dict_row,
        )

    def poll(self, ctx):
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT observed_at, temperature_c, humidity_pct, co2_ppm
                FROM env_samples_wide
                ORDER BY observed_at DESC
                LIMIT 1
                """
            )
            row = cur.fetchone()

        if row is None:
            return kanary.no_data(reason="env_samples_wide has no rows")

        observed_at = row["observed_at"]
        return kanary.inputs(
            [
                ("temperature", row["temperature_c"], observed_at, {"column": "temperature_c", "unit": "C"}),
                ("humidity", row["humidity_pct"], observed_at, {"column": "humidity_pct", "unit": "%"}),
                ("co2_ppm", row["co2_ppm"], observed_at, {"column": "co2_ppm", "unit": "ppm"}),
            ],
            metadata={"table": "env_samples_wide"},
        )

    def terminate(self, ctx):
        if hasattr(self, "conn"):
            self.conn.close()


@kanary.rule(
    rule_id="postgres.wide.temperature.range",
    inputs="postgres.wide:temperature",
    severity=kanary.WARN,
    tags=["postgres", "wide", "temperature"],
    owner="expert_env",
)
class WideTemperatureRange(kanary.RangeRule):
    low = 18.0
    high = 28.0


@kanary.rule(
    rule_id="postgres.wide.humidity.stale",
    inputs="postgres.wide:humidity",
    severity=kanary.ERROR,
    tags=["postgres", "wide", "humidity"],
    owner="expert_env",
)
class WideHumidityStale(kanary.StaleRule):
    timeout = 2 * kanary.minute


@kanary.rule(
    rule_id="postgres.wide.co2.levels",
    inputs="postgres.wide:co2_ppm",
    severity=kanary.WARN,
    tags=["postgres", "wide", "co2"],
    owner="expert_env",
)
class WideCo2Levels(kanary.ThresholdRule):
    direction = "high"
    thresholds = [
        (800.0, kanary.WARN),
        (1200.0, kanary.ERROR),
        (1800.0, kanary.CRITICAL),
    ]


@kanary.rule(
    rule_id="postgres.wide.environment.fresh",
    source="postgres.wide",
    severity=kanary.ERROR,
    tags=["postgres", "wide", "freshness"],
    owner="expert_env",
)
class WideEnvironmentFresh:
    timeout = 2 * kanary.minute

    def evaluate(self, payload, ctx):
        stale_inputs: list[str] = []
        missing_inputs: list[str] = []
        for item in ctx.inputs():
            if item.timestamp is None:
                missing_inputs.append(item.name)
                continue
            age_seconds = (ctx.now - item.timestamp).total_seconds()
            if age_seconds > self.timeout:
                stale_inputs.append(f"{item.name} ({kanary.format_time(age_seconds)})")

        result_payload = dict(payload)
        result_payload["stale_inputs"] = stale_inputs
        result_payload["missing_inputs"] = missing_inputs

        if stale_inputs or missing_inputs:
            details = stale_inputs + [f"{name} (timestamp missing)" for name in missing_inputs]
            return kanary.error(
                f"wide row contains stale inputs: {', '.join(details)}",
                extra=result_payload,
            )

        return kanary.ok(
            "all wide-format inputs are fresh",
            extra=result_payload,
        )
