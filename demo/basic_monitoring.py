from datetime import datetime, timezone

import kanary


@kanary.source(source_id="demo", interval=10.0)
class DemoSource:
    def poll(self):
        return kanary.inputs([
            # (name, value, timestamp)
            ("temperature", 23.4, datetime.now(timezone.utc)),
        ])


@kanary.rule(rule_id="demo.temperature.high",
             inputs="demo:temperature",
             severity=kanary.WARN,
             tags=["demo"],)
class DemoTemperatureHigh:
    threshold = 25.0

    def evaluate(self, ctx):
        temperature = ctx.value()  # Only one input value is specified in the decorator

        return kanary.fire_if(temperature > self.threshold,
                               f"temperature={temperature} is higher than {self.threshold}",
        ) or kanary.ok(f"temperature={temperature} is within limit",)


@kanary.output(output_id="console",
               minimum_severity=kanary.WARN,
               include_tags=['demo'])
class ConsoleOutput:
    def emit(self, event):
        print(
            event.rule_id,
            event.current_state.value,
            event.current_severity.name,
            event.transition.value if event.transition else "-",
            event.message,
        )
