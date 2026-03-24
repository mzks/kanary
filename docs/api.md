# API

## HTTP API

Default bind address:

```text
0.0.0.0:8000
```

Use `--api-host` and `--api-port` to change the bind address.

### Read Endpoints

- `GET /health`
  Returns a small runtime health summary, including loaded sources and rules.
- `GET /peer-status`
  Returns a compact status payload intended for peer monitoring.
- `GET /alerts`
  Returns the current alert list for the local node.
- `GET /export-alerts`
  Returns alerts in a stable format intended for remote alert import.
- `GET /history/{rule_id}`
  Returns alert events and operator actions for one rule.
- `GET /silences`
  Returns active, scheduled, and cancelled silences.
  The raw API does not add a separate `EXPIRED` state. The Web viewer and `kanaryctl` may derive `EXPIRED` locally for silences whose window has already ended.
- `GET /plugins`
  Returns current status for sources, rules, and outputs.
- `GET /viewer`
  Serves the built-in Web viewer.
- `GET /plugins/{type}/{plugin_id}/source`
  Returns read-only source code for one loaded plugin.

### Write Endpoints

- `POST /alerts/{rule_id}/ack`
  Acknowledges one alert.
- `POST /alerts/{rule_id}/unack`
  Removes acknowledgement from one alert.
- `POST /silences/duration`
  Creates a silence for a relative duration such as 10 minutes.
- `POST /silences/window`
  Creates a silence for an explicit time window.
- `POST /silences/{silence_id}/cancel`
  Cancels an existing silence.
- `POST /reload`
  Triggers a manual reload of the watched rule directories.

## Design Notes

- The Web viewer and `kanaryctl` use the same API.
- History is only persisted when SQLite storage is enabled.
- `GET /plugins/{type}/{plugin_id}/source` returns source code only for loaded plugins.
- Raw file paths are not accepted.
- `GET /export-alerts` is the stable endpoint for remote alert import.
- `GET /export-alerts` includes `origin_node_id`, `origin_rule_id`, and `mirror_path`.

## kanaryctl

`kanaryctl` is a thin client for the HTTP API.

Main subcommands:

- `health`
  Shows the runtime health summary.
- `alerts`
  Shows current alerts.
  `--filter` supports text and glob matching.
- `history`
  Shows stored history for one rule.
- `plugins`
  Shows source, rule, and output plugin status.
  `--filter` supports text and glob matching.
- `silences`
  Shows configured silences.
  `--filter` supports text and glob matching.
- `ack`
  Acknowledges one alert.
- `unack`
  Removes acknowledgement from one alert.
- `silence-for`
  Creates a silence for a duration.
- `silence-until`
  Creates a silence for an explicit start and end time.
- `unsilence`
  Cancels one silence.
- `reload`
  Triggers a manual reload.

Common argument:

- `--base-url`
  Selects the Kanary API base URL.

Examples:

```bash
kanaryctl alerts
kanaryctl ack sqlite.value1.stale --operator operator_name --reason "investigating"
kanaryctl unack sqlite.value1.stale --operator operator_name --reason "re-open"
kanaryctl silence-for --operator operator_name --minutes 10 --rule 'sqlite.*'
kanaryctl reload
```
