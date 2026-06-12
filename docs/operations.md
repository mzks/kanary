# Operations

## Starting Kanary

Basic run:

```bash
kanary ./plugins
```

Read multiple directories:

```bash
kanary ./plugins ./local-plugins
```

Use the standard API and viewer port:

```bash
kanary ./plugins --api-port 8000
```

Expose the API and viewer on the LAN:

```bash
kanary ./plugins --api-host 0.0.0.0 --api-port 8000
```

Change the log level:

```bash
kanary ./plugins --log-level DEBUG
```

Exclude plugins:

```bash
kanary ./plugins --exclude 'sqlite.*.stale' --exclude 'discord'
```

Main arguments:

- `rule_directories...`
  Plugin directories to load.
- `--api-port`
- `--api-host`
- `--log-level`
- `--state-db`
- `--node-id`
- `--exclude`
- `--disable-default-viewer`

Environment variables:

- `KANARY_SQLITE_PATH`
- `KANARY_API_URL`
- `KANARY_API_HOST`
- `KANARY_NODE_ID`

Kanary itself does not require any environment variables. Source-specific connection settings belong to each source implementation.

## Runtime Behavior

- Kanary loads one or more plugin directories at startup.
- `@kanary.source`, `@kanary.rule`, and `@kanary.output` are the registration points.
- A source that fails during `init()` is marked `FAILED`, but the engine, API, and viewer still start.
- Each source is polled in its own thread according to `interval`.
- Rules are evaluated against the latest result from their source.
- Plugin directories are watched continuously and Python file changes are detected automatically.
- Non-Python files such as local TOML config files are not watched; after changing them, run an explicit `kanaryctl reload ...`.
- With the default `--auto-reload off`, discovered changes are applied explicitly with `kanaryctl reload ...`.

## Web Viewer

The viewer is available at:

```text
http://<host>:8000/viewer
```

The built-in viewer provides:

- dashboard
- alerts
- sources
- rules
- outputs
- silences
- admin page
- read-only plugin source display

Every write operation available in the viewer is also available through `kanaryctl`.
The viewer is the standard UI built on top of the HTTP API.
If you only want the API and CLI, `--disable-default-viewer` makes `/viewer` return `404`.

## CLI

`kanaryctl` is the thin client for the API.

```bash
kanaryctl health
kanaryctl alerts
kanaryctl alerts --json
kanaryctl history sqlite.value1.stale
kanaryctl test-poll sqlite
kanaryctl test-evaluate sqlite.value1.range --payload-file payload.json
kanaryctl test-fire sqlite.value1.range --state FIRING --reason "output check"
kanaryctl reload --dirty
kanaryctl plugins
kanaryctl silences
kanaryctl ack sqlite.value1.stale --operator operator_name --reason "investigating"
kanaryctl unack sqlite.value1.stale --operator operator_name --reason "re-open"
kanaryctl silence-for --operator operator_name --minutes 10 --rule 'sqlite.*'
kanaryctl silence-until --operator operator_name --start-at 2026-03-19T10:00:00+09:00 --end-at 2026-03-19T12:00:00+09:00 --tag sqlite
kanaryctl unsilence <silence_id> --operator operator_name
kanaryctl reload --all
```

## Log history Persistence

Enable SQLite history with `--state-db` or `KANARY_SQLITE_PATH`.

```bash
kanary ./plugins --state-db ./var/kanary.db
```

Stored data:

- alert events, including state changes and severity transitions
- output dispatch summaries
- operator actions
- silences

The history API and the viewer's History panel only retain data when SQLite persistence is enabled.

Kanary stamps newly created SQLite state DBs with a schema version.
This versioning is intentionally minimal: if you point 0.3.x at an older legacy DB without a recognized schema version, Kanary rejects it and asks you to start with a fresh state DB path.

## Demo And Examples

- [demo/basic_monitoring.py](../demo/basic_monitoring.py)
- [examples/getting_started.py](../examples/getting_started.py)
- [examples/sqlite_monitoring.py](../examples/sqlite_monitoring.py)
- [examples/sqlite_console_output.py](../examples/sqlite_console_output.py)
- [examples/discord_webhook_output.py](../examples/discord_webhook_output.py)
- [examples/postgres_wide_format.py](../examples/postgres_wide_format.py)
- [examples/postgres_long_format.py](../examples/postgres_long_format.py)
- [examples/peer_monitoring.py](../examples/peer_monitoring.py)
- [examples/remote_alarm_import.py](../examples/remote_alarm_import.py)
