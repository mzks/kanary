# API

## HTTP API

Default bind address:

```text
0.0.0.0:8000
```

Use `--api-host` and `--api-port` to change the bind address.

### Read Endpoints

- `GET /health`
- `GET /peer-status`
- `GET /alerts`
- `GET /export-alerts`
- `GET /history/{rule_id}`
- `GET /silences`
- `GET /plugins`
- `GET /viewer`
- `GET /plugins/{type}/{plugin_id}/source`

### Write Endpoints

- `POST /alerts/{rule_id}/ack`
- `POST /alerts/{rule_id}/unack`
- `POST /silences/duration`
- `POST /silences/window`
- `POST /silences/{silence_id}/cancel`
- `POST /reload`

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
- `alerts`
- `history`
- `plugins`
- `silences`
- `ack`
- `unack`
- `silence-for`
- `silence-until`
- `unsilence`
- `reload`

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
