# Plugin Model

This document first explains the minimum interface a user needs to implement, and then the built-in helper classes.

To start from a working file, run `kanary new --list` and `kanary new -t <template> <id>`; see [Getting Started](getting_started.md). The templates in `src/kanary/templates/` also show every option of each plugin type.

## Decorator Arguments and Class Attributes

Every attribute listed in this document can be written either as a decorator argument or as a class attribute. The decorator copies its arguments onto the class, so these two definitions behave identically:

```python
@kanary.source(source_id="postgres.wide", interval=30.0, description="Read the latest environment row.")
class WideEnvironmentSource:
    def poll(self): ...

@kanary.source(source_id="postgres.wide", interval=30.0)
class WideEnvironmentSource:
    description = "Read the latest environment row."

    def poll(self): ...
```

If the same attribute is set in both places, the decorator value wins.

Recommended style:

- Decorator: short values that identify the plugin and decide when and where it runs, such as `source_id` / `rule_id` / `output_id`, `interval` / `schedule`, `source` / `inputs`, `severity`, `tags`, `include_tags`, `exclude_tags`, `exclude_states`, and `minimum_severity`.
- Class body: longer text and behavior-specific settings, such as `description`, `runbook`, `thresholds`, helper settings like `field` or `timeout`, and methods.

`kanary lint` warns when:

- the same attribute is set in both the decorator and the class body with different values (a class-body `None` counts as a placeholder, not a conflict)
- a decorator argument is not an attribute Kanary knows and the class does not define it (for example `descripton=`), with a suggestion when a close name exists

To pass your own setting through the decorator without a warning, declare it in the class body first, for example `dsn: str` or `dsn = None`, and then set the value in the decorator.

## Where Plugin Code Runs

Sources and outputs each run on a thread of their own, so a slow one delays only itself. Rules are the exception.

- **Source:** `init()`, `poll()`, and `terminate()` of a source are only ever called from the source's own thread, never two at once, so a connection made in `init()` can be one that only works on the thread that made it (`sqlite3`). A reload stops the thread, which terminates the old instance once its `poll()` has returned (never during it), and starts a new thread for the new instance. `kanaryctl test-poll` runs the source's `test_poll()` on that thread too, between polls; its result is shown and never evaluated, and the regular polls keep their schedule.
- **Output:** `init()`, `emit()`, the callbacks of `OutputFollowups` and `QuietHours`, and `terminate()` all run on the output's own thread, one at a time, so an output needs no locks of its own. Events wait in a queue in the order they happened; a slow or unreachable destination holds only this output's queue. Different outputs run in parallel, so do not share mutable state between them without a lock.
- **Rule:** rules are evaluated one at a time, in dependency order, because a rule may depend on other rules and on several sources. Every other evaluation waits while one runs, so `evaluate()` must be fast and free of I/O; fetch data in a source. An `evaluate()` slower than 100 ms is logged and shown as `SLOW` on the Rules page. An output's routing filters (`matches()`) are checked during evaluation too.

## 1. Source

### Minimum Interface

Required:

- `source_id`
- `poll()`

Optional metadata:

- `description`

`description` is surfaced by the API and the default Web viewer.

Optional:

- `interval`
- `schedule`
- `init()`
- `terminate()`
- `test_poll()`
- `max_retry`
- `max_reinit`

`test_poll()` answers `kanaryctl test-poll`: what `poll()` would return now, without changing the source. The default calls `poll()`, which is right when `poll()` only reads. Override it only when `poll()` takes something that is then gone - values from a queue, a read position in a log - and return the same kind of result without taking it; otherwise a test poll would take data that the next real poll then misses. `LogCountSource` already does this. What `poll()` adds to a `kanary.Buffer` is dropped after a test poll by itself, so keeping history needs no override.

If you omit both `interval` and `schedule`, Kanary uses `interval = 60.0`.
If you use `schedule`, do not set `interval` at the same time. One written on the class itself (in its body or decorator) replaces the other one inherited from a base class.
The `poll(ctx)`, `init(ctx)`, and `terminate(ctx)` of KANARY before 0.8.0 are an error now: drop the `ctx` argument.

`interval` is a polling interval in seconds, or text such as `"30s"`, `"5m"`, or `"1h30m"`. `interval = 0` calls `poll()` again as soon as it returns; use it only for a `poll()` that waits for data (see "Values that arrive by themselves").
`schedule` is a Unix cron-compatible 5-field string interpreted in the local time
of the Kanary server. A small set of macros is also supported, such as
`@hourly`, `@daily`, `@weekly`, `@monthly`, and `@yearly`.
It also takes what other cron implementations write:

- months and weekdays as names, abbreviated or not, in any case: `JAN`, `september`, `mon-fri`, `Tues`
- Sunday as 0 or 7 (`5-7` is Friday to Sunday); `5/15` is `5-59/15`
- a range that wraps: `22-2` in the hours is 22:00 to 02:00, `FRI-MON` is Friday to Monday
- `?` in the day fields, the same as `*`
- `L` (the last day of the month) and `L-2` (two days before it) in the day of the month
- `5L` or `FRIL` (the last Friday of the month) and `MON#1` (the first Monday) in the weekday
- a time zone in front: `CRON_TZ=Asia/Tokyo 0 9 * * *` or `TZ=UTC @daily` (any zone `kanary.to_timezone()` reads)
- `@every 5m` (also `90s`, `1h30m`, `2d`): this is `interval = 300`, not a schedule

As in cron, when both the day of the month and the weekday are given (neither is
`*` or `?`), a day matches when either does: `0 0 1 * 1` runs on the 1st and on every
Monday, and `kanary lint` warns about it. For the first Monday of a month write
`0 9 * * MON#1`. A schedule that never runs (`0 0 31 2 *`, cron's way to turn a job
off) is accepted with a lint warning: the source runs `init()` and answers
`kanaryctl test-poll`, but is never polled, and the Sources page shows it as
`NOT POLLED`. Six or seven fields (seconds, years) are not read: implementations put
them in different places.

