# API

## HTTP API

Default bind address:

```text
0.0.0.0:8000
```

Use `--api-host` and `--api-port` to change the bind address.

[openapi.yaml](openapi.yaml) describes the same endpoints as an OpenAPI 3.1 definition, for tools and generated clients. `tests/test_openapi.py` checks that it lists exactly the routes KANARY serves.

### Read Endpoints

- `GET /health`
  Returns a small runtime health summary, including loaded sources, rules, and `output_emit_enabled`.
  `time`, `timezone`, and `utc_offset` give the host's clock and time zone: a time sent without a time zone (a silence window, for example) is read in it.
- `GET /meta`
  Returns the installed version and project links, the state database (enabled, schema version, the last failed write and how many failed), `output_emit_enabled`, and `plugin_load_error`: why the plugin files did not load at the last scan.
- `GET /peer-status`
  Returns a compact status payload intended for peer monitoring.
- `GET /alerts`
  Returns the current alert list for the local node.
  After a restart, each rule's alert is restored from the state database as KANARY stopped with it, with `"restored": true` and the message and values of its last evaluation before the stop, until the rule is evaluated again; a silence that began or ended while KANARY was stopped applies at the start. A restored alert can be acknowledged and silenced. A rule that has no alert yet (never evaluated, or no `--state-db`) is not in this list; the viewer and `kanaryctl alerts` show it as `WAITING`.
- `GET /export-alerts`
  Returns alerts in a stable format intended for remote alert import.
- `GET /history/{rule_id}`
  Returns alert events, output dispatch summaries, and operator actions for one rule, newest first (by when they happened, not by when they were written: a dispatch is written once every output is done). `?limit=N` returns the newest N of each (the viewer asks for 200, `kanaryctl alerts RULE` for 5); without it, all of them.
  Output dispatches include `emit_skipped_outputs` for matched outputs skipped by `--no-output-emit`.
- `GET /alert-history`
  Returns alert events and operator actions across all rules as one newest-first timeline.
  The first request returns up to `limit` recent rows from each history stream. Subsequent requests can pass `after_alert_id` and `after_action_id` to fetch only rows added after those IDs; `limit` is bounded to 500 per stream.
  Filtering is performed by the Web viewer over its loaded history. The Web viewer folds ACK and UNACK actions into their corresponding alert events.
  Initial evaluations that are already OK (`INITIAL -> OK`) are omitted; initially non-OK events are retained.
- `GET /silences`
  Returns active, scheduled, and cancelled silences.
  The raw API does not add a separate `EXPIRED` state. The Web viewer and `kanaryctl` may derive `EXPIRED` locally for silences whose window has already ended.
- `GET /plugins`
  Returns current status for sources, rules, and outputs.
  Plugin rows may be `DISCOVERED`, `DIRTY`, `PENDING_REMOVE`, `READY`, `RELOADING`, or `FAILED`.
  `warning` is set while a rule's `evaluate()` is slow. Output rows add `queued` (events waiting or being sent) and `dropped` (events dropped from a full queue or at shutdown).
- `GET /viewer`
  Serves the built-in Web viewer.
- `GET /plugins/{type}/{plugin_id}/source`
  Returns read-only source code for one loaded or discovered plugin.

In JSON, a severity is its name (`"ERROR"`); the requests below accept a name in any case, or `kanary.ERROR`'s number.

### Write Endpoints

A request body is a JSON object; anything else (not JSON, a list, a string) returns 400. The answers of ack, unack, and the silence endpoints have `warnings`: a silence target that matches nothing, or a peer that did not get an action forwarded to it (the local action holds all the same). An operator action that names an unknown rule or silence returns 404; one that does not apply to the alert as it is (acknowledging an alert that is not `FIRING`, unacknowledging one that is not acknowledged, cancelling a cancelled silence) returns 409. During `reload --full`, while one engine hands over to the next, ack, unack, and the silence endpoints return 503: try again a moment later. Each error reply has an `error` text.

- `POST /alerts/{rule_id}/ack`
  Acknowledges one `FIRING` alert (also one restored as `FIRING` after a restart).
- `POST /alerts/{rule_id}/unack`
  Removes acknowledgement from one alert.
- `POST /silences/duration`
  Creates a silence for a relative duration such as 10 minutes.
