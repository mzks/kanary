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
  Returns alert events, output dispatch summaries, and operator actions for one rule.
- `GET /silences`
  Returns active, scheduled, and cancelled silences.
  The raw API does not add a separate `EXPIRED` state. The Web viewer and `kanaryctl` may derive `EXPIRED` locally for silences whose window has already ended.
- `GET /plugins`
  Returns current status for sources, rules, and outputs.
  Plugin rows may be `DISCOVERED`, `DIRTY`, `PENDING_REMOVE`, `READY`, `RELOADING`, or `FAILED`.
- `GET /viewer`
  Serves the built-in Web viewer.
- `GET /plugins/{type}/{plugin_id}/source`
  Returns read-only source code for one loaded or discovered plugin.

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
  Applies discovered plugin changes.
  The JSON body must contain exactly one target:
  - `{"rule":"postgres.*"}`
  - `{"source":"postgres*"}`
  - `{"output":"discord*"}`
  - `{"dirty":true}`
  - `{"all":true}`
  For legacy compatibility, an empty body is still accepted and behaves like `{"all":true}`.
- `POST /test-poll/{source_id}`
  Polls one source and returns the normalized source payload.
- `POST /test-evaluate/{rule_id}`
  Dry-runs one rule against an explicit payload and returns the normalized evaluation result.
  This payload uses an `inputs` object keyed by fully-qualified input names such as `postgres:temperature`. Normal rule implementations should still prefer `ctx.value()`, `ctx.inputs()`, and related accessors.
- `POST /test-fire/{rule_id}`
  Sends a synthetic state change through the output pipeline without changing the live alert state.

## Design Notes

- The Web viewer and `kanaryctl` use the same API.
- History is only persisted when SQLite storage is enabled.
- `GET /plugins/{type}/{plugin_id}/source` returns source code for loaded and discovered plugins.
- `dirty` is a practical reload hint, not a complete dependency proof. Kanary tracks plugin definition changes and watched-root static imports, but it does not guarantee detection of every same-file helper change or dynamic dependency.
- Raw file paths are not accepted.
- `GET /export-alerts` is the stable endpoint for remote alert import.
- `GET /export-alerts` includes `origin_node_id`, `origin_rule_id`, and `mirror_path`.

## kanaryctl

`kanaryctl` is a thin client for the HTTP API.

In the main `kanary` CLI, `run` is optional. For example, `kanary ./plugins` means the same thing as `kanary run ./plugins`. `lint` must still be written explicitly.

Main subcommands:

- `health`
  Shows the runtime health summary.
- `alerts`
  Shows current alerts.
  `--filter` supports text and glob matching.
- `history`
  Shows stored history for one rule.
  `--since` and `--limit` are applied client-side after fetching the history payload.
  When SQLite persistence is enabled, history includes output dispatch summaries.
- `plugins`
  Shows source, rule, and output plugin status.
  `--filter` supports text and glob matching.
- `silences`
  Shows configured silences.
  `--filter` supports text and glob matching.
  `--since` and `--limit` are applied client-side after fetching the silence list.
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
  Applies discovered plugin changes.
  Use exactly one of `--rule`, `--source`, `--output`, `--dirty`, or `--all`.
  For legacy compatibility, `POST /reload` with an empty body still behaves like `--all`.
- `test-poll`
  Polls one source and prints the normalized payload as JSON.
- `test-evaluate`
  Dry-runs one rule against a payload from `--payload-json`, `--payload-file`, or `--payload-stdin`.
- `test-fire`
  Sends a synthetic alert event through the output pipeline and prints the dispatch summary as JSON.

Common argument:

- `--base-url`
  Selects the Kanary API base URL.

Examples:

```bash
kanaryctl alerts
kanaryctl test-poll sqlite
kanaryctl test-evaluate sqlite.value1.range --payload-json '{"inputs":{"sqlite:value1":{"value":120,"timestamp":"2026-05-29T00:00:00+00:00"}},"status":"ok"}'
kanaryctl test-fire sqlite.value1.range --state FIRING --reason "output check"
kanaryctl ack sqlite.value1.stale --operator operator_name --reason "investigating"
kanaryctl unack sqlite.value1.stale --operator operator_name --reason "re-open"
kanaryctl silence-for --operator operator_name --minutes 10 --rule 'sqlite.*'
kanaryctl reload --rule 'sqlite.*'
kanaryctl reload --dirty
```
