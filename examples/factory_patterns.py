from datetime import datetime, timezone

import kanary


def make_constant_source(
    *,
    source_id: str,
    interval: float,
    measurements: dict[str, float],
):
    def poll(self, ctx):
        now = datetime.now(timezone.utc)
        return kanary.SourceResult(
            measurements=[
                kanary.Measurement(name=name, value=value, timestamp=now)
                for name, value in measurements.items()
            ],
            status="ok",
        )

    cls_name = f"{source_id.replace('.', '_').title()}Source"
    cls = type(
        cls_name,
        (),
        {
            "poll": poll,
        },
    )
    return kanary.source(source_id=source_id, interval=interval)(cls)


def make_threshold_rule(
    *,
    rule_id: str,
    source: str,
    measurement: str,
    thresholds: list[tuple[float, int]],
    direction: str = "high",
    severity: int = kanary.WARN,
    tags: list[str] | None = None,
    owner: str | None = None,
):
    cls_name = f"{rule_id.replace('.', '_').title()}Rule"
    cls = type(
        cls_name,
        (kanary.ThresholdRule,),
        {
            "measurement": measurement,
            "direction": direction,
            "thresholds": list(thresholds),
        },
    )
    return kanary.rule(
        rule_id=rule_id,
        source=source,
        severity=severity,
        tags=list(tags or []),
        owner=owner,
    )(cls)


FactoryDemoSource = make_constant_source(
    source_id="factory_demo",
    interval=30 * kanary.second,
    measurements={
        "temperature": 24.5,
        "humidity": 48.0,
    },
)


FactoryTemperatureThreshold = make_threshold_rule(
    rule_id="factory_demo.temperature.threshold",
    source="factory_demo",
    measurement="temperature",
    thresholds=[
        (25.0, kanary.WARN),
        (28.0, kanary.ERROR),
    ],
    tags=["factory", "demo"],
    owner="demo_owner",
)


FactoryHumidityThreshold = make_threshold_rule(
    rule_id="factory_demo.humidity.threshold",
    source="factory_demo",
    measurement="humidity",
    thresholds=[
        (60.0, kanary.WARN),
        (75.0, kanary.ERROR),
    ],
    tags=["factory", "demo"],
    owner="demo_owner",
)