- `POST /silences/window`
  Creates a silence for an explicit time window.
  `start_at` and `end_at` are ISO 8601 times; without a time zone they are the host's local time. Both silence endpoints return the window as it was read (`start_at`, `end_at`, with the offset).
  `rule_patterns` and `tags` are lists of names; one name may be a plain string (`"rule_patterns": "sqlite.*"` is one pattern).
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
  - `{"full":true}`
  `{"all":true}` always reruns full plugin discovery even when no watched Python file changed.
  `{"full":true}` performs an in-process engine restart after successful discovery and validation.
  For legacy compatibility, an empty body is still accepted and behaves like `{"all":true}`.
  A reloaded rule is evaluated at once with the last values of its sources, so a changed threshold applies without waiting for the next value. A rule whose sources have given no value since the start waits for one. While a reload changes sources, outputs, and rules, evaluations wait (reads and operator actions do not): a value read meanwhile, also the first one of a new source, is judged once all of them are in place, so its alert reaches an output added by the same reload.
  The answer lists, per `sources` / `rules` / `outputs`: `matched`, `reloaded`, `removed`, `failed` (not applied: its class or `__init__()` failed, and the running instance goes on), `init_failed` (applied, but its first `init()` failed; it keeps trying), and `errors` (the reason for each). `status` is `reloaded` when everything was applied, `partial` when some of it was, and `reload_failed` when nothing was (also when the plugin files do not load or validate). Any status but `reloaded` comes with HTTP 500 and an `error` text that names each plugin.
- `POST /test-poll/{source_id}`
  Runs the source's `test_poll()` on the source's own thread, between its polls, and returns what it gave, with fully-qualified input names: `{"source_id": "postgres", "status": "ok", "metadata": {...}, "inputs": {"postgres:temperature": {"value": 21.5, "timestamp": "...", "metadata": {}}}}` (`reason` and `metadata` only when the poll gave them). This is a payload for `POST /test-evaluate/{rule_id}` as it is. The result is not evaluated, the source's state and status do not change, and its regular polls keep their schedule. Returns 409 when the source is not running (its `init()` failed) or the test poll does not finish within 30 seconds.
- `POST /test-evaluate/{rule_id}`
  Dry-runs one rule against an explicit payload and returns the normalized evaluation result.
  The payload is one of:
  - an `inputs` object keyed by fully-qualified input names such as `postgres:temperature`, with `status`, `reason`, and `metadata` beside it if wanted. This is what `POST /test-poll/{source_id}` returns; for a rule that reads several sources, put the `inputs` of their test polls together. An input may also give `prev_value`, `prev_timestamp`, and `prev_metadata`. A `source_id` beside `inputs` is the source the payload is from: a name without `source_id:` is its input, and with no inputs the payload is an empty snapshot of that source. Without it, a rule that reads one source also takes names without `source_id:`, as in an alert's payload. The `channels` of KANARY before 0.8.0 are read like `inputs`.
  - for a rule with one input, `{"value": ..., "timestamp": ..., "prev_value": ...}`.

  Normal rule implementations should still prefer `ctx.value()`, `ctx.inputs()`, and related accessors.

  The rule is judged by a copy of the live rule, with copies of its previous alert (`ctx.previous_alert`) and of the source snapshots, so what its `evaluate()` changes (a counter or a last value on `self`, the previous alert, an input) does not reach the live state; a rule that cannot be copied (it holds a lock or a connection) is judged by a new instance. What it adds to a `kanary.Buffer` is dropped, as for a test poll.
- `GET /test-evaluate-template/{rule_id}`
  Returns a canonical `inputs` payload template for one rule. Single-input rules may also include a shorthand template.
- `POST /test-fire/{rule_id}`
  Sends a synthetic state change through the output pipeline without changing the live alert state.
  The body gives either `"state"` or `"evaluation"`: the result of `POST /test-evaluate/{rule_id}`, whose state, severity, message, and values the event then has. The message starts with `[TEST-FIRE]` either way.
  With `--no-output-emit`, it reports matching outputs in `emit_skipped_outputs` without calling them.
  The event has `event.synthetic = True`. `OutputFollowups` and `QuietHours` keep a test fire's followups and held events apart from the rule's real ones: a test `OK` does not cancel the followups of the live alert.

## Design Notes

- There is no login and no roles: KANARY runs on a closed network, where everyone of the experiment may act on any alert (see "Network And Access" in [deployment.md](deployment.md)). For a wider network, put a reverse proxy with TLS and authentication in front.
- The Web viewer and `kanaryctl` use the same API.
- History is only persisted when SQLite storage is enabled.
- `GET /plugins/{type}/{plugin_id}/source` returns source code for loaded and discovered plugins.
- `dirty` marks a plugin whose code changed: its class (with the classes it extends from plugin files), the rest of its file outside the plugin classes (a constant at the top changes every plugin of the file), and the modules of the plugin directories it imports, also through other helpers and relative imports. An import KANARY cannot see in the code (`importlib`, a name built at run time) and non-Python files are not tracked: apply those with `reload --rule ...` or `--all`.
- Raw file paths are not accepted.
- `GET /export-alerts` is the stable endpoint for remote alert import.
- `GET /export-alerts` includes `origin_node_id`, `origin_rule_id`, and `mirror_path`.

