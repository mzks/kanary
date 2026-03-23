# Getting Started

This guide walks through a small monitoring setup by hand so you can understand the `Source -> Rule -> Output` flow.

The example reads the local machine's load average, fires an alert when it becomes too high, and records state changes to a file.

All code used in this guide is collected in [examples/getting_started.py](../examples/getting_started.py). The easiest way to start is to place that file in a watched directory, run it, and then edit it step by step.

## 1. Install And Start

The normal installation method is PyPI:

```bash
pip install kanary
```

Start Kanary:

```bash
kanary ./examples
```

If you want to access the Web viewer from another machine, bind explicitly:

```bash
kanary ./examples --api-host 0.0.0.0 --api-port 8000
```

## 2. Viewer

Once Kanary is running, open:

```text
http://127.0.0.1:8000/viewer
```

At this point you should see the plugins loaded from `examples/`.

## 3. Write A Source

Create a file such as `getting_started.py` in a watched directory.
The same source already exists in [examples/getting_started.py](../examples/getting_started.py).

```python
from datetime import datetime, timezone
import os

import kanary


@kanary.source(source_id="local_load", interval=10 * kanary.second)
class LocalLoadSource:
    def poll(self, ctx):
        load1, _, _ = os.getloadavg()
        cpu_count = os.cpu_count() or 1
        return kanary.SourceResult(
            measurements=[
                kanary.Measurement(
                    name="load1_per_cpu",
                    value=load1 / cpu_count,
                    timestamp=datetime.now(timezone.utc),
                    metadata={"raw_load1": load1, "cpu_count": cpu_count},
                ),
            ],
            status="ok",
        )
```

The minimum source interface is:

- `@kanary.source(source_id="...")`
- `poll(self, ctx)`
- return `kanary.SourceResult(...)`

`interval` controls how often the source is polled. The default is 60 seconds.
You can also implement `init(self, ctx)` and `terminate(self, ctx)`.

## 4. Write A Rule

Add a rule that fires when the load becomes high.

```python
@kanary.rule(
    rule_id="local_load.busy",
    source="local_load",
    severity=kanary.WARN,
    tags=["getting-started", "demo"],
    owner="demo_owner",
)
class LocalLoadBusy:
    description = "Alert when the 1-minute load average per CPU is high."
    runbook = "Run `uptime` or `top` on the monitored host."

    def evaluate(self, payload, ctx):
        load = ctx.value("load1_per_cpu")
        threshold = 0.70
        if load is None:
            return kanary.Evaluation(
                state=kanary.AlertState.OK,
                payload=payload,
                message="load1_per_cpu is missing",
            )
        if load > threshold:
            return kanary.Evaluation(
                state=kanary.AlertState.FIRING,
                payload=payload,
                message=f"load1_per_cpu={load:.2f} is over {threshold:.2f}",
            )
        return kanary.Evaluation(
            state=kanary.AlertState.OK,
            payload=payload,
            message=f"load1_per_cpu={load:.2f} is within the normal range",
        )
```

The minimum rule interface is:

- `@kanary.rule(rule_id="...", source="...")`
- `severity`
- `tags`
- `evaluate(self, payload, ctx)`
- return `kanary.Evaluation(...)`

High-level accessors such as `ctx.value("load1_per_cpu")` are usually enough.

## 5. Use Rule Helper Classes

The same idea can often be expressed more compactly with a built-in helper class.
For example, here is a `ThresholdRule`:

```python
@kanary.rule(
    rule_id="local_load.busy_threshold",
    source="local_load",
    severity=kanary.WARN,
    tags=["getting-started", "demo"],
    owner="demo_owner",
)
class LocalLoadBusyThreshold(kanary.ThresholdRule):
    measurement = "load1_per_cpu"
    direction = "high"
    thresholds = [
        (0.70, kanary.WARN),
        (1.00, kanary.ERROR),
    ]
```

With helper classes, you usually do not write `evaluate()`. Instead, you configure class variables.

For `ThresholdRule`, you typically edit:

- `measurement`
- `direction`
- `thresholds`
- `hysteresis` when you want to reduce chattering near a boundary

Other common helper-class settings:

