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

Run a shadow instance against production inputs without executing Output delivery:

```bash
kanary ./plugins \
  --api-port 8001 \
  --node-id production-shadow \
  --no-output-emit
```

`--no-output-emit` still loads and initializes Output plugins and evaluates their routing filters, but it never calls `emit()`. Dispatch summaries report these routes in `emit_skipped_outputs`; `delivered_outputs` and each Output's Emit Count remain unchanged. Source polling, Rule evaluation, state changes, operator actions, and remote action propagation continue normally. Use a separate `--state-db` path, or no state DB, when running beside production.

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
- `--viewer-title`
- `--no-output-emit`

Environment variables:

- `KANARY_SQLITE_PATH`
- `KANARY_API_URL`
- `KANARY_API_HOST`
- `KANARY_NODE_ID`
- `KANARY_VIEWER_TITLE`

Kanary itself does not require any environment variables. Source-specific connection settings belong to each source implementation.

## Runtime Behavior

- Kanary loads one or more plugin directories at startup.
- `@kanary.source`, `@kanary.rule`, and `@kanary.output` are the registration points.
- A source or output whose `init()` fails is marked `FAILED`, but the engine, API, and viewer still start, and the plugin keeps trying its `init()` on its own thread (see "Failure recovery" in [plugins.md](plugins.md)). After a power cut KANARY may start before its database or mail server; it picks them up when they come back.
- Each source is polled in its own thread according to `interval`.
- Rules are evaluated against the latest result from their source.
- Plugin directories are watched continuously and Python file changes are detected automatically.
- Plugin directories are on Python's import path (after Python's own modules and installed packages), so a plugin can import a helper module next to it (`from shared import LIMIT`). Such a helper is read from its file again on every scan of the plugin files, like the plugin files; `reload --dirty` reloads the plugins that import it, also through other helpers. A directory with an `__init__.py` is a package: its files are modules of it (`pkg/limits.py` is `pkg.limits`, so `from .values import LIMIT` works), each run once per scan, and they may define plugins too. Import helpers at the top of the plugin file: a running plugin keeps the helper it was loaded with until it is reloaded, while an `import` inside `evaluate()` or `poll()` gets the helper of the last scan, also when the change is not applied yet (with `--auto-reload off`).
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
kanaryctl reload --all
kanaryctl reload --full
kanaryctl plugins
kanaryctl silences
kanaryctl ack sqlite.value1.stale --operator operator_name --reason "investigating"
kanaryctl unack sqlite.value1.stale --operator operator_name --reason "re-open"
kanaryctl silence-for --operator operator_name --minutes 10 --rule 'sqlite.*'
kanaryctl silence-until --operator operator_name --start-at 2026-03-19T10:00:00+09:00 --end-at 2026-03-19T12:00:00+09:00 --tag sqlite
kanaryctl unsilence <silence_id> --operator operator_name
```

- `reload --dirty`
  Apply only `DISCOVERED`, `DIRTY`, and `PENDING_REMOVE` plugins.
- `reload --all`
  Rerun full plugin discovery even when no watched Python file changed, then apply the result.
- `reload --full`
  Perform an in-process engine restart. All sources, rules, and outputs are reinitialized. A plugin whose `init()` fails does not stop the restart (it keeps trying); a plugin that cannot be created at all does, and the running engine goes on.
  The new engine starts from what the running one wrote to the state database. During the switch, acknowledgements and silences are refused with HTTP 503 (`kanaryctl` prints the error; try again a moment later), so none is lost between the two engines. When the state database does not take the running engine's last writes within 10 seconds (locked by another process, a full disk), the restart is given up (`reload_failed`) and the running engine goes on as before.
  Sources go on polling during the switch (what they read waits); their threads stop only once the new engine runs, so a restart given up leaves them as they were. Every event of the running engine is sent (or fails) before the switch, so a receiver never gets a `FIRING` after the `OK` that follows it: when an output is still sending after 5 seconds (a slow or unreachable destination), the restart is given up the same way, with the output named in the error. Try again once it has sent, or restart the process.
  Plugins the files no longer have are removed as `reload --all` removes them, and listed in `removed`: a removed rule's ack and judgment are not kept, so a rule added again later with the same id starts afresh.

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
- what each output was sent and not yet the `OK` of, with the severity it was sent last (so the `OK` after a restart reaches the outputs it closes)
- each rule's own last judgment (`FIRING` or `OK`) with its message and values, apart from what was shown (written when the judgment changes, and at shutdown and before `reload --full`, so a restored alert has the values it was last evaluated with): after a restart a parent that was `SILENCED` still holds the rules behind it, and a silence that began or ended while KANARY was stopped applies at the start

The history API and the viewer's History panel only retain data when SQLite persistence is enabled.

Writes to the database run on a thread of their own, in order, so evaluation and notifications never wait for it. A write the database refuses for now (another process holds it locked past SQLite's busy timeout, the disk is full) is tried again, after 1 s and then up to every 30 s, until it goes in: the history comes late but complete, and a restart restores what really happened. Meanwhile it is shown as `state_db_error` in `GET /health` and `GET /meta`, and as "State DB Write Error" in the viewer. What is still waiting at shutdown gets 10 seconds; writes given up then (or beyond 100,000 waiting) are logged.

Kanary stamps newly created SQLite state DBs with a schema version.
This versioning is intentionally minimal: if you point 0.3.x at an older legacy DB without a recognized schema version, Kanary rejects it and asks you to start with a fresh state DB path.

### Removing old history

The database grows only when something happens: a row per state change, per delivery, and per operator action, not per evaluation. A hundred state changes a day add some tens of MB a year; a rule that flaps every poll adds far more, and is worth fixing in itself. To remove history older than a date, stop KANARY, back up the file, and run:

```bash
sqlite3 ./var/kanary.db <<'SQL'
-- Keep each rule's newest event: KANARY restores the alerts from it after a restart.
DELETE FROM alert_events
 WHERE occurred_at < '2024-01-01'
   AND id NOT IN (SELECT MAX(id) FROM alert_events GROUP BY rule_id);
DELETE FROM output_dispatches WHERE occurred_at < '2024-01-01';
-- Keep acknowledgements and rule removals: an alert still acknowledged keeps its ack from here.
DELETE FROM operator_actions WHERE created_at < '2024-01-01' AND action_type NOT IN ('ack', 'unack', 'rule_removed');
DELETE FROM silences WHERE end_at < '2024-01-01';
VACUUM;
SQL
```

Times are stored as ISO 8601 text, so comparing with a date such as `'2024-01-01'` works.

## Demo And Examples

- [demo/basic_monitoring.py](../demo/basic_monitoring.py)
- [examples/getting_started.py](../examples/getting_started.py)
- [examples/factory_patterns.py](../examples/factory_patterns.py)
- [examples/sqlite_monitoring.py](../examples/sqlite_monitoring.py)
- [examples/sqlite_console_output.py](../examples/sqlite_console_output.py)
- templates: `kanary new --list`, [templates.md](templates.md)