The next run is found however far ahead it is (`0 9 * FEB MON#5`, a fifth Monday in
February, runs next in 2044). Around daylight saving time the fields are read on the
wall clock, and a run always comes after the one before in real time, never twice for
one time: on the day the clock goes back, a time that occurs twice runs once; on the day
it goes forward, only the first of the times the clock skips runs, an hour late
(`30 2 * * *` at 03:30, also when KANARY starts at 03:10 that day; `*/15 2 * * *` once at 03:00). Where the number of runs matters,
use a zone without daylight saving time (`CRON_TZ=UTC`, or Japan's) or `interval`.

Examples:

- `interval = 60.0`
- `schedule = "*/5 * * * *"`
- `schedule = "@hourly"`

Example:

```python
@kanary.source(source_id="sqlite", interval=5.0)
class SqliteSource:
    description = "Read the latest sqlite sample rows and expose them as Kanary inputs."

    def poll(self):
        ...
```

Failure recovery defaults:

- `max_retry = 1`
- `max_reinit = 1`

If `poll()` raises, Kanary retries in-process before marking the source as failed.
Attempt `N` waits `N**2` seconds first. With the defaults, Kanary:

1. waits 1 second and retries `poll()`
2. waits 4 seconds, runs `terminate() -> init()`, and retries `poll()`

If all attempts fail, the source stays `FAILED` until the next scheduled poll or an explicit reload.

Only an exception from `poll()` is retried. A `poll()` that returns something that is not a poll result (for example `return object()`) is a bug in the source: it is not retried; the source shows `FAILED` with `poll() returned an invalid result: ...` and polls again at its next interval.

`init()` runs on the source's own thread too. When it fails (at start, a database that is still booting after a power cut), the source is `FAILED` and its thread tries `init()` again, with the same `max_reinit` attempts, every `interval` (at most a minute apart) until it works; then it polls as usual. The other sources start meanwhile, and KANARY starts without waiting for them.

### Returning inputs

`poll()` returns one of `kanary.inputs(...)`, `kanary.no_data(...)`, `kanary.no_update(...)`, and `kanary.skip(...)`.

Tuple/list style:

```python
return kanary.inputs(
    [
        ("temperature", 23.4, observed_at, {"unit": "C"}),
        ("humidity", 40.0),
    ],
    metadata={"table": "env_samples_wide"},
)
```

Mapping style is also accepted:

```python
return kanary.inputs(
    {
        "temperature": (23.4, observed_at, {"unit": "C"}),
        "humidity": 40.0,
    }
)
```

Rules:

- `(name, value)`, `(name, value, timestamp)`, and `(name, value, timestamp, metadata)` are accepted
- if the item timestamp is omitted or `None`, Kanary uses the outer `timestamp=` if present, otherwise the server's current time
- a timestamp is a `datetime`, ISO 8601 text, or unix time; see [Times and time zones](#times-and-time-zones)
- `Decimal` (what database drivers return for `NUMERIC`) and numpy numbers become plain `float` / `int`, so rules and JSON treat them as numbers
- NaN and infinity are no measurement: the input has no value (`None`; the API and history show `null`), and its `metadata["error"]` says why (`value is nan`). Rules treat it as missing: `RangeRule(missing_fires=True)` fires with `no value: x (value is nan)`
- outer `metadata=` is the snapshot's `metadata`, next to its inputs: `ctx.source_payload()["metadata"]`, and in what `kanaryctl test-poll` prints
- `kanary.no_data(reason=..., metadata=...)` means the poll succeeded and produced an empty snapshot; Kanary updates the source snapshot and still evaluates rules
- `kanary.no_update(reason=..., metadata=...)` means the poll succeeded but there is no new snapshot; Kanary keeps the last snapshot and still evaluates rules against it
- `kanary.skip(reason=..., metadata=...)` means the poll should be ignored entirely; Kanary keeps the last snapshot and does not evaluate rules
- raising an exception means source/plugin failure and triggers the runtime retry/reinit policy

To add inputs to what a helper source returns, give its result to `kanary.inputs()`:

```python
class Pumps(kanary.HTTPJSONSource):
    source_id = "pumps"
    url = "http://pump-controller.local/status.json"

    def poll(self):
        result = super().poll()
        values = {item.name: item.value for item in result.inputs}
        if values.get("power"):
            return kanary.inputs(result, ("efficiency", values["flow"] / values["power"]))
        return result
```

The result's inputs and metadata come first, then the other items; an item replaces an input of the same name. A result of `kanary.no_update()` or `kanary.skip()` is returned as it is, since there is no new snapshot to add to, and `kanary.no_data()` has no inputs. A result has `.status` (`"ok"`, `"empty"`, `"no_update"`, or `"skip"`) and `.inputs`, each with `.name`, `.value`, `.timestamp`, and `.metadata`, like the samples of a `kanary.Buffer`.

#### Times and time zones

A time without a time zone is read as **the local time of the host that runs KANARY**. This holds everywhere KANARY takes a time in: input timestamps, `kanaryctl silence-until --start-at 2026-11-21T22:00`, the HTTP API, and the state database. A unix time is always absolute.

Give times a time zone when you can, so they mean the same on any host:

```python
from datetime import datetime, timezone

datetime.now().astimezone()      # now, with this host's offset
datetime.now(timezone.utc)       # now, in UTC
kanary.to_datetime("2026-11-21 22:00", naive_timezone="Asia/Tokyo")  # text from a system on another zone
"2026-11-21T22:00:00+09:00"      # ISO 8601 text with the offset ("...Z" for UTC)
```

In SQL, prefer `timestamp with time zone` (PostgreSQL `timestamptz`). For a table that stores times without a zone in UTC (or any zone other than the host's), set `timestamp_timezone = "UTC"` on the SQL helper sources. `StaleRule` warns when a timestamp is more than 30 minutes in the future, which is how a wrong zone usually shows.

Typical guidance:

- use `kanary.inputs(...)` for a normal successful poll
- use `kanary.no_data(...)` when "empty" is the correct current result
- use `kanary.no_update(...)` when you want `StaleRule` and other rules to keep looking at the last good snapshot
- use `kanary.skip(...)` only for explicit no-op cases such as warm-up or maintenance windows, because stale detection does not advance

Plugins are also free to read local files from their own directory when they need site-specific configuration.
Kanary provides small helpers for this:

```python
import kanary

config = kanary.load_toml()
dsn = kanary.load_toml("dsn")
role_id = kanary.load_toml("mention.role_id", filename="discord_config.toml")
plugin_root = kanary.plugin_dir()
```

- `kanary.load_toml()` defaults to `config.toml`
- `kanary.load_json()` defaults to `config.json`
- relative `filename=` paths are resolved against the caller script's directory
- absolute paths are accepted as-is
- if `key` is omitted, the whole mapping is returned
- dotted keys such as `"mention.role_id"` walk nested TOML/JSON tables and objects
- missing files or missing keys raise `RuntimeError` (under `kanary lint` they give a warning and `None` instead)
- `kanary.load_json(...)` works the same way for JSON files

Using `Path(__file__).with_name(...)` directly is still fine when you want full manual control.
These local config files are not part of auto-reload detection, so after editing them you should run an explicit
`kanaryctl reload ...`.

Secrets (passwords, tokens, webhook URLs) stay out of the plugin file. The usual place is an environment variable, set in the systemd unit, an `EnvironmentFile`, or the shell that starts kanary:

```python
import os

@kanary.output(output_id="slack")
class Slack(kanary.SlackOutput):
    url = os.environ.get("SLACK_WEBHOOK_URL")
```

`os.environ.get` gives `None` when the variable is missing, so the output fails its own `init()` ("url is not set") and the other plugins run on. `kanary.load_json()` works in the same place, for a file next to the plugin:

```python
    url = kanary.load_json("slack_webhook_url", filename="secrets.json")
```

Written in the class body, it is read when the plugin file is loaded: a missing file or key stops KANARY from starting, and a reload that hits one is refused (the running plugins stay). `kanary lint` uses none of the settings, so there a missing file or key only gives a warning and `None`. Keep `secrets.json` next to the plugin with `chmod 600`, and out of version control (for example in `.gitignore`). Built-in helpers take secrets as plain settings (`url`, `password`, `smtp_password`, `headers`), so a test sets them directly.

## 2. Rule

### Minimum Interface

Required:

- `rule_id`
- `inputs`
- `severity`
- `evaluate(ctx)`

`tags` is a list of tags, or one tag as a string (`tags="infra"`). It is optional, but write it: a rule without tags reaches only the outputs without `include_tags`, cannot be silenced by tag, and is left out by remote imports filtered by tag. Lint warns about such a rule.

`source="postgres"` remains available as a shorthand for `inputs="postgres:*"` when you want to depend on everything exposed by one source.

`inputs` may be:

- one exact input, such as `inputs="postgres:temperature"`
- a list, such as `inputs=["primary:temperature", "secondary:temperature"]`
- a glob on the source side, the input side, or both, such as `inputs="postgres:*"` or `inputs="kernel_*:temperature"`

Internally, `inputs="postgres:temperature"` is normalized to `["postgres:temperature"]`.
Kanary resolves `resolved_sources` from these selectors at load/reload time and reevaluates the rule whenever one of those sources updates.

`evaluate(ctx)` must be fast and free of I/O: rules are evaluated one at a time (see "Where Plugin Code Runs"), and one slower than 100 ms is reported as `SLOW`.

`severity` (default `ERROR`) is the rule's default or fallback severity.
If you return `kanary.firing(..., severity=...)` or `kanary.Evaluation(severity=...)`, that specific evaluation overrides the class-level severity.
The `evaluate(payload, ctx)` of KANARY before 0.8.0 is an error now: read the values from `ctx` (`ctx.value()`, `ctx.source_payload()`).

Optional metadata:

- `owner`
- `description`
- `runbook`

These appear in the alert API and in the viewer detail panel. `runbook` (what to check first, a link to a dashboard) also goes with the rule's notifications: a "Runbook" field in Slack, Mattermost, Discord, and mail (URLs become links), and `runbook` in the webhook JSON.

### RuleContext

Use input-based accessors:

- `ctx.inputs(selector=None, previous=False)`
- `ctx.value(selector=None, previous=False)`
- `ctx.timestamp(selector=None, previous=False)`
- `ctx.metadata(selector=None, previous=False)`
- `ctx.prev_value(selector=None)`
- `ctx.prev_timestamp(selector=None)`
- `ctx.prev_metadata(selector=None)`
- `ctx.names(selector=None, previous=False)`
- `ctx.values(selector=None, previous=False)`
- `ctx.timestamps(selector=None, previous=False)`
- `ctx.metadatas(selector=None, previous=False)`

Small conveniences built on these:

- `ctx.age(selector=None)`: seconds between the input's timestamp and `ctx.now`
- `ctx.delta(selector=None)`: current value minus previous value; `None` unless both are numbers
- `ctx.changed(selector=None)`: `True` when both values exist and differ
- `ctx.missing(*selectors)`: the selectors (default: the rule's `inputs`) that match nothing, plus matched inputs without a value

"Previous" is the snapshot of the source's previous poll. A source that polls faster than its data changes reads the same sample again, so `delta()` is then `0` and `changed()` is `False`. When that matters, either have `poll()` return `kanary.no_update()` when nothing is new (the snapshots are then not rotated, so previous is always the previous sample), or compare timestamps in the rule. A source that simply returns whatever it read is also fine; that is the source author's choice.

`ctx.inputs()` returns `InputView` items sorted by fully-qualified input name and removes duplicates when multiple selectors match the same input.

Each `InputView` has:

- `name`
- `source_id`
- `input_name`
- `value`
- `timestamp`
- `metadata`

For a single resolved input, you can omit the selector and call `ctx.value()` directly. Multi-input rules should usually iterate over `ctx.inputs()`.
If the selector would match more than one input, `ctx.value()` and the other scalar helpers raise an error instead of guessing.
`ctx.source_payload("source-id")` is the whole snapshot of one source, with the source's own input names; a rule that reads a single source may leave out the id. Use `ctx.inputs()` for cross-source data access.

```python
{
    "status": "ok",              # "empty" after kanary.no_data()
    "reason": "...",             # only when the poll gave one
    "metadata": {"rows": 12},    # only when the poll gave some
    "inputs": {"temperature": {"value": 21.5, "timestamp": datetime(...), "metadata": {}}},
}
```

Before 0.8.0 the inputs were under `"channels"`.

### Returning evaluations

The usual public API is one of:

- `kanary.ok(message, extra=...)`
- `kanary.firing(message, severity=..., extra=...)`
- `kanary.warn(...)`, `kanary.error(...)`, `kanary.critical(...)`
- `kanary.fire_if(...)`, `kanary.warn_if(...)`, `kanary.error_if(...)`, `kanary.critical_if(...)`
- `kanary.fire(...)` is the same as `kanary.firing(...)`, and `kanary.firing_if(...)` the same as `kanary.fire_if(...)`: use whichever reads better

Example:

```python
def evaluate(self, ctx):
    value = ctx.value()
    if value is None:
        return kanary.ok("temperature is missing")
    return kanary.error_if(
        value > self.threshold,
        f"temperature={value} is higher than {self.threshold}",
    ) or kanary.ok(
        f"temperature={value} is within limit",
    )
```

Accepted shorthand forms:

- `None` means `OK`
- `True` means `FIRING`, `False` means `OK`
- `(severity, message)` means `FIRING` with that severity
- `(None, message)` means `OK`
- a severity is written the same way everywhere (a rule's `severity`, `thresholds`, `kanary.firing(severity=...)`, an output's `minimum_severity`): `kanary.ERROR`, its name in any case (`"ERROR"`, `"Error"`, `"error"`), or its number (`30`). A rule that sets none is `ERROR`

If you do not specify a payload explicitly, Kanary automatically inherits the current source payload. Use `extra={...}` to merge additional fields into that payload.
`kanary.Evaluation(...)` remains available as an advanced form. Its `state` is `kanary.FIRING` / `kanary.OK` or the name in any case (`"firing"`), and its `severity` is written as anywhere else; another value makes the rule fail with the reason.

## 3. Output

### Minimum Interface

Required:

- `output_id`
- `emit(event)`

Optional metadata:

- `description`

`description` is surfaced by the API and the default Web viewer.

Optional:

- `init()`
- `terminate()`
- `include_tags`
- `exclude_tags`
- `exclude_states`
- `exclude_transitions`
- `minimum_severity`
- `max_retry`
- `max_reinit`

The `emit(event, ctx)`, `init(ctx)`, and `terminate(ctx)` of KANARY before 0.8.0 are an error now: drop the `ctx` argument.

`emit()` runs on the output's own thread. The engine only decides which outputs an event goes to and adds it to their queues, so evaluation, other outputs, and operator actions never wait for a delivery. Each output keeps the order of its events. Up to 1000 events wait per output; beyond that the oldest is dropped, logged, and recorded as not sent. The Outputs page shows how many events are queued and how many were dropped. On shutdown, queued events get up to 5 seconds to go out.

`include_tags` and `exclude_tags` support glob patterns.  
For example, `include_tags=["expert_*"]` matches tags such as `expert_db` and `expert_shift`. A pattern matches a tag, so even `include_tags=["*"]` does not match a rule without tags.

`exclude_states` drops events whose current state matches a listed value. It defaults to `["SUPPRESSED", "SILENCED"]`.

One tag pattern, state, or transition may be written as a string instead of a list (`include_tags="infra"`), and states and transitions in any case (`"ok"`); an unknown state or transition name is an error when the file loads. To add `OK` to the defaults and drop recovery events too, use `exclude_states=kanary.Output.exclude_states + ["OK"]`. An explicitly configured list replaces rather than merges with the defaults, so debugging and audit outputs that need every state event should set `exclude_states=[]`.

An `OK` goes only to an output that was sent a non-OK event for the rule since its last `OK` (a `FIRING`, an `ACKED`, or a state its `exclude_states` lets through): an `OK` closes what the output was told. "Sent" means its `emit()` worked: when the `FIRING` failed to go out (the destination was down through every retry), the output gets no `OK` for it either. So a rule that was `OK` before a silence or a suppression sends no `OK` when that ends, while a rule that fired, was silenced, and recovered during the silence sends its `OK` as the silence ends. After a restart, alerts restored as not `OK` count as sent. An `OK` held back this way is recorded as filtered in the output dispatch history.

The `OK` has the severity this output was last sent for the alert (`event.effective_severity`, which `minimum_severity` checks): an output with `minimum_severity=kanary.ERROR` that was sent a `CRITICAL` alert gets its `OK` also when the alert went down to `WARN` before it recovered. With `--state-db` this holds over a restart too: what each output was sent is kept.

To drop a transition that passes the filters, inspect `previous_state` and `current_state` in `emit()`:

```python
def emit(self, event):
    if event.previous_state == kanary.SILENCED and event.current_state == kanary.OK:
        return
    ...
```

`exclude_transitions` also starts empty. 

Common values for `exclude_states`:

- `OK`
  Recovery events.
- `FIRING`
  Active alert events.
- `ACKED`
  Operator acknowledgement events (`FIRING -> ACKED`).
- `SILENCED`
  An alert covered by an active silence, with its normal alert state temporarily muted.
- `SUPPRESSED`
  An alert held back by a rule in its `suppressed_by` or `depends_on` that is firing.

Common values for `exclude_transitions`:

- `UNACK`
  Derived transition for `ACKED -> FIRING`.
- `ESCALATED`
  Same-state severity increase, such as `FIRING(WARN) -> FIRING(ERROR)`.
- `DEESCALATED`
  Same-state severity decrease, such as `FIRING(CRITICAL) -> FIRING(ERROR)`.

Each output `event` includes:

- `previous_state`
- `current_state`
- `previous_severity`
- `current_severity`
- `transition`

`transition` is `None` for ordinary state changes, and one of `UNACK`, `ESCALATED`, `DEESCALATED` for derived transitions.

Example:

```python
@kanary.output(
    output_id="discord",
    include_tags=["sqlite"],
    minimum_severity=kanary.ERROR,
)
class DiscordOutput:
    def emit(self, event):
        ...
```

### Delayed Followups

`kanary.OutputFollowups` is an in-process helper for calling another output action when an alert remains unattended after its first delivery.

```python
def init(self):
    self.followups = kanary.OutputFollowups()

def terminate(self):
    self.followups.close()

def emit(self, event):
    followups = self.followups.for_event(event)

    if event.current_state == kanary.FIRING and event.previous_state != kanary.FIRING:
        followups.now(self.output_to_owner)
        followups.after(10 * kanary.minute, self.output_to_group)
        followups.after(20 * kanary.minute, self.output_to_mailing_list)
        return

    if event.current_state in {
        kanary.ACKED, kanary.SILENCED, kanary.OK, kanary.SUPPRESSED,
    }:
        followups.cancel()
```

`now()` invokes its callback synchronously with the current event. Callback exceptions propagate to `emit()`, so the Output retry/reinit policy applies.

The other callbacks run on the output's own thread between `emit()` calls, never at the same time as `emit()`; a slow `emit()` delays them. An exception there is logged and shown as the output's last error, and is not retried.

Each `after()` delay is an absolute offset from the first followup registered for the same `rule_id`. In this example, the mailing-list action runs 20 minutes after the initial delivery, not 20 minutes after the group action. A callback receives the latest event seen by `for_event(event)`, rather than the event captured when it was registered.

Use `cancel(callback)` to cancel only registrations for that callback. Cancellation cannot stop a callback that has already started. Registrations are kept only in memory and do not survive a process restart; call `close()` from `terminate()`.

An output that cancels followups on `SILENCED` or `SUPPRESSED` must set `exclude_states=[]` in its decorator so those events are not removed by default routing. `kanary new -t followups` writes a complete one.

There is no `fire_after` setting on rules. Where it goes depends on what the delay is for:

- A short excursion is not a problem at all (measurement noise): decide it in the source, so the UI and every output agree. For example publish the window minimum of a `kanary.Buffer` series and put a `RangeRule` on it; it is above the limit only when every sample of the window was (the `buffered` template).
- The alert may be shown, but this output should not page for it yet: schedule the notification with `after(delay, ...)` and `cancel()` it on recovery, and send the recovery only when the alert was sent (the `delayed` template).

Failure recovery defaults:

- `max_retry = 1`
- `max_reinit = 1`

If `emit()` raises, Kanary retries delivery on the output's thread before leaving the output in `FAILED`; meanwhile only this output's later events wait.
Attempt `N` waits `N**2` seconds first. With the defaults, Kanary:

1. waits 1 second and retries `emit()`
2. waits 4 seconds, runs `terminate() -> init()`, and retries `emit()`

An output whose `init()` failed (its server not reachable yet) still gets its events: its thread runs `init()` before sending each one, and while it is idle it tries `init()` again after 10 s, then twice as long each time, up to every 5 minutes. An event that comes while the destination is down is not sent later: it is recorded as failed in the dispatch history.

If all attempts fail, the output is `FAILED`. While it is `FAILED`, each later event gets a single attempt without waits, so a dead destination does not hold every queued event through the whole sequence; the first success makes the output `READY` again. Events that failed are not sent again.

## 4. Built-In Helper Classes

The helper classes live in `src/kanary/helpers/`, apart from the engine, and use only the plugin API described above, so each reads as an example. Plugins import them from `kanary` (`kanary.SQLWideSource`, `kanary.Buffer`), never from the module.

### Source Helpers

#### Keeping history: kanary.Buffer

`kanary.Buffer` keeps recent samples, so that a source can publish what it computes from them: averages, trends, spikes. It is an ordinary object: create it in `init()`, then add to it and read it in `poll()`.

```python
@kanary.source(source_id="vacuum", interval=10 * kanary.second)
class Vacuum(kanary.Source):
    def init(self):
        # per input: at most 1024 samples, none older than 1 hour (from the newest sample);
        # the first limit reached drops the oldest
        self.buffer = kanary.Buffer(size=1024, max_age=1 * kanary.hour)

    def poll(self):
        self.buffer.add(("pressure", read_gauge()))  # the same items as kanary.inputs()
        pressure = self.buffer["pressure"]
        if pressure.span < 10 * kanary.minute:  # wait until 10 minutes are buffered
            return kanary.skip(reason="filling the buffer")
        last_10m = pressure.window(10 * kanary.minute)
        return kanary.inputs(
            ("pressure", pressure.last.value),
            ("pressure_avg_10m", last_10m.mean()),
            ("pressure_min_5m", pressure.window(5 * kanary.minute).min()),
        )
```

A buffer holds, per input name, the samples added to it, oldest first. Each sample has `.name`, `.value`, `.timestamp`, and `.metadata`.

Adding:

- `add(...)` takes the same items as `kanary.inputs()`: `add(("pressure", value))`, several tuples, `("pressure", value, observed_at)` or `("pressure", value, observed_at, metadata)`, or a dict. A timestamp is a datetime or unix time; without one the sample is timestamped now (or with `timestamp=`). When the reading carries its own time, give it: it keeps the windows right.
- `add(result)` adds a whole poll result: `kanary.inputs(...)` or `super().poll()` of a helper. This adds history to any helper class: a subclass of `HTTPJSONSource`, say, whose `poll()` does `self.buffer.add(super().poll())` and then computes from the buffer. A result that is not ok (`no_update()`, `skip()`, an error) adds nothing. A helper with an `init()` of its own (`SQLSource`, `LogCountSource`) needs `super().init()` in yours.
- A sample with the same timestamp as the input's newest one replaces it (the same reading again), so a source that reads an unchanged reading does not distort the statistics; an older one is ignored.

`self.buffer["name"]` is a `kanary.Series`: a read-only copy of that input's samples, oldest first. A name never added raises `KeyError`, naming the inputs the buffer has, so a typo shows at once; check an input that may not have arrived yet with `"name" in self.buffer`.

- Samples: `first`, `last` (each a sample with `.value`, `.timestamp`, `.metadata`, or `None`), `values`, `timestamps`, `len()`, iteration, `span` (seconds from first to last)
- Selecting: `window(duration)` - the samples of the last `duration` (`10 * kanary.minute`), counted back from the newest; `tail(n)` - the newest `n`
- Statistics: `mean()`, `median()`, `min()`, `max()`, `sum()`, `stddev()`, `delta()` (last minus first), `rate()` (from the first and last sample), `slope()` (least-squares over every sample, less sensitive to one noisy point) - both per second; `* kanary.minute` gives per minute, `zscore()` (standard deviations of the newest value from the mean), `eta(target)` (seconds until the trend reaches `target`; `None` when flat or moving away), `changes(tolerance=)` (how often the value changed between samples: toggling, flapping), `unchanged_seconds(tolerance=)` (how long the value has stayed the same: a frozen sensor keeps sending new timestamps with the same value)

`values` and `timestamps` are plain lists, so for anything else `numpy.array(series.values)` works.

Several inputs at once: `self.buffer.select("temp_*")` gives `{name: Series}` for every matching name, and `self.buffer.last_values("temp_*")` gives `{name: newest value}`; `self.buffer.names` lists them all. `kanary.inputs(self.buffer.last_values())` publishes the newest values as they are. `print(self.buffer)` shows, per input, how many samples are kept, from when to when, and the newest value.

What to publish while the buffer is still filling is up to the source. Check `len(series)` or `series.span` in `poll()` and, for example, return `kanary.skip()` until there is enough (no new snapshot, rules are not evaluated), as above; or publish partial results right away. A statistic returns `None` when there is no data for it (an empty window, one sample for a rate, fewer than two values for a standard deviation), so that input simply has no value. Statistics use only numeric values; `None` and text are left out.

`kanaryctl test-poll` runs `poll()` too. What it adds to a buffer is dropped when the test poll ends, so the next real poll sees the history unchanged. A buffer has no lock: use it from `poll()` (test polls run on the same thread), or lock it yourself when another thread, a listener say, adds to it.

History lives in the source, not in rules: rules only see the current and previous snapshot. Publish the results as inputs from `poll()` and check them with an ordinary rule, for example a `ThresholdRule` with `direction = "low"` on an `eta` input. Only sampled values are compared, so a value that changes and comes back between two samples is not seen; count faster changes upstream. The buffer lives in memory and starts empty after a reload or restart. The `buffered`, `trend`, `spike`, and `frozen` templates show the statistics in use.

#### CommandSource and SSHCommandSource

`kanary.CommandSource` runs `command` on every poll (a list runs as-is; a string is split like a shell would, without a shell) with a finite `timeout`. stdout is converted to numeric inputs according to `output_format`:

- `"number"`: the whole output is one number, named `value_name`
- `"keyvalue"`: `name=value` pairs separated by spaces or newlines
- `"json"`: a number or an object; nested keys become `a.b`
- `"auto"` (default): chosen from the shape of the output

Inputs can hold any Python object, but the helpers convert values with `kanary.to_number()` (`"85%"` becomes `85.0`, `true`/`false` become `1.0`/`0.0`) because rules compare numbers. Values that do not convert are kept in `metadata["text"]`. The result metadata names only the program, not its arguments, since arguments may carry tokens. The exit code is reported as the `exit_code` input (`report_exit_code = False` leaves it out), and a timeout is a poll failure. Override `parse(stdout)` for custom output. `env` values are passed as text (`{"RETRIES": 3}` works), and a relative `cwd` is next to the plugin file, as for the other helpers.

`kanary.SSHCommandSource` runs the command on `host` through `ssh` with `BatchMode=yes` and `ConnectTimeout`, using `user`, `port`, `identity_file`, and `ssh_options`. The remote command runs in a subshell followed by a marker line carrying its exit code, so a failure of ssh itself (no marker) is raised as a poll failure while the command's own exit code, even 255, is reported as `exit_code`. This needs a POSIX-like login shell on the remote host; with `wrap_exit_code = False`, exit code 255 is always treated as an ssh failure. This is the usual way to read disk, load, or GPU status of other machines.

#### PingSource, TCPSource, HTTPStatusSource

Each checks its targets in parallel once per poll and reports what it measured:

- `PingSource`: `hosts` maps input names to hosts (or lists hosts, each named after itself); `<name>.rtt_ms` is the round-trip time of one ICMP echo through the system `ping` command.
- `TCPSource`: `targets` maps input names to `"host:port"` (or lists them: `"db1:5432"` is named `db1_5432`); `<name>.connect_ms` is how long the TCP connect took. Nothing is sent after connecting.
- `HTTPStatusSource`: `urls` maps input names to URLs (or lists them: `"https://example.org/health"` is named `example.org/health`); `<name>.status` is the HTTP status code and `<name>.response_ms` the response time. An error status such as `503` is a value to check with a rule, not a failure.

A target that does not answer is a result, not a poll failure: its inputs have no value (`None`) and the error text is in their metadata. Put `missing_fires = True` on the rule so that fires, for example a `RangeRule` with `high = 200.0` on `*.rtt_ms`, or an `ExpectedValueRule` with `expected = 200` on `*.status`. To read values out of a JSON response, use `HTTPJSONSource`.

#### FileSource and LogCountSource

`kanary.FileSource` reports `<name>.exists`, `<name>.age_seconds`, and `<name>.size_bytes` for each entry of `paths`, and `<name>.value` from the last line when `read_last_line = True`. A relative path is resolved next to the plugin file.

An entry can be a glob pattern such as `"/data/runs/*.root"` (`**` matches subfolders). The inputs then describe the newest matching file, plus `<name>.count` (matching files) and `<name>.total_size_bytes`; `<name>.exists` is `1` when anything matches. `paths` may also be a plain list; each entry is then named after its file name (`"daily.tar.gz"`) or, for a pattern, its fixed part (`"*.root"` becomes `root`), and two entries with the same name raise.

`kanary.LogCountSource` reports, for each regular expression in `patterns`, how many new lines in `path` matched since the previous poll; `interval` is therefore the counting window. Rotation and truncation restart from the top of the file, including a `copytruncate` file that has already grown past the old position (detected by comparing the last bytes read), and `start_at_end = True` ignores what was in the file at startup.

#### HTTPJSONSource

`kanary.HTTPJSONSource` GETs `url` with `headers` and a `timeout`, and maps `fields` (input name to dotted path such as `data.sensors.0.value`) to numeric inputs. With no `fields`, every numeric leaf is used. Missing paths and non-numeric values are listed in the result metadata. Override `select(document)` for other shapes, and set tokens from the environment in `init()`.

#### PrometheusSource

`kanary.PrometheusSource` reads a page in the Prometheus text format, such as node_exporter's `/metrics` or an application's, with `url`, `headers`, and `timeout`. `metrics` maps an input name to a series, written as in Prometheus:

```python
metrics = {
    "load1": "node_load1",
    "root_free_bytes": 'node_filesystem_avail_bytes{mountpoint="/"}',
    "temp.{sensor}": 'node_hwmon_temp_celsius{chip="platform_coretemp_0"}',
}
```

The labels written must match exactly; the others may be anything. A series selector that matches several series needs the label that varies in the input name: `{sensor}` is replaced by each series' label, giving `temp.temp1`, `temp.temp2`. Without it the poll fails and names the label. A series that is not on the page is listed in the result's `metadata["missing"]`, and `NaN` has no value; the page not answering is a poll failure. A counter (`..._total`) only grows: a `RateRule` on it gives its rate per second.

#### SQLWideSource and SQLLongSource

Usually only settings are needed; the connection and the SELECT are built from them. Which class depends on the shape of the table:

A **wide** table has one row per time and one column per quantity. `SQLWideSource` reads the newest row; each column becomes an input.

| observed_at | temperature | humidity | pump_state |
|---|---|---|---|
| 2026-11-20 10:00:00 | 21.5 | 40.1 | 1 |
| 2026-11-20 10:00:30 | 21.6 | 40.3 | 1 |

A **long** table has one row per quantity and time: a name column and a value column. `SQLLongSource` reads the newest row of every name; each name becomes an input.

| observed_at | metric_name | metric_value |
|---|---|---|
| 2026-11-20 10:00:00 | room_a.temperature | 21.5 |
| 2026-11-20 10:00:00 | room_b.temperature | 19.8 |
| 2026-11-20 10:00:30 | room_a.temperature | 21.6 |

A wide table suits a fixed set of quantities written together; a long table suits many or changing quantities, written each on its own.

- Connection: `database_type` (`"sqlite"`, `"postgresql"`, or `"mysql"`, which also covers MariaDB), `database` (a file path for SQLite, relative to the plugin file; otherwise the database name), `host`, `port`, `user`, `password` (a secret: `password = os.environ.get("DB_PASSWORD")`), and `timeout` (connect and statement timeout). PostgreSQL uses `psycopg`, which is installed with KANARY; MySQL needs the `pymysql` package, installed into the Python environment that runs KANARY (see [Deployment: When A Plugin Needs Additional Python Modules](deployment.md#when-a-plugin-needs-additional-python-modules)). The connection uses autocommit, and a failing poll reconnects through the normal retry/reinit path.
- `SQLWideSource`: `table`, `timestamp_column`, and `columns` (input name to column; empty means every column). It reads the newest row: `SELECT ... ORDER BY timestamp_column DESC LIMIT 1`.
- `SQLLongSource`: `table`, `name_column`, `value_column`, and `timestamp_column`. It reads the newest row of every name; `lookback` limits the scan to recent rows. `names` maps upstream names to input names and filters the rest.

Table and column names must be plain identifiers (`schema.table` is allowed). To read anything else, such as a join, a view, or a `WHERE` clause, set `query` (with `params` in the driver's parameter style); it is then used as written, newest rows first, and the table settings are ignored. For another driver, override `connect()` and return any DB-API connection.

`timestamp_column` sets each input's timestamp. Timestamps stored without a timezone are read in `timestamp_timezone` (a time zone as `kanary.to_timezone()` reads it: an IANA name such as `"Asia/Tokyo"`, an offset such as `"+09:00"`, `"+9"`, or `"UTC+9"`, or `"UTC"`), or as this host's local time when it is unset. An offset never changes, so use an IANA name where daylight saving time applies. The two classes are short on purpose; read them in `src/kanary/helpers/sql.py` when writing a source for another shape.

#### Values that arrive by themselves (webhook, MQTT, callbacks)

To evaluate a value as soon as it arrives, let `poll()` wait for it: the rules of the source run right after `poll()` returns, and `interval = 0` calls `poll()` again at once, so the source is always waiting.

- `kanary.HTTPPushSource` receives HTTP POSTs (`host`, `port`, `path`, `token`). The body is a JSON object of input name to value, or to `{"value": ..., "timestamp": ...}` (unix time or ISO 8601). With `token` set, requests need `Authorization: Bearer <token>`. It uses the standard library's `http.server`: fine on a trusted network; to accept from the internet, put a reverse proxy with TLS in front. Override `parse(body)` for another body format.
- `kanary.MQTTSource` subscribes to `topics`, a mapping of topic filter to input name; `None`, which every filter with a wildcard needs, names each input after its topic (`plant/pumps/p1/flow` becomes `plant.pumps.p1.flow`). Settings: `host`, `port`, `username`, `password`, `tls`, `qos`. A payload is a number, a state (`"ON"` / `"true"` become 1, as `kanary.to_number()` reads them), other text (kept as it is), or JSON; a JSON object gives one input per key. The input `mqtt.connected` is 1 while connected. The client reconnects by itself and subscribes again after each connect. It needs the `paho-mqtt` package in the Python environment that runs KANARY (see [Deployment: When A Plugin Needs Additional Python Modules](deployment.md#when-a-plugin-needs-additional-python-modules)); it is imported in `init()`, so without it only that source fails. Override `parse(payload)` for another payload format.
- For any other listener (a device library's callback, a socket, a serial line), use `kanary.Inbox`:

```python
def init(self):
    self.inbox = kanary.Inbox()
    ...  # start your listener; on its own thread it calls self.inbox.put(("temperature", 21.5))

def poll(self):
    return self.inbox.wait(timeout=1.0)
```

  - `put()` takes the same items as `kanary.inputs()`, works from any thread, and never blocks. A value without a time of its own gets the time it arrived, not the time `poll()` took it.
  - `wait(timeout)` waits up to `timeout` seconds for something to arrive, then returns every input with its newest value: inputs that did not arrive this time keep their last value, so they do not count as missing, and a burst is evaluated once. When nothing arrived it returns `kanary.skip()`: no new snapshot, and the rules do not run.
  - `take(timeout)` returns every sample that arrived instead, for a `poll()` that must see each value of a burst, for example into a `kanary.Buffer`.
  - At most `size` samples (10000) wait; beyond that the oldest are dropped and counted in `dropped`, which the result metadata also shows.
  - `kanaryctl test-poll` shows what `wait()` would return, without waiting or taking anything, so no `test_poll()` is needed.
- KANARY can stop or reload a waiting source only between polls and waits 2 seconds for that, so wait about 1 second at a time. `terminate()` stops the listener; the old instance's `terminate()` runs before the new one's `init()`, so a reload can bind the same port again.
- Nothing arriving is not an error for the source: put a `StaleRule` with `evaluation_interval` on its inputs.
- `interval = 0` is only for a `poll()` that waits: one that returns at once would spin a CPU core.

`HTTPPushSource` and `MQTTSource` run with `interval = 0` and nothing else: leave `interval` and `schedule` out; any other makes `init()` fail with a message. The `http-push`, `mqtt`, and `push` (your own listener) templates show each.

### Rule Helpers

The built-in rules only read the values of the inputs selected by `inputs` (there are no `field` settings that point into the source payload). They are kept short, so reading `kanary/helpers/value_rules.py` before subclassing shows exactly what they do.

#### ValueRule

`kanary.ValueRule` is the base of `RangeRule`, `ThresholdRule`, `StaleRule`, and `RateRule`. Its `evaluate()` judges each selected input on its own and fires with the most severe result. A subclass overrides only small methods:

- `check(value, ctx)`: return `True` or a severity to fire, `False` or `None` when normal
- `value_of(item, ctx)`: the number to judge (default: the input's value); `None` lists the input in `missing_inputs`
- `phrase(fired)`: the end of the message, such as `"out of range [0, 100]"`; `format_value(value)` formats the number

Messages read `<source:input>=<value> <phrase>`. The payload has `values` (input name to judged value), `matched_inputs`, and `missing_inputs`. With `missing_fires = True`, an input without a value to judge fires too, with the `error` from its metadata in the message; `StaleRule` sets it, and it is the way to alert on a probe that got no answer.

```python
@kanary.rule(rule_id="pump.mode.even", inputs="pump:*", severity=kanary.WARN, tags=["pump"])
class EvenMode(kanary.ValueRule):
    def check(self, value, ctx):
        return value % 2 == 0
```

#### RangeRule

- single-range rule
- single severity
- `low` and `high` are both optional; omitting one makes a one-sided check (for example, only `high` fires above the upper bound). `kanary lint` warns when both are omitted, because the rule can never fire
- `lower_inclusive` and `upper_inclusive` define `[]` vs `()`
- `hysteresis` narrows the normal range by that much while the rule is alerting: with `high = 100` and `hysteresis = 5`, the alert fires above `100` and stays `FIRING` until the value is `95` or lower. A hysteresis larger than half of `high - low` raises, because the alert could never clear
- `fire_when = "outside"` (default) treats `[low, high]` as normal; `fire_when = "inside"` treats it as a forbidden band, so entering it fires (for example a resonance band). Inclusive flags then say whether the edges belong to the forbidden band, and hysteresis widens the band while alerting
- when multiple inputs are matched, each input is evaluated independently and any out-of-range input fires the rule. Hysteresis is per rule: while alerting, every input is checked against the narrowed range
- inputs whose value is not a number are listed in `missing_inputs` and do not fire
- methods to use when subclassing: `check(value, ctx)`, `breaches(value, margin)`, `range_text()`

#### DifferenceRule, KOfNRule, AggregateRule, CountRule

Rules over several inputs, usually selected with a glob:

- `DifferenceRule`: fires when highest - lowest exceeds `max_difference` (redundant sensors). Stays OK below `min_inputs` (default 2).
- `KOfNRule`: fires when at least `k` inputs break a RangeRule-style range (`low`, `high`, `fire_when`, inclusive flags). `count_missing = True` also counts inputs without a value.
- `AggregateRule`: checks the `"sum"`, `"mean"`, `"median"`, `"min"`, or `"max"` of the inputs against a RangeRule-style range, with hysteresis.
- `CountRule`: fires when the number of inputs that have a value is below `min_count` or above `max_count`.

#### ExpectedValueRule

Fires when an input's value is not `expected`, which is one value or a list such as `[200, 204]`. Numbers compare by value (`1 == 1.0 == True`) and text values such as `"RUNNING"` work too, so it fits status codes, 0/1 flags, and states where a range reads awkwardly. With `fire_when = "equal"`, the listed values fire instead (for example error codes).

#### StaleRule

- checks the age of each input via its timestamp
- when multiple inputs are matched, any stale or timestamp-missing input fires the rule
- `values` in the payload maps input names to their age in seconds
- a timestamp more than `future_tolerance` seconds in the future (default `30 * kanary.minute`) fires a `WARN` with "(time zone?)": such an input would never look stale, and it is usually a time written without its time zone. `future_tolerance = None` turns this off

Rules are normally evaluated when one of their Sources updates. Set `evaluation_interval` to a positive number of seconds (or text such as `"30s"` or `"5m"`) to also evaluate a Rule on an independent fixed interval:

```python
@kanary.rule(
    rule_id="kanary.peer.heartbeat.stale",
    inputs="kanary.peer:heartbeat",
    evaluation_interval=30.0,
)
class PeerHeartbeatStale(kanary.StaleRule):
    timeout = 120.0
```

Scheduled evaluation uses saved Source snapshots and does not poll or update a Source. For a multi-Source Rule, use `ctx.inputs()` or `ctx.source_payload("source-id")`: without the id, `ctx.source_payload()` is an error there, however the rule was triggered. Wildcards only select inputs that exist in a snapshot and do not declare a required input set. Check required inputs explicitly in custom Rule code.

#### RateRule

- computes each input's rate from its current and previous value and timestamp, and checks it against a RangeRule range (hysteresis included)
- the rate is per second: write the bounds with the time units, e.g. `high = 1.0 / kanary.minute` for 1 per minute; messages show the rate in the unit of the larger bound
- inputs without a previous sample or with non-numeric values are listed in `missing_inputs` and do not fire
- "previous" is the source's previous poll. When a poll returns the same sample again (the timestamp did not advance), the rate judged last time is kept, so the alert does not clear just because nothing new arrived. A source can also return `kanary.no_update()` when nothing is new
- a rate from two samples amplifies noise: a value with resolution `q` read every `Δt` seconds gives rates in steps of `q / Δt`, and random noise grows by about `√2 / Δt`. Keep the range well outside that, or publish a window `slope()` of a `kanary.Buffer` series and use a `RangeRule` on it
- `values` in the payload maps input names to their rate

#### ThresholdRule

- multi-level severity
- `direction = "high" | "low"`
- `thresholds = [(value, severity), ...]`
- `hysteresis` moves the thresholds up to the current severity back by that much while alerting, so severity drops and recovery are delayed; reaching a higher severity is never made easier
- methods to use when subclassing: `check(value, ctx)`, `level(value, margin)`
- when multiple inputs are matched, the rule fires if any input matches and uses the highest matched severity

Example:

```python
@kanary.rule(
    rule_id="sqlite.value1.threshold",
    inputs="sqlite:value1",
    severity=kanary.WARN,
    tags=["sqlite", "value1"],
)
class Value1Threshold(kanary.ThresholdRule):
    direction = "high"
    hysteresis = 1.0
    thresholds = [
        (20.0, kanary.WARN),
        (24.0, kanary.ERROR),
        (28.0, kanary.CRITICAL),
    ]
```

`RangeRule` and `ThresholdRule` provide intentionally simple hysteresis behavior. If you need asymmetric clear margins or more complex recovery logic, write a custom rule.

#### RemoteKanarySource

- reads `/export-alerts` from another Kanary node
- returns each remote alert as an input: 1 while it is not OK, the alert in its metadata
- can forward `ack`, `unack`, `silence`, and `unsilence` to the remote API
- typically configured with `base_url` and `interval`: `base_url = "http://remote-kanary.example:8000"`, the node's address (`url` is another name for it; a URL that ends with `/export-alerts` is taken without it)
- uses the hostname as the default node ID
- skips imported alerts when the local node ID is already present in `mirror_path`

#### RemoteAlarm

- mirrors one remote alert into one local rule via `remote_alarm_id`
- preserves remote state and severity locally
- can forward local operator actions when `propagate_ack` or `propagate_silence` is enabled. The local ack or silence always holds: when the peer cannot be reached (during an incident it may be what is down), the API answer, `kanaryctl`, and the viewer show a warning, and a silence the peer did not cancel ends there at its end time
- carries `origin_node_id`, `origin_rule_id`, and `mirror_path` in the payload

#### import_remote_alarms

- factory that generates one `RemoteAlarm` rule per id in `remote_alarm_ids` (required)
- the ids are written out on purpose: reading them from the remote node would make loading the file depend on that node being up, and after a power cut both nodes start at once
- supports `prefix`, `suffix`, `add_tags`, `owner`, `severity`, and `include_rule_ids` / `exclude_rule_ids` (glob patterns over the listed ids)
- the local rule id is `prefix.remote_id.suffix`: `prefix="remote"` makes `remote.daq.link` (`prefix="remote."` gives the same)
- each generated rule is treated as an independent local rule; while the remote alert fires, its own severity and message are shown

### Output Helpers

#### MailOutput

- sends email through SMTP
- configured with `smtp_host`, `smtp_port`, `sender`, and `recipients` (a list, or one string: `"ops@example.org, oncall@example.org"`); `smtp_username` and `smtp_password` when the server needs a login
- port 587 or 25 uses STARTTLS (`use_starttls = False` for a relay without TLS); port 465 uses TLS from the start (or set `use_ssl = True` for another port)
- keep `smtp_password` out of the plugin file: `smtp_password = os.environ.get("SMTP_PASSWORD")` (or `kanary.load_json()`)
- each mail has a plain-text part and an HTML part with the same content as the chat outputs (colored header, message, a table of fields); override `subject(event)`, `body(event)`, or `html(event)` (return `None` for plain text only), and set `viewer_url` to link the viewer

Example:

```python
@kanary.output(output_id="mail")
class MailAlert(kanary.MailOutput):
    sender = "kanary@example.com"
    recipients = ["operator@example.com"]
    subject_prefix = "[KANARY production]"

    def body(self, event):  # the plain-text part; html() is the HTML part
        notice = kanary.describe(event)
        return f"{notice.title}\n\n{notice.message}\nRunbook: https://wiki.example.org/kanary"
```

Override `recipients_for(event)` to choose recipients per event, for example from an `OnCallSchedule`; `recipients` can then stay empty.

#### WebhookOutput, SlackOutput, MattermostOutput, DiscordOutput

`kanary.WebhookOutput` POSTs `payload(event)` as JSON to `url` with `headers` and a `timeout`, and raises on an HTTP error so the event is retried. The default payload has `rule_id`, `state`, `previous_state`, `transition`, `severity`, `message`, `owner`, `tags`, `occurred_at`, `active_since`, and `acked_by`. `text(event)` is a one-line summary.

A webhook URL is usually a secret: `url = os.environ.get("WEBHOOK_URL")`, as shown under plugin configuration above.

`SlackOutput`, `MattermostOutput`, and `DiscordOutput` send a rich message in each service's format: a colored bar (green when OK, grey when silenced or suppressed, otherwise by severity: INFO blue, WARN yellow, ERROR red, CRITICAL purple), a title such as `🔴 FIRING ERROR oil.temperature`, the rule's message, and fields for state, severity, since, ack, owner, tags, and the judged values. An event sent before the rule was evaluated again after a restart (a silence that ended while KANARY was stopped) has an `Evaluated` field with the time of the values, "before the restart". Notifications and previews show the one-line `text(event)`. Set `viewer_url` to link the title to the KANARY viewer; `MattermostOutput` and `DiscordOutput` also take `username`, and `MattermostOutput` takes `icon_url`. Override `payload()` for another layout.

`kanary.describe(event)` returns those parts as a `kanary.Notice` (`title`, `headline`, `symbol`, `color`, `color_hex`, `message`, `fields`, `time`, `text`, `link`), for an output of your own such as Teams or a pager, and for an overridden `payload()`.

#### FileOutput and CommandOutput

- `FileOutput` appends one JSON object per event to `path` (relative to the plugin file). `include_payload = True` adds the rule's payload. Override `record(event)` to change the object; `kanary.event_record(event)` is the default object.
- `CommandOutput` runs `command` per event with the event as JSON on stdin and `KANARY_RULE_ID`, `KANARY_STATE`, `KANARY_PREVIOUS_STATE`, `KANARY_TRANSITION`, `KANARY_SEVERITY`, `KANARY_MESSAGE`, `KANARY_OWNER`, and `KANARY_TAGS` in the environment. A non-zero exit raises.

#### QuietHours

`kanary.QuietHours` holds alert events during quiet hours and sends them later. Like `OutputFollowups`, an output creates it in `init()`, closes it in `terminate()`, and calls it from `emit()`; outputs that do not use it are unaffected.

```python
@kanary.output(output_id="slack", minimum_severity=kanary.WARN)
class Slack(kanary.SlackOutput):
    url = os.environ.get("SLACK_WEBHOOK_URL")

    def init(self):
        super().init()
        self.quiet = kanary.QuietHours([("22:00", "07:00")], timezone="Asia/Tokyo",
                                       minimum_severity=kanary.CRITICAL,
                                       release="summary", summary=self.emit_summary)

    def terminate(self):
        self.quiet.close()

    def emit(self, event):
        self.quiet.send(event, super().emit)
```

- `send(event, callback)` calls `callback(event)` right away unless the event falls in a window (`("HH:MM", "HH:MM")`, may wrap past midnight, read in `timezone`; `"24:00"` is the midnight that ends the day; one window may be written without the list) and is below `minimum_severity` (written as anywhere else: `kanary.CRITICAL`, `"critical"`, `40`). A window that covers the whole day is an error: use the output's `minimum_severity` for that.
- Such events are held until the quiet period ends; back-to-back windows count as one period. `release` decides what goes out then: `"all"` (default) passes every held event to `callback` in its original order; `"latest"` only the newest held event of each rule; `"summary"` calls `summary(events)` once with all of them, for one message instead of many (`SlackOutput`, `MattermostOutput`, `DiscordOutput`, `WebhookOutput`, and `MailOutput` have `emit_summary(events)`; a single held event goes to `callback` as usual). `defer=False` drops held events instead.
- When an event of a rule is sent right away (for example an escalation to `CRITICAL` at night), that rule's held events are sent first with `"all"`, so the receiver never sees them out of order; with `"latest"` and `"summary"` they are dropped, since the event sent now is newer.
- `timezone` is anything `kanary.to_timezone()` reads: an IANA name (`"Asia/Tokyo"`), an offset (`"+09:00"`, `"+9"`, `"UTC+9"`), or `"UTC"`; without it, the host's local time.
- Held events are sent on the output's own thread, between `emit()` calls. A failure there is logged and not retried; a failure while sending right away propagates, so the engine retries `emit()` as usual.
- Held events live in memory and are lost on `close()`, reload, and restart (with a warning in the log). `held()` lists them and `applies(event)` tells whether an event would be held.

See the `quiet-hours` template.

#### OnCallSchedule

`kanary.OnCallSchedule(shifts, timezone=None, default=())` answers who is on call: `schedule.recipients(at)`. Each shift is a mapping, typically from `kanary.load_toml()`:

- weekly: `weekdays` (names, abbreviated or not: `["mon", "Tuesday"]`, a range `"mon-fri"`, or `"mon,wed,fri"`; an unknown name is an error), `from`, `to` (may wrap past midnight; `"24:00"` ends at midnight; leave both out for the whole day), `recipients` (a list, or one comma-separated string)
- dated: `start`, `end`, `recipients`; dated shifts win over weekly ones

`default` (a list, or one comma-separated string) is returned when no shift matches. The `oncall` template combines it with `MailOutput.recipients_for()` and `QuietHours`.

### Invalid Settings

Helper classes raise `ValueError` with the setting's name when a setting is invalid, at the point where it is used: in `init()` or `poll()` for sources, `init()` or `emit()` for outputs, and `evaluate()` for rules. The plugin then fails like any other plugin error and shows the message in its status; other plugins keep running. Settings may therefore also be assigned in `init()`, for example from `kanary.load_toml()`.

## 5. User-Defined Factories

Kanary does not require built-in factories for every repeated pattern.
If you prefer, you can write your own factory functions in plain Python and generate plugin classes yourself.

The natural pattern is:

1. build a class dynamically with `type(...)`
2. fill in the class attributes or methods you need
3. apply `kanary.source(...)`, `kanary.rule(...)`, or `kanary.output(...)` to register it

This keeps the generated plugins as normal, independent plugins after registration.

For example:

- generate one source from a mapping of input names to values
- generate several `ThresholdRule` classes from a list of inputs

See [examples/factory_patterns.py](../examples/factory_patterns.py) for a concrete example.

That example includes:

- `make_constant_source(...)`
  Generates a simple source class from a dictionary of input names to values.

## 6. Self-Monitoring Pattern

`kanary new -t plugin-health` writes a source and two rules that watch this KANARY's own plugins; they are ordinary plugins, placed and edited like any other.

- `kanary.PluginHealthSource` reads the engine it runs in (no HTTP, no settings) and publishes, for every other plugin, `<type>.<plugin_id>.failed` (1 while `FAILED`, with the error in the metadata); for outputs also `output.<id>.queued` and `output.<id>.dropped` (events dropped since the previous poll); for rules `rule.<id>.slow`.
- The template's rules fire when any plugin has failed (listing each with its error) and when an output drops events.

An alert about a failed output goes out through the other outputs; with a single output only the viewer shows it. KANARY itself stopping cannot be seen from inside: watch it from another KANARY (the `peer` template), or send a heartbeat to a service that alerts when it stops (the `heartbeat` template).
- `make_threshold_rule(...)`
  Generates one `ThresholdRule`-based rule class.

This approach is often enough when only one project needs the factory. If the pattern becomes common across multiple deployments, that is the point where adding a built-in helper may make sense.

## States And Dependencies

Rule relationships: `suppressed_by` and `depends_on` name the rules (one id or a list) whose failure explains this rule's. While one of them fires, the rule is `SUPPRESSED`, and `SUPPRESSED` is excluded from outputs by default: when a DAQ link goes down, you get the link's alert, not one per channel behind it.

| | `evaluate()` while the parent fires | shown as |
|---|---|---|
| `suppressed_by` | called | `SUPPRESSED`, with what the rule judged: `suppressed by daq.link; would fire: daq:hv1=0 out of range [1000, 2000]` |
| `depends_on` | not called | `SUPPRESSED`: `blocked by daq.link (not evaluated)` |

Use `suppressed_by` for a rule that judges each value on its own (`RangeRule`, `ThresholdRule`, ...): the viewer shows what it would say. Use `depends_on` for a rule whose `evaluate()` cannot work, or should not see the values, while the parent fires: one that keeps its own history (a `kanary.Buffer` in the rule) would otherwise keep the values read during the failure and fire after it.

- "Fires" is the parent's own judgment: silencing or acknowledging the parent does not hold its children back.
- A parent is evaluated before the rules it holds back, so on one source the parent's alert comes alone, whatever order the rules are written in.
- When the parent fires, a rule on another source becomes `SUPPRESSED` at once.
- When the parent recovers, the rule comes back once every source it reads has given a value after the recovery: in the same poll on the same source, otherwise at its sources' next polls (it shows `recovered; waiting for a new value` until then). A rule that reads sources A and B stays held while only A has a new value. The values read during the failure are not judged again; a source that has given no value at all has none from the failure.
- If the rule's own source sees the problem before the parent's source does, the rule's alert is sent first: suppression works best when the parent is read at least as often as the rules behind it.
- An `evaluate()` that raises while its parent fires leaves the rule `SUPPRESSED` (its status shows the error).
- A rule that lists itself, or rules that list each other in a cycle, are an error; a name that is not a rule is a lint warning.

Alert states:

- `OK`
  The rule currently evaluates as healthy.
- `FIRING`
  The rule currently evaluates as abnormal.
- `ACKED`
  The alert is still abnormal, but an operator acknowledged it. An acknowledgement belongs to one alert: it ends when the rule itself judges the alert over, also while a silence or a parent hides that (the history then shows an unack by `system`), so the rule's next alert is not shown as acknowledged.
- `SILENCED`
  An active silence covers the alert and temporarily mutes its normal alert state.
- `SUPPRESSED`
  A rule in `suppressed_by` or `depends_on` fires, so this alert is held back.

Derived transitions:

- `UNACK`
  Emitted when an acknowledged alert is reopened (`ACKED -> FIRING`).
- `ESCALATED`
  Emitted when severity rises while the state stays the same.
- `DEESCALATED`
  Emitted when severity drops while the state stays the same.

In practice:

- `SILENCED` shows viewers and history that an alert is muted on purpose. Remove it from `exclude_states` explicitly if an output should receive it.
- Rule removals during reload are not represented as an alert state. They are recorded in history as an operator action with `action_type = "rule_removed"`.