- `StaleRule`
  - `measurement`, `timeout`
- `RangeRule`
  - `measurement`, `low`, `high`, `hysteresis`
- `RateRule`
  - `measurement`, `per_seconds`, `high`, `low`

Use a custom rule only when a helper class no longer matches the monitoring logic cleanly.

## 6. Write An Output

Outputs define where state changes go.
For a first example, a JSONL file is easy to understand.

```python
from pathlib import Path
import json

import kanary


@kanary.output(output_id="file", include_tags=["getting-started"])
class FileOutput:
    output_path = Path("getting_started_alerts.jsonl")

    def init(self, ctx):
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self.output_path.touch(exist_ok=True)

    def emit(self, event, ctx):
        record = {
            "rule_id": event.rule_id,
            "previous_state": event.previous_state.value if event.previous_state else None,
            "current_state": event.current_state.value,
            "severity": kanary.severity_label(int(event.alert.severity)),
            "message": event.alert.message,
            "occurred_at": event.occurred_at.isoformat(),
        }
        with self.output_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
```

This output only records alerts tagged with `getting-started`.

## 7. What Happens Next

Once you save the file, Kanary reloads the plugin definitions automatically.
In the viewer, you should see:

- a `local_load` source on the Plugins page
- `local_load.busy` and `local_load.busy_threshold` on the Alerts page
- a `file` output on the Outputs page

Each state change appends one line to `getting_started_alerts.jsonl`.

Changes to plugin files reload automatically. Changes to `src/kanary` require a process restart.

You can also inspect alerts with the CLI:

```bash
kanaryctl --base-url http://127.0.0.1:8000 alerts
```

The Web viewer and `kanaryctl` use the same API.

## 8. More Features

### BufferedSource

Use `kanary.BufferedSource` when you want to keep a short in-memory history inside the source itself.
It gives you helper methods such as:

- `history()`
- `latest()`
- `average_value()`
- `min_value()`
- `max_value()`
- `count()`
- `rate()`

You implement `fetch()` instead of `poll()`, and use that short history to produce derived measurements.

### Remote Kanary Nodes

You can read alerts from another Kanary node and mirror them as local rules.

- [examples/peer_monitoring.py](../examples/peer_monitoring.py)
- [examples/remote_alarm_import.py](../examples/remote_alarm_import.py)

Remote alert import uses `origin_node_id` and `mirror_path` to avoid import loops.

### Mail Output With Mailpit

`kanary.MailOutput` is a short helper for SMTP output.
For local testing, Mailpit is much easier than a real SMTP server.

```python
@kanary.output(output_id="mail", include_tags=["getting-started"])
class MailAlert(kanary.MailOutput):
    smtp_host = "127.0.0.1"
    smtp_port = 1025
    use_starttls = False
    sender = "kanary@example.test"
    recipients = ["operator@example.test"]
```

Start Mailpit with:

```bash
docker run --rm -p 1025:1025 -p 8025:8025 axllent/mailpit
```

Then open:

```text
http://127.0.0.1:8025
```

### ACK And Silence

After you confirm an alert, you can acknowledge it with `ACK`.
If you want to suppress notifications temporarily, create a silence.

Both are available from the Web viewer and from `kanaryctl`.

```bash
kanaryctl --base-url http://127.0.0.1:8000 ack local_load.busy --operator operator_name --reason "investigating"
kanaryctl --base-url http://127.0.0.1:8000 silence-for --operator operator_name --minutes 10 --rule 'local_load.*' --reason "load test in progress"
```

### Lint

Run lint before starting Kanary to catch definition mistakes early.

```bash
kanary lint ./examples
```

Lint checks things such as:

- missing or invalid references
- missing `owner`
- invalid `StaleRule.timeout`
- rules with no matching output

`lint ok` means the directory is loadable. Warnings do not stop execution, but should usually be reviewed before production use.

## Development Install

If you want to work from a source checkout while developing Kanary itself, use:

```bash
git clone https://github.com/mzks/kanary
cd kanary
uv sync
uv run python -m kanary ./examples
```

## 9. Next Documents

- [README.md](../README.md)
- [plugins.md](plugins.md)
- [operations.md](operations.md)
- [api.md](api.md)
