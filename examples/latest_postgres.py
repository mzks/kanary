# Source & alarm definition example for PostgreSQL

import os
from datetime import datetime, timezone

import psycopg
from psycopg.rows import dict_row

import kanary


@kanary.source(source_id="postgres", interval=30.0)
class SlowLatestSource:

    def init(self, ctx):
        # DSN format
        # host=*** port=**** dbname=*** user=*** password=*****
        dsn = os.environ["KANARY_POSTGRES_DSN"]
        self.conn = psycopg.connect(dsn, row_factory=dict_row)

    def poll(self, ctx):
        try:
            with self.conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT DISTINCT ON (name) ts, name, value
                    FROM v_records
                    WHERE name IN (
                        'data.env.baby.envmon00.temperature',
                        'data.env.baby.envmon00.humidity'
                    )
                    ORDER BY name, ts DESC
                    """
                )
                rows = cur.fetchall()
        except Exception as exc:
            return kanary.SourceResult(
                status="error",
                error=str(exc),
            )

        if not rows:
            return kanary.SourceResult(status="empty")

        measurements = []
        for row in rows:
            full_name = row["name"]
            short_name = full_name.rsplit(".", 1)[-1]
            measurements.append(
                kanary.Measurement(
                    name=short_name,
                    value=row["value"],
                    timestamp=row["ts"],
                    metadata={"source_name": full_name},
                )
            )

        return kanary.SourceResult(measurements=measurements, status="ok")

    def terminate(self, ctx):
        self.conn.close()


@kanary.rule(
    rule_id="postgres.temperature.stale",
    source="postgres",
    severity=kanary.ERROR,
    tags=["infra", "postgres"],
    owner="expert_db",
)
class SlowLatestStale(kanary.StaleRule):
    measurement = "temperature"
    timeout = 1 * kanary.minute


@kanary.rule(
    rule_id="postgres.temperature.range",
    source="postgres",
    severity=kanary.WARN,
    tags=["slow", "temperature"],
    owner="expert_env",
)
class SlowLatestHighTemperature(kanary.RangeRule):
    measurement = "temperature"
    low = 20.0
    high = 28.0


@kanary.rule(
    rule_id="postgres.connection.failed",
    source="postgres",
    severity=kanary.ERROR,
    tags=["infra", "postgres"],
    owner="expert_db",
)
class SlowLatestDbConnection:

    def evaluate(self, payload, ctx):
        status = payload.get("status")
        if status == "ok":
            return kanary.Evaluation(
                state=kanary.AlertState.OK,
                payload=payload,
                message="database query succeeded",
            )

        return kanary.Evaluation(
            state=kanary.AlertState.FIRING,
            payload=payload,
            message=payload.get("error") or f"source status={status}",
        )


@kanary.rule(
    rule_id="postgres.humidity.stale",
    source="postgres",
    severity=kanary.ERROR,
    tags=["slow", "humidity"],
    owner="expert_env",
)
class SlowLatestHumidityStale(kanary.StaleRule):
    measurement = "humidity"
    timeout = 1 * kanary.minute


@kanary.rule(
    rule_id="postgres.humidity.range",
    source="postgres",
    severity=kanary.WARN,
    tags=["slow", "humidity"],
    owner="expert_env",
)
class SlowLatestHumidityRange(kanary.RangeRule):
    measurement = "humidity"
    low = 20.0
    high = 60.0
