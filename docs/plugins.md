# Plugin Model

This document first explains the minimum interface a user needs to implement, and then the built-in helper classes.

## 1. Source

### Minimum Interface

Required:

- `source_id`
- `poll()`

Optional:

- `interval`
- `schedule`
- `init()`
- `terminate()`
- `max_retry`
- `max_reinit`

If you omit both `interval` and `schedule`, Kanary uses `interval = 60.0`.
If you use `schedule`, do not set `interval` at the same time.
The older `poll(ctx)`, `init(ctx)`, and `terminate(ctx)` signatures still work for compatibility, but lint warns and the documentation now treats the argument-less form as the formal API.

`interval` is a polling interval in seconds.
`schedule` is a Unix cron-compatible 5-field string interpreted in the local time
of the Kanary server. A small set of macros is also supported, such as
`@hourly`, `@daily`, `@weekly`, `@monthly`, and `@yearly`.

Examples:

- `interval = 60.0`
- `schedule = "*/5 * * * *"`
- `schedule = "@hourly"`

Example:

```python
@kanary.source(source_id="sqlite", interval=5.0)
class SqliteSource:
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

### Returning inputs

The usual public API is `kanary.inputs(...)`, `kanary.no_data(...)`, `kanary.no_update(...)`, and `kanary.skip(...)`.

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
- outer `metadata=` becomes `SourceResult.metadata`
- `kanary.no_data(reason=..., metadata=...)` means the poll succeeded and produced an empty snapshot; Kanary updates the source snapshot and still evaluates rules
- `kanary.no_update(reason=..., metadata=...)` means the poll succeeded but there is no new snapshot; Kanary keeps the last snapshot and still evaluates rules against it
- `kanary.skip(reason=..., metadata=...)` means the poll should be ignored entirely; Kanary keeps the last snapshot and does not evaluate rules
- raising an exception means source/plugin failure and triggers the runtime retry/reinit policy

`kanary.SourceResult(...)` remains available as an advanced form.

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
- missing files or missing keys raise `RuntimeError`
- `kanary.load_json(...)` works the same way for JSON files

Using `Path(__file__).with_name(...)` directly is still fine when you want full manual control.
These local config files are not part of auto-reload detection, so after editing them you should run an explicit
`kanaryctl reload ...`.

## 2. Rule

### Minimum Interface

Required:

- `rule_id`
- `inputs`
- `severity`
- `tags`
- `evaluate(ctx)`

`source="postgres"` remains available as a shorthand for `inputs="postgres:*"` when you want to depend on everything exposed by one source.

`inputs` may be:

- one exact input, such as `inputs="postgres:temperature"`
- a list, such as `inputs=["primary:temperature", "secondary:temperature"]`
- a glob on the source side, the input side, or both, such as `inputs="postgres:*"` or `inputs="kernel_*:temperature"`

Internally, `inputs="postgres:temperature"` is normalized to `["postgres:temperature"]`.
Kanary resolves `resolved_sources` from these selectors at load/reload time and reevaluates the rule whenever one of those sources updates.

`severity` is required. It acts as the default or fallback severity.
If you return `kanary.firing(..., severity=...)` or `kanary.Evaluation(severity=...)`, that specific evaluation overrides the class-level severity.
The older `evaluate(payload, ctx)` signature still works for compatibility, but lint warns and the formal API is `evaluate(ctx)`.

Optional metadata:

- `owner`
- `description`
- `runbook`

These appear in the alert API and in the viewer detail panel.

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
If you need the normalized payload for the source that triggered the current evaluation, use `ctx.source_payload()`. Use `ctx.inputs()` for cross-source data access.

### Returning evaluations

The usual public API is one of:

- `kanary.ok(message, extra=...)`
- `kanary.firing(message, severity=..., extra=...)`
- `kanary.warn(...)`, `kanary.error(...)`, `kanary.critical(...)`
- `kanary.fire_if(...)`, `kanary.warn_if(...)`, `kanary.error_if(...)`, `kanary.critical_if(...)`

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
- `severity` may be a constant such as `kanary.ERROR` or a string such as `"ERROR"`

If you do not specify a payload explicitly, Kanary automatically inherits the current source payload. Use `extra={...}` to merge additional fields into that payload.
`kanary.Evaluation(...)` remains available as an advanced form.

## 3. Output

### Minimum Interface

Required:

- `output_id`
- `emit(event)`

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

The built-in SMTP output is an exception to the "prefer local plugin config" style used by the examples in this repository.
It still reads `KANARY_SMTP_*` environment variables for convenience.
The older `emit(event, ctx)`, `init(ctx)`, and `terminate(ctx)` signatures still work for compatibility, but lint warns and the formal API omits `ctx`.

`include_tags` and `exclude_tags` support glob patterns.  
For example, `include_tags=["expert_*"]` matches tags such as `expert_db` and `expert_shift`.

`exclude_states` starts from "allow all states" and removes the listed ones.  
`exclude_transitions` also starts empty. 

Common values for `exclude_states`:

- `OK`
  Recovery events.
- `FIRING`
  Active alert events.
- `ACKED`
  Operator acknowledgement events (`FIRING -> ACKED`).
- `SILENCED`
  A firing alert that is currently covered by an active silence.
- `SUPPRESSED`
  A firing alert suppressed by another rule via `suppressed_by`.

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
    exclude_states=["SUPPRESSED"],
    minimum_severity="ERROR",
)
class DiscordOutput:
    def emit(self, event):
        ...
```

