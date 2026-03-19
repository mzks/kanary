from datetime import datetime, timezone

import kanary


@kanary.source(source_id="demo", interval=10.0)
class DemoSource:
    def poll(self, ctx):
        return kanary.SourceResult(
            measurements=[
                kanary.Measurement(
                    name="temperature",
                    value=23.4,
                    timestamp=datetime.now(timezone.utc),
                )
            ],
            status="ok",
        )


@kanary.rule(
    rule_id="demo.temperature.high",
    source="demo",
    severity=kanary.WARN,
    tags=["demo"],
    owner="demo_owner",
)
class DemoTemperatureHigh:
    threshold = 25.0

    def evaluate(self, payload, ctx):
        temperature = ctx.value("temperature")
        if temperature is None:
            return kanary.Evaluation(
                state=kanary.AlertState.OK,
                payload=payload,
                message="temperature is missing",
            )
        if temperature > self.threshold:
            return kanary.Evaluation(
                state=kanary.AlertState.FIRING,
                payload=payload,
                message=f"temperature={temperature} is higher than {self.threshold}",
            )
        return kanary.Evaluation(
            state=kanary.AlertState.OK,
            payload=payload,
            message=f"temperature={temperature} is within limit",
        )


@kanary.output(output_id="console")
class ConsoleOutput:
    def emit(self, event, ctx):
        print(event.rule_id, event.current_state.value, event.alert.message)
