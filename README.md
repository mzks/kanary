# Kanary

Kanary is a Python-based alerting, notification, and reliability monitoring runtime inspired by the historical “canary in a coal mine”
You define three kinds of plugins in Python:

- `Source`
  Reads values from a system, database, API, or device.
- `Rule`
  Evaluates those values and decides whether an alert should fire.
- `Output`
  Sends state changes to humans or other systems.

This separation keeps collection, evaluation, and notification independent, so monitoring definitions stay manageable as the system grows.

## Installation

The normal installation method is PyPI:

```bash
pip install kanary
```

If you use `uv`, this also works:

```bash
uv tool install kanary
```
Then `kanary` and `kanaryctl` executables will be installed.

Installing from a source checkout is still supported, but it should be treated as a development workflow:

```bash
git clone https://github.com/mzks/kanary
cd kanary
uv sync
uv run python -m kanary ./demo
```

## What To Do First

Start by running the smallest example from `demo/`.

```bash
kanary ./demo
```

Then move in this order:

1. Read [demo/basic_monitoring.py](demo/basic_monitoring.py) to understand the smallest possible `Source`, `Rule`, and `Output`.
2. Read [docs/getting_started.md](docs/getting_started.md) and work through the examples in [examples/getting_started.py](examples/getting_started.py).
3. Browse `examples/` for PostgreSQL, Discord, peer monitoring, and remote alert import.
4. Create your own `plugins/` directory and start with one `Source` and one `Rule`.

## Minimal Example

The smallest working example is in [demo/basic_monitoring.py](demo/basic_monitoring.py).
Make a directory to place plugin files then put the following scripts.

```python
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
```

In this example, you only implement the minimum interface:

- a source that returns values
- a rule that evaluates them
- an output that reacts to state changes

Internally, Kanary handles plugin loading, periodic source polling, rule evaluation, alert state tracking, the HTTP API, and the Web viewer.

If you want shorter definitions later, you can switch to built-in helper classes such as `RangeRule`, `StaleRule`, and `ThresholdRule`.
Users can create plugin class factory too.

## Running Kanary

Basic run:

```bash
kanary ./demo
```

Change the API and Web viewer port:

```bash
kanary ./demo --api-port 8000
```

Expose the API and Web viewer on the LAN:

```bash
kanary ./demo --api-host 0.0.0.0 --api-port 8000
```

Persist history in SQLite:

```bash
kanary ./demo --state-db ./var/kanary.db
```

The Web viewer is available at:

```text
http://<host>:8000/viewer
```

See all CLI options with:

```bash
kanary --help
kanaryctl help
```

## Environment Variables

Kanary does not require any environment variables by default.
You can use these when needed:

- `KANARY_SQLITE_PATH`
  Alternative way to set the SQLite database path.
- `KANARY_API_URL`
  Default API base URL for `kanaryctl`.
- `KANARY_API_HOST`
  Bind host for the local API and Web viewer. The default is `0.0.0.0`.
- `KANARY_NODE_ID`
  Optional node identifier for peer export and import. If unset, Kanary uses the hostname.

Connection details for actual monitoring targets are defined by each `Source` implementation. For example, a PostgreSQL source may use `KANARY_POSTGRES_DSN`.

## Demo And Examples

Smallest example:

- [demo/basic_monitoring.py](demo/basic_monitoring.py)

More realistic examples:

- [examples/getting_started.py](examples/getting_started.py)
- [examples/factory_patterns.py](examples/factory_patterns.py)
- [examples/fake_alarm_monitoring.py](examples/fake_alarm_monitoring.py)
- [examples/fake_alarm_target.py](examples/fake_alarm_target.py)
- [examples/sqlite_monitoring.py](examples/sqlite_monitoring.py)
- [examples/sqlite_console_output.py](examples/sqlite_console_output.py)
- [examples/discord_webhook_output.py](examples/discord_webhook_output.py)
- [examples/latest_postgres.py](examples/latest_postgres.py)
- [examples/peer_monitoring.py](examples/peer_monitoring.py)
- [examples/self_plugin_monitoring.py](examples/self_plugin_monitoring.py)
- [examples/remote_alarm_import.py](examples/remote_alarm_import.py)

`demo/` is for the first working run. `examples/` is closer to real deployments and includes helper classes, remote monitoring, PostgreSQL, and webhook outputs.

## Web Viewer

Kanary includes a built-in Web viewer.
The operational surface, however, is the HTTP API. The viewer is the standard UI built on top of that API, and you can replace it with your own tooling if needed.

## Documentation

- [docs/getting_started.md](docs/getting_started.md)
  Hands-on introduction.
- [docs/plugins.md](docs/plugins.md)
  Plugin interfaces and built-in helper classes.
- [docs/operations.md](docs/operations.md)
  Running Kanary, the viewer, the CLI, and persistence.
- [docs/api.md](docs/api.md)
  HTTP API and `kanaryctl`.
- [docs/development.md](docs/development.md)
  Development, linting, and tests.
- [docs/deployment.md](docs/deployment.md)
  Deployment layout and `systemd`.

Japanese versions are available as `_ja` documents, for example [README_ja.md](README_ja.md).