Failure recovery defaults:

- `max_retry = 1`
- `max_reinit = 1`

If `emit()` raises, Kanary retries delivery in-process before leaving the output in `FAILED`.
Attempt `N` waits `N**2` seconds first. With the defaults, Kanary:

1. waits 1 second and retries `emit()`
2. waits 4 seconds, runs `terminate() -> init()`, and retries `emit()`

If all attempts fail, the output remains `FAILED` until the next alert event or an explicit reload.

## 4. Built-In Helper Classes

### Source Helpers

#### BufferedSource

`kanary.BufferedSource` keeps a short in-memory history inside the source plugin.
Implement `fetch(self)` and return normal source data from there. `BufferedSource.poll()` records that result automatically.

Available helpers:

- `history()`
- `latest()`
- `average_value()`
- `min_value()`
- `max_value()`
- `count()`
- `rate()`

### Rule Helpers

#### RangeRule

- single-range rule
- single severity
- `lower_inclusive` and `upper_inclusive` define `[]` vs `()`
- `hysteresis` shifts the clear boundary slightly after a firing condition
- when multiple inputs are matched, each input is evaluated independently and any out-of-range input fires the rule

#### StaleRule

- checks measurement age via its timestamp
- when multiple inputs are matched, any stale or timestamp-missing input fires the rule

#### RateRule

- computes a rate from current and previous snapshots and evaluates it as a range
- when multiple inputs are matched, each input rate is evaluated independently

#### ThresholdRule

- multi-level severity
- `direction = "high" | "low"`
- `thresholds = [(value, severity), ...]`
- `hysteresis` adds a return margin when severity drops
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
- returns each remote alert as a measurement-like input
- can forward `ack`, `unack`, `silence`, and `unsilence` to the remote API
- typically configured with `base_url` and `interval`
- uses the hostname as the default node ID
- skips imported alerts when the local node ID is already present in `mirror_path`

#### RemoteAlarm

- mirrors one remote alert into one local rule via `remote_alarm_id`
- preserves remote state and severity locally
- can forward local operator actions when `propagate_ack` or `propagate_silence` is enabled
- carries `origin_node_id`, `origin_rule_id`, and `mirror_path` in the payload

#### import_remote_alarms

- factory that generates multiple `RemoteAlarm` rules
- supports `prefix`, `suffix`, `add_tags`, `include_rule_ids`, `exclude_rule_ids`, `include_tags`, and `exclude_tags`
- `include_rule_ids`, `exclude_rule_ids`, `include_tags`, and `exclude_tags` support glob patterns
- each generated rule is treated as an independent local rule

### Output Helpers

#### MailOutput

