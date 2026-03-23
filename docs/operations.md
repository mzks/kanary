# Operations

## Starting Kanary

Basic run:

```bash
kanary ./rules
```

Read multiple directories:

```bash
kanary ./rules ./local-rules
```

Use the standard API and viewer port:

```bash
kanary ./rules --api-port 8000
```

Expose the API and viewer on the LAN:

```bash
kanary ./rules --api-host 0.0.0.0 --api-port 8000
```

Change the log level:

```bash
kanary ./rules --log-level DEBUG
```

Exclude plugins:

```bash
kanary ./rules --exclude 'sqlite.*.stale' --exclude 'discord'
```

Main arguments:

- `rule_directories...`
- `--api-port`
- `--api-host`
- `--log-level`
- `--state-db`
- `--node-id`
- `--exclude`

Environment variables:

- `KANARY_SQLITE_PATH`
- `KANARY_API_URL`
- `KANARY_API_HOST`
- `KANARY_NODE_ID`

Kanary itself does not require any environment variables. Source-specific connection settings belong to each source implementation.

## Runtime Behavior

- Kanary loads one or more rule directories at startup.
- `@kanary.source`, `@kanary.rule`, and `@kanary.output` are the registration points.
- Each source is polled in its own thread according to `interval`.
- Rules are evaluated against the latest result from their source.
- Rule directories are watched continuously and reloaded automatically.

## Web Viewer

The viewer is available at:

```text
http://<host>:8000/viewer
```

The built-in viewer provides:

- dashboard
- alerts
- plugins
- outputs
- silences
- admin page
- read-only plugin source display

Every write operation available in the viewer is also available through `kanaryctl`.
The viewer is the standard UI built on top of the HTTP API.

## CLI

`kanaryctl` is the thin client for the API.

```bash
kanaryctl health
kanaryctl alerts
kanaryctl alerts --json
kanaryctl history sqlite.value1.stale
kanaryctl plugins
kanaryctl silences
kanaryctl ack sqlite.value1.stale --operator operator_name --reason "investigating"
kanaryctl unack sqlite.value1.stale --operator operator_name --reason "re-open"
kanaryctl silence-for --operator operator_name --minutes 10 --rule 'sqlite.*'
kanaryctl silence-until --operator operator_name --start-at 2026-03-19T10:00:00+09:00 --end-at 2026-03-19T12:00:00+09:00 --tag sqlite
kanaryctl unsilence <silence_id> --operator operator_name
kanaryctl reload
```

## Persistence

Enable SQLite history with `--state-db` or `KANARY_SQLITE_PATH`.

```bash
kanary ./rules --state-db ./var/kanary.db
```

Stored data:

- alert state changes
- operator actions
- silences

The history API and the viewer's History panel only retain data when SQLite persistence is enabled.

## Demo And Examples

- [demo/basic_monitoring.py](../demo/basic_monitoring.py)
- [examples/getting_started.py](../examples/getting_started.py)
- [examples/sqlite_monitoring.py](../examples/sqlite_monitoring.py)
- [examples/sqlite_console_output.py](../examples/sqlite_console_output.py)
- [examples/discord_webhook_output.py](../examples/discord_webhook_output.py)
- [examples/latest_postgres.py](../examples/latest_postgres.py)
- [examples/peer_monitoring.py](../examples/peer_monitoring.py)
- [examples/remote_alarm_import.py](../examples/remote_alarm_import.py)
