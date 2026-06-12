# PostgreSQL example for a long-format table.
#
# Best practices shown here:
# - normalize upstream metric names to stable Kanary input names
# - keep raw metric names in metadata for debugging
# - compare multiple inputs in one custom rule with ctx.inputs()/ctx.values()
# - rely on runtime retry/reinit for database failures instead of hiding them
#   inside ordinary source payloads
# - keep connection settings in a local TOML file next to this plugin
# - remember that changing the TOML requires an explicit reload because only
#   Python files are watched automatically

from pathlib import Path
import tomllib

import psycopg
from psycopg.rows import dict_row

import kanary

CONFIG_PATH = Path(__file__).with_name("postgres_long_format_config.toml")


def load_config() -> dict:
    with CONFIG_PATH.open("rb") as handle:
        return tomllib.load(handle)

INPUT_NAME_MAP = {
    "plant.room_a.temperature_c": "room_a.temperature",
    "plant.room_b.temperature_c": "room_b.temperature",
    "plant.room_a.humidity_pct": "room_a.humidity",
}


@kanary.source(source_id="postgres.long", interval=30.0)
class LongEnvironmentSource:

    def init(self, ctx):
        config = load_config()
        dsn = str(config.get("dsn") or "").strip()
        if not dsn:
            raise RuntimeError(f"{CONFIG_PATH.name} must define dsn")
        connect_timeout = int(config.get("connect_timeout_seconds", 5))
        statement_timeout_ms = int(config.get("statement_timeout_ms", 5000))
        self.conn = psycopg.connect(
            dsn,
            row_factory=dict_row,
            connect_timeout=connect_timeout,
            options=f"-c statement_timeout={statement_timeout_ms}",
        )

    def poll(self, ctx):
        upstream_names = tuple(INPUT_NAME_MAP)
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT ON (metric_name) observed_at, metric_name, metric_value
                FROM env_samples_long
                WHERE metric_name = ANY(%s)
                ORDER BY metric_name, observed_at DESC
                """,
                (list(upstream_names),),
            )
            rows = cur.fetchall()

        if not rows:
            return kanary.no_data(reason="env_samples_long has no rows")

        items = []
        for row in rows:
            upstream_name = row["metric_name"]
            kanary_name = INPUT_NAME_MAP[upstream_name]
            items.append(
                (
                    kanary_name,
                    row["metric_value"],
                    row["observed_at"],
                    {"metric_name": upstream_name},
                )
            )

        return kanary.inputs(
            items,
            metadata={"table": "env_samples_long"},
        )

    def terminate(self, ctx):
        if hasattr(self, "conn"):
            self.conn.close()


@kanary.rule(
    rule_id="postgres.long.room_a.temperature.range",
    inputs="postgres.long:room_a.temperature",
    severity=kanary.WARN,
    tags=["postgres", "long", "temperature", "room_a"],
    owner="expert_env",
)
class LongRoomATemperatureRange(kanary.RangeRule):
    low = 18.0
    high = 28.0


@kanary.rule(
    rule_id="postgres.long.room_a.humidity.stale",
    inputs="postgres.long:room_a.humidity",
    severity=kanary.ERROR,
    tags=["postgres", "long", "humidity", "room_a"],
    owner="expert_env",
)
class LongRoomAHumidityStale(kanary.StaleRule):
    timeout = 2 * kanary.minute


@kanary.rule(
    rule_id="postgres.long.room_temperature.delta",
    inputs="postgres.long:room_*.temperature",
    severity=kanary.ERROR,
    tags=["postgres", "long", "temperature", "delta"],
    owner="expert_env",
)
class LongRoomTemperatureDelta:
    max_delta = 3.0

    def evaluate(self, payload, ctx):
        matched_inputs = ctx.inputs()
        if len(matched_inputs) < 2:
            return kanary.ok("room temperature delta requires at least two matched inputs")

        values = {item.name: item.value for item in matched_inputs}
        delta = max(values.values()) - min(values.values())
        result_payload = dict(payload)
        result_payload["matched_inputs"] = values
        result_payload["delta"] = delta

        if delta > self.max_delta:
            return kanary.error(
                f"room temperature delta {delta:.1f} exceeded {self.max_delta:.1f}",
                extra=result_payload,
            )

        return kanary.ok(
            f"room temperature delta {delta:.1f}",
            extra=result_payload,
        )


@kanary.rule(
    rule_id="postgres.long.room_temperature.average.levels",
    inputs="postgres.long:room_*.temperature",
    severity=kanary.WARN,
    tags=["postgres", "long", "temperature", "average"],
    owner="expert_env",
)
class LongRoomTemperatureAverageLevels:

    def evaluate(self, payload, ctx):
        values = ctx.values()
        if not values:
            return kanary.ok("no room temperatures are available")

        average = sum(values) / len(values)
        result_payload = dict(payload)
        result_payload["average_temperature"] = average
        result_payload["matched_inputs"] = {
            item.name: item.value for item in ctx.inputs()
        }

        if average >= 30.0:
            severity = kanary.CRITICAL
        elif average >= 27.0:
            severity = kanary.ERROR
        elif average >= 24.0:
            severity = kanary.WARN
        else:
            return kanary.ok(
                f"average room temperature {average:.1f}C",
                extra=result_payload,
            )

        return kanary.firing(
            f"average room temperature {average:.1f}C exceeded threshold",
            severity=severity,
            extra=result_payload,
        )