## kanaryctl

`kanaryctl` is a thin client for the HTTP API.

In the main `kanary` CLI, `run` is optional. For example, `kanary ./plugins` means the same thing as `kanary run ./plugins`. `lint` must still be written explicitly.

Main subcommands:

- `health`
  Shows the runtime health summary.
- `alerts [RULE_ID]`
  Shows current alerts, alerting first; every rule is listed, one without an alert yet as `WAITING`, and a state restored from before a restart is marked `*`.
  On a terminal the message is cut to fit the width (`…`); `--wide` adds the outputs and file columns and cuts nothing, and piped output is not cut.
  With `RULE_ID` (or a glob), shows that alert in full: message, values, ack, silences, tags, outputs, runbook, file, and its newest history.
  `--filter` supports text and glob matching.
- `history`
  Shows stored history for one rule.
  `--since` and `--limit` are applied client-side after fetching the history payload.
  When SQLite persistence is enabled, history includes output dispatch summaries.
- `plugins [PLUGIN_ID]`
  Shows source, then rule, then output plugin status; the time of the last update is local.
  On a terminal the last error is cut to fit the width; `--wide` adds the file column and cuts nothing.
  With `PLUGIN_ID` (or a glob), shows that plugin in full: runs, the whole last error, and for a rule its inputs and outputs, for an output its routing and queue. `--traceback` adds where the last error happened.
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
  `--start-at 2026-11-21T22:00` is the local time of the KANARY host (not of the machine running kanaryctl); add an offset (`2026-11-21T22:00+09:00`) to be explicit. The window as KANARY read it is printed on stderr; stdout is the silence id.
- `unsilence`
  Cancels one silence.
- `reload`
  Applies discovered plugin changes.
  Use exactly one of `--rule`, `--source`, `--output`, `--dirty`, `--all`, or `--full`.
  `--all` reruns full discovery even without local Python file changes.
  `--full` performs an in-process engine restart and reinitializes all sources, rules, and outputs.
  For legacy compatibility, `POST /reload` with an empty body still behaves like `--all`.
  Prints the status (`reloaded`, `partial`, or `reload_failed`) and a line for each plugin that was not applied or whose `init()` failed; exits with 1 unless the status is `reloaded`.
- `test-poll`
  Runs the source's `test_poll()` (on its own thread, between polls; not evaluated) and prints what it gave as JSON, with `source_id:input` names.
- `test-evaluate`
  Dry-runs one rule against a payload from `--payload-json`, `--payload-file`, or `--payload-stdin`; the output of `test-poll` works as a payload.
  Use `--print-template` first if you want a canonical inputs-based payload skeleton.
- `test-fire`
  Sends a synthetic alert event through the output pipeline and prints the dispatch summary as JSON. It goes ahead of queued events and waits up to 30 seconds; outputs that have not sent by then are listed in `pending_outputs`.
  Give `--state`, or `--evaluation-stdin` / `--evaluation-file` with the output of `test-evaluate`: the notification then shows the state, severity, message, and values the rule judged.

Common argument:

- `--base-url`
  Selects the Kanary API base URL.

Examples:

```bash
kanaryctl alerts
kanaryctl test-poll sqlite
kanaryctl test-evaluate sqlite.value1.range --print-template
kanaryctl test-evaluate sqlite.value1.range --payload-json '{"inputs":{"sqlite:value1":{"value":120,"timestamp":"2026-05-29T00:00:00+00:00"}},"status":"ok"}'
kanaryctl test-fire sqlite.value1.range --state FIRING --reason "output check"
kanaryctl test-poll sqlite | kanaryctl test-evaluate sqlite.value1.range --payload-stdin \
  | kanaryctl test-fire sqlite.value1.range --evaluation-stdin
kanaryctl alerts sqlite.value1.range
kanaryctl plugins sqlite --traceback
kanaryctl ack sqlite.value1.stale --operator operator_name --reason "investigating"
kanaryctl unack sqlite.value1.stale --operator operator_name --reason "re-open"
kanaryctl silence-for --operator operator_name --minutes 10 --rule 'sqlite.*'
kanaryctl silence-until --operator operator_name --start-at 2026-11-21T22:00 --end-at 2026-11-22T06:00 --rule 'daq.*'
kanaryctl reload --rule 'sqlite.*'
kanaryctl reload --dirty
```

Additional HTTP endpoint:

- `GET /test-evaluate-template/{rule_id}`
  Returns a canonical `inputs` payload template plus, when possible, a single-input shorthand template.