- sends email through SMTP
- typically configured with `smtp_host`, `sender`, and `recipients`
- if these are not defined as class attributes, the following environment variables are used:
  - `KANARY_SMTP_HOST`
  - `KANARY_SMTP_PORT`
  - `KANARY_SMTP_USER`
  - `KANARY_SMTP_PASSWORD`
  - `KANARY_SMTP_SENDER`
  - `KANARY_SMTP_RECIPIENTS`

Example:

```python
@kanary.output(output_id="mail")
class MailAlert(kanary.MailOutput):
    sender = "kanary@example.com"
    recipients = ["operator@example.com"]
    subject_prefix = "[KANARY production]"

    def _body(self, event):
        lines = [
            f"Rule: {event.rule_id}",
            f"Occurred At: {event.occurred_at.isoformat()}",
            f"Previous State: {event.previous_state.value if event.previous_state is not None else '-'}",
            f"State: {event.current_state.value}",
            (
                "Previous Severity: "
                f"{kanary.severity_label(event.previous_severity) if event.previous_severity is not None else '-'}"
            ),
            f"Severity: {kanary.severity_label(event.current_severity)}",
            f"Transition: {event.transition.value if event.transition else '-'}",
            f"Owner: {event.owner or '-'}",
            f"Tags: {', '.join(event.tags) if event.tags else '-'}",
            f"Message: {event.message or '-'}",
        ]
        return "\n".join(lines)
```

## 5. User-Defined Factories

Kanary does not require built-in factories for every repeated pattern.
If you prefer, you can write your own factory functions in plain Python and generate plugin classes yourself.

The natural pattern is:

1. build a class dynamically with `type(...)`
2. fill in the class attributes or methods you need
3. apply `kanary.source(...)`, `kanary.rule(...)`, or `kanary.output(...)` to register it

This keeps the generated plugins as normal, independent plugins after registration.

For example:

- generate one source from a measurement mapping
- generate several `ThresholdRule` classes from a list of measurements

See [examples/factory_patterns.py](../examples/factory_patterns.py) for a concrete example.

That example includes:

- `make_constant_source(...)`
  Generates a simple source class from a measurement dictionary.

## 6. Self-Monitoring Pattern

Kanary can also monitor its own runtime through the HTTP API.
One practical pattern is:

- a `Source` that reads `GET /plugins` from the local Kanary node
- one or more `Rule` classes that turn failed source/rule/output plugins into ordinary alerts

See [examples/self_plugin_monitoring.py](../examples/self_plugin_monitoring.py) for a compact example.
That example keeps the rule IDs coarse, such as `kanary.source.failure`, and puts the concrete failure summary into the alert message and metadata.
- `make_threshold_rule(...)`
  Generates one `ThresholdRule`-based rule class.

This approach is often enough when only one project needs the factory. If the pattern becomes common across multiple deployments, that is the point where adding a built-in helper may make sense.

## States And Dependencies

Rule relationships:

- `depends_on`
- `suppressed_by`

`depends_on` expresses a prerequisite for meaningful evaluation. For example, you might only evaluate an instrument timeout while a network rule is healthy.

`suppressed_by` is for automatic alert suppression during higher-level failures. For example, if `database.connection.failed` is firing, dependent stale alerts can become `SUPPRESSED`.

Alert states:

- `OK`
  The rule currently evaluates as healthy.
- `FIRING`
  The rule currently evaluates as abnormal.
- `ACKED`
  The alert is still abnormal, but an operator acknowledged it.
- `SILENCED`
  The alert would be firing, but an active silence currently masks it.
- `SUPPRESSED`
  The alert would be firing, but another rule listed in `suppressed_by` is active.

Derived transitions:

- `UNACK`
  Emitted when an acknowledged alert is reopened (`ACKED -> FIRING`).
- `ESCALATED`
  Emitted when severity rises while the state stays the same.
- `DEESCALATED`
  Emitted when severity drops while the state stays the same.

In practice:

- `SILENCED` is useful if you want outputs or viewers to show that an alert is muted on purpose.
- Rule removals during reload are not represented as an alert state. They are recorded in history as an operator action with `action_type = "rule_removed"`.
