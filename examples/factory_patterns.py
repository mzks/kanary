from datetime import datetime, timezone

import kanary


def make_constant_source(
    *,
    source_id: str,
    interval: float,
    inputs: dict[str, float],
):
    def poll(self):
        now = datetime.now(timezone.utc)
        return kanary.inputs(inputs, timestamp=now)

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
    input_name: str,
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
            "inputs": f"{source}:{input_name}",
            "direction": direction,
            "thresholds": list(thresholds),
        },
    )
    return kanary.rule(
        rule_id=rule_id,
        severity=severity,
        tags=list(tags or []),
        owner=owner,
    )(cls)


FactoryDemoSource = make_constant_source(
    source_id="factory_demo",
    interval=30 * kanary.second,
    inputs={
        "temperature": 24.5,
        "humidity": 48.0,
    },
)


FactoryTemperatureThreshold = make_threshold_rule(
    rule_id="factory_demo.temperature.threshold",
    source="factory_demo",
    input_name="temperature",
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
    input_name="humidity",
    thresholds=[
        (60.0, kanary.WARN),
        (75.0, kanary.ERROR),
    ],
    tags=["factory", "demo"],
    owner="demo_owner",
)
