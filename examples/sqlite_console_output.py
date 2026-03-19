import json

import kanary


@kanary.output(output_id="console")
class ConsoleOutput:

    def emit(self, event, ctx):
        print(
            json.dumps(
                {
                    "rule_id": event.rule_id,
                    "previous_state": (
                        event.previous_state.value if event.previous_state is not None else None
                    ),
                    "current_state": event.current_state.value,
                    "message": event.alert.message,
                    "occurred_at": event.occurred_at.isoformat(),
                },
                ensure_ascii=False,
            )
        )
