# Plugin Model

This document first explains the minimum interface a user needs to implement, and then the built-in helper classes.

## 1. Source

### Minimum Interface

Required:

- `source_id`
- `poll(ctx) -> kanary.SourceResult`

Optional:

- `interval`
- `init(ctx)`
- `terminate(ctx)`

`interval` is optional. If you do not specify it, the default is `60.0` seconds.

Example:

```python
@kanary.source(source_id="sqlite", interval=5.0)
class SqliteSource:
    def poll(self, ctx):
        ...
```

### SourceResult

`SourceResult` can return multiple `Measurement` objects.

```python
kanary.SourceResult(
    measurements=[
        kanary.Measurement(name="temperature", value=..., timestamp=...),
        kanary.Measurement(name="humidity", value=..., timestamp=...),
    ],
    status="ok",
)
```

## 2. Rule

### Minimum Interface

Required:

- `rule_id`
- `source`
- `severity`
- `tags`
- `evaluate(payload, ctx) -> kanary.Evaluation`

`severity` is required. It acts as the default or fallback severity.
If you return `kanary.Evaluation(severity=...)`, that specific evaluation overrides the class-level severity.

Optional metadata:

- `owner`
- `description`
- `runbook`

These appear in the alert API and in the viewer detail panel.

### RuleContext

High-level accessors:

- `ctx.measurement(name)`
- `ctx.value(name)`
- `ctx.timestamp(name)`
- `ctx.metadata(name)`

If you need to access the previous polled value, add `previous=True` in the argument.

Low-level accessors:

- `ctx.get_current(path)`
- `ctx.get_previous(path)`

## 3. Output

### Minimum Interface

Required:

- `output_id`
- `emit(event, ctx)`

Optional:

- `init(ctx)`
- `terminate(ctx)`
- `include_tags`
- `exclude_tags`
- `include_states`
- `exclude_states`

`include_tags` and `exclude_tags` support glob patterns.  
For example, `include_tags=["expert_*"]` matches tags such as `expert_db` and `expert_shift`.

Example:

```python
@kanary.output(output_id="discord", include_tags=["sqlite"])
class DiscordOutput:
    def emit(self, event, ctx):
        ...
```

## 4. Built-In Helper Classes

### Source Helpers

#### BufferedSource

`kanary.BufferedSource` keeps a short in-memory history inside the source plugin.

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

#### StaleRule

- checks measurement age via its timestamp

#### RateRule

- computes a rate from current and previous snapshots and evaluates it as a range

#### ThresholdRule

- multi-level severity
- `direction = "high" | "low"`
- `thresholds = [(value, severity), ...]`
- `hysteresis` adds a return margin when severity drops

Example:

```python
@kanary.rule(
    rule_id="sqlite.value1.threshold",
    source="sqlite",
    severity=kanary.WARN,
    tags=["sqlite", "value1"],
)
class Value1Threshold(kanary.ThresholdRule):
    measurement = "value1"
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
- `FIRING`
- `ACKED`
- `SILENCED`
- `SUPPRESSED`
- `RESOLVED`
