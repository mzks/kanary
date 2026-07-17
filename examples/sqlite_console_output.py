import json

import kanary


@kanary.output(output_id="console", exclude_states=[])
class ConsoleOutput:
    description = "Print alert transitions as JSON lines for local testing and debugging."

    def emit(self, event):
        print(
            json.dumps(
                {
                    "rule_id": event.rule_id,
                    "previous_state": (
                        event.previous_state.value if event.previous_state is not None else None
                    ),
                    "current_state": event.current_state.value,
                    "previous_severity": (
                        kanary.severity_label(event.previous_severity)
                        if event.previous_severity is not None else None
                    ),
                    "current_severity": kanary.severity_label(event.current_severity),
                    "transition": event.transition.value if event.transition else None,
                    "owner": event.owner,
                    "tags": list(event.tags),
                    "message": event.message,
                    "occurred_at": event.occurred_at.isoformat(),
                },
                ensure_ascii=False,
            )
        )
