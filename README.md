# Kanary

<img width="1024" height="1024" alt="Image" src="https://github.com/user-attachments/assets/b01161f4-f4f6-443f-8fa7-6682e06612c3" />

Kanary is a Python-based alerting, notification, and reliability monitoring runtime inspired by the historical “canary in a coal mine”
You define three kinds of plugins in Python:

- `Source`
  Reads values from a system, database, API, or device.
- `Rule`
  Evaluates those values and decides whether an alert should fire.
- `Output`
  Sends alert events to humans or other systems.

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
    def poll(self):
        return kanary.inputs([
            ("temperature", 23.4, datetime.now(timezone.utc)),
        ])


@kanary.rule(
    rule_id="demo.temperature.high",
    inputs="demo:temperature",
    severity=kanary.WARN,
    tags=["demo"],
)
class DemoTemperatureHigh:
    threshold = 25.0

    def evaluate(self, ctx):
        temperature = ctx.value()
        if temperature is None:
            return kanary.ok("temperature is missing")
        return kanary.error_if(
            temperature > self.threshold,
            f"temperature={temperature} is higher than {self.threshold}",
        ) or kanary.ok(
            f"temperature={temperature} is within limit",
        )


@kanary.output(output_id="console")
class ConsoleOutput:
    def emit(self, event):
        print(
            event.rule_id,
            event.current_state.value,
            event.current_severity.name,
            event.transition.value if event.transition else "-",
            event.message,
        )
```

In this example, you only implement the minimum interface:

- a source that returns values
- a rule that evaluates them
- an output that reacts to alert events

Internally, Kanary handles plugin loading, periodic source polling, rule evaluation, alert state tracking, the HTTP API, and the Web viewer.

If you want shorter definitions later, you can switch to built-in helper classes such as `RangeRule`, `StaleRule`, and `ThresholdRule`.
Users can create plugin class factory too.

For sources, the usual public API is `kanary.inputs(...)`, `kanary.no_data(...)`, `kanary.no_update(...)`, and `kanary.skip(...)`.
For rules, the usual public API is `kanary.ok(...)`, `kanary.firing(...)`, `kanary.warn(...)`, `kanary.error(...)`, and `kanary.critical(...)`.
`kanary.SourceResult(...)` and `kanary.Evaluation(...)` remain available as advanced forms.

## Running Kanary

Basic run:

```bash
kanary ./demo
```

Check the installed version:

```bash
kanary --version
kanaryctl --version
```

`run` is optional. These are equivalent:

```bash
kanary ./demo
kanary run ./demo
python -m kanary ./demo
python -m kanary run ./demo
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

Quick diagnostic commands:

```bash
kanaryctl test-poll demo
kanaryctl test-evaluate demo.temperature.high --print-template
kanaryctl test-evaluate demo.temperature.high --payload-json '{"inputs":{"demo:temperature":{"value":30.0,"timestamp":"2026-05-29T00:00:00+00:00"}},"status":"ok"}'
kanaryctl test-fire demo.temperature.high --state FIRING --reason "delivery test"
```

Rule code should normally use input-based access such as `ctx.value()` for a single input or `ctx.inputs()` for multiple inputs.
`test-evaluate` is the exception: it accepts an `inputs` map keyed by fully-qualified input names.
Use `kanaryctl test-evaluate <rule_id> --print-template` to print a canonical payload skeleton before writing test data.

Targeted plugin reload:

```bash
kanaryctl reload --rule 'demo.*'
kanaryctl reload --source 'demo*'
kanaryctl reload --output 'mail*'
kanaryctl reload --dirty
kanaryctl reload --all
```

`dirty` is a practical hint, not a complete dependency tracker. Kanary detects plugin definition changes and watched-root static imports, but it does not try to prove every same-file helper change or dynamic dependency. If you changed code intentionally, apply the relevant reload explicitly.

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

The `KANARY_*` prefix is primarily reserved for Kanary engine/runtime settings.
Example plugins in this repository usually read connection details from local files such as
`*_config.toml` next to the plugin script instead of relying on additional `KANARY_*` variables.
The recommended helper is `kanary.load_toml(...)`, which resolves relative paths against the caller script's directory.
Those local config files are not watched for auto-reload, so after editing them you should run an explicit
`kanaryctl reload ...`.

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
- [examples/postgres_wide_format.py](examples/postgres_wide_format.py)
- [examples/postgres_long_format.py](examples/postgres_long_format.py)
- [examples/peer_monitoring.py](examples/peer_monitoring.py)
- [examples/self_plugin_monitoring.py](examples/self_plugin_monitoring.py)
- [examples/remote_alarm_import.py](examples/remote_alarm_import.py)

`demo/` is for the first working run. `examples/` is closer to real deployments and includes helper classes, remote monitoring, PostgreSQL wide/long table patterns, and webhook outputs.

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
