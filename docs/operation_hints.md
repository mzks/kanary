# Operational Hints

This document supplements the formal API with guidance for adding plugins safely and avoiding common operational problems when using Kanary as an alerting and notification system. See [plugins.md](plugins.md) for the complete API and [operations.md](operations.md) for operational commands.

## Test Environments

Plugins are loaded and run independently by Source, Rule, and Output. A failure in one plugin does not immediately stop the others. Plugins can still interfere with each other when they share an external API, state DB, notification destination, or file. In particular, Kanary cannot prevent a badly configured Rule from firing at a short interval and overloading the people receiving its notifications.

First, verify that the plugin directory can be loaded.

```bash
kanary lint ./plugins
```

`lint ok` means that Kanary could import the directory and inspect its definitions. It does not guarantee every behavior, including external connectivity, notification content, thresholds, or decorator typos. Importing a plugin module executes arbitrary Python code, so keep external connections and writes in `init()`, `poll()`, or `emit()`, rather than at module top level.

To verify routing with the same plugins and inputs as production, start a shadow instance on a different port with a different node ID.

```bash
kanary ./plugins \
  --api-port 8001 \
  --node-id production-shadow \
  --no-output-emit
```

With `--no-output-emit`, Output plugins are loaded and initialized and their routing filters are evaluated, but `emit()` is not called. Source polling, Rule evaluation, state changes, and operator actions continue normally. Do not share a state DB with production when running alongside it. Use a separate `--state-db` path, or run without a state DB and without `KANARY_SQLITE_PATH` set.

Use `kanaryctl` to check individual pieces.

```bash
kanaryctl test-poll source_id
kanaryctl test-evaluate rule_id --print-template
kanaryctl test-fire rule_id --state FIRING --reason "output check"
```

`test-fire` sends a synthetic event through Output routing. Against the production API, ordinary Outputs will deliver real notifications, so use it carefully.

## Time Units in Plugins

Kanary represents durations as floating-point seconds. Unit constants make configuration values easier to read.

```python
import kanary
from kanary import minute as m


@kanary.source(source_id="example", interval=10 * m)
class ExampleSource:
    ...
```

`nanosecond`, `microsecond`, `millisecond`, `second`, `minute`, `hour`, and `day` are available.

## Configuration and Lifecycle

Keep environment-specific settings such as endpoints, timeouts, and notification destinations outside Python source. Use `kanary.load_toml()`, environment variables, or a systemd `EnvironmentFile`. Do not commit tokens or passwords to a repository. Non-Python configuration files are not watched by auto-reload, so an explicit reload or process restart is required after changing them.

For Sources and Outputs, create connections and threads in `init()` and close them in `terminate()`. Caches and timers stored in a custom Rule's `__init__()` or in a Source or Output instance's `self` are lost on process restart and plugin reload. Store operational state explicitly in an external store when it must persist.

## Monitoring Kanary Itself

One Kanary node can monitor a peer node's health. [peer_monitoring.py](../examples/peer_monitoring.py) shows how to expose a peer's heartbeat, API latency, and failed-plugin count as ordinary inputs.

A practical arrangement is for the primary node to own business Sources, Rules, and Outputs, while the monitoring node has only a small set of peer-monitoring Rules and their notification Outputs. When monitoring peers in both directions, verify that the monitoring path does not depend on the same network, power, or notification destination. Kanary does not provide automatic peer redundancy or consensus.

Use `RemoteKanarySource` and `RemoteAlarm` when another node's alerts themselves need to become local Rules. Keep this distinct from peer monitoring, where transport failure is treated as a Source failure, so that causes remain easy to diagnose.

## Sources

### Returning Inputs

Normally, return `kanary.inputs(...)`. Explicit input names, values, and observation timestamps let StaleRule and history behave correctly.

```python
def poll(self):
    return kanary.inputs(
        ("temperature", 23.4, measured_at),
        ("fan_rpm", 1200, measured_at, {"unit": "rpm"}),
    )
```

`dict[name, value]` and tuple/list input forms are also supported, but `kanary.inputs(...)` is more explicit when timestamps, metadata, and the meaning of empty data matter.

- `kanary.no_data(reason=...)`: the poll succeeded but returned no data.
- `kanary.no_update(reason=...)`: retain the previous values.
- `kanary.skip(reason=...)`: do not evaluate this poll.

For temporary external API communication failures, normally raise an exception. Kanary handles Source retry / reinitialization and plugin status. Turning failures into `no_data()` loses the distinction between a communication failure and a valid empty result.

### External I/O, Timestamps, and Poll Intervals

Always set timeouts for external I/O such as HTTP, database, and socket operations. Without one, a poll can block indefinitely and delay the next evaluation.

When possible, set `Measurement.timestamp` to the time when the monitored system actually observed the value, rather than the retrieval time. Using Kanary's poll time makes old data appear fresh, which makes StaleRule and rate calculations inaccurate. Use the poll time only when no observation time is available.

`interval` determines both the load on the external system and detection latency. With a Source that polls every minute, a Rule that requires a condition to persist for five minutes fires no earlier than five minutes later, and usually at the following poll. Represent monitored-system faults with ordinary inputs and Rules; represent an inability of the monitoring plugin itself to collect data as a Source failure by raising an exception.

## Rules

### How Much to Combine in One Rule

There is no requirement that one variable equals one Rule. Inputs with a shared owner, runbook, firing condition, and notification policy are often clearer in one custom Rule. Use separate Rules when their ACK, silence, suppression, or notification destinations must be independent.

For a custom Rule that handles multiple inputs, iterate over `ctx.inputs()` instead of making `ctx.value()` guess. `ctx.value()` raises an error when its selector matches more than one input.

### Owner, Tags, and Runbooks

`owner` identifies the person or team responsible for response, `runbook` identifies the response procedure, and tags are attributes for routing, searching, silencing, and classification. Keep their roles distinct, for example: `owner="expert_db"`, `tags=["database", "production"]`, and `runbook="https://..."`.

It is usually easier to maintain a small number of stable classification tags and combine them with an Output's `include_tags` / `exclude_tags` than to create unlimited tags for each notification destination. Keep individual contact details in Output configuration.

### Rule Hysteresis

`hysteresis` on `RangeRule` and `ThresholdRule` is a value margin that prevents repeated transitions between `FIRING` and `OK`, or repeated severity changes, near a boundary. It does not change the boundary that fires. Only after the alert becomes abnormal does it hold recovery or severity reduction until the value has moved `hysteresis` further into the normal range.

For example, a RangeRule with an upper bound of `20` and `hysteresis = 1` fires above `20`, then remains `FIRING` until the value is `19` or lower. This is not a time delay.

```python
class Temperature(kanary.RangeRule):
    high = 20
    hysteresis = 1
```

For asymmetric recovery boundaries or more complex recovery conditions, use the helper implementation as a reference and write a custom Rule.

### Time Conditions in a Custom Rule

Conditions such as "fire after five minutes of abnormality" or "recover after two minutes of normality" can currently be written with custom Rule instance state and `ctx.now`. Evaluation happens at the next poll, so a transition occurs on the first poll after the duration, not precisely at its boundary.

```python
class TemperatureForDuration:
    def __init__(self):
        self.bad_since = None
        self.good_since = None
        self.raw_firing = False

    def evaluate(self, ctx):
        value = ctx.value()
        is_bad = value is not None and value > 90

        if not self.raw_firing:
            if not is_bad:
                self.bad_since = None
                return kanary.ok()
            self.bad_since = self.bad_since or ctx.now
            if ctx.now - self.bad_since < 5 * kanary.minute:
                return kanary.ok("temperature is high; waiting for 5 minutes")
            self.raw_firing = True
            self.good_since = None
            return kanary.firing("temperature has been high for 5 minutes")

        if is_bad:
            self.good_since = None
            return kanary.firing("temperature is high")
        self.good_since = self.good_since or ctx.now
        if ctx.now - self.good_since < 2 * kanary.minute:
            return kanary.firing("temperature recovered; waiting for 2 minutes")

        self.raw_firing = False
        self.bad_since = None
        self.good_since = None
        return kanary.ok("temperature has been normal for 2 minutes")
```

Keep `raw_firing` separately because `ACKED`, `SILENCED`, and `SUPPRESSED` are states applied outside the rule's underlying input condition. This timer resets on process restart and plugin reload. There is no dedicated `PENDING` state while waiting; include the reason in an `OK` or `FIRING` message when needed.

## Outputs

### Narrowing State-Transition Notifications

By default, an Output has `exclude_states=["SUPPRESSED", "SILENCED"]`. It therefore normally receives `FIRING`, `OK`, and `ACKED`, including recovery events.

Because `SILENCED -> OK` has `OK` as its current state, it is not excluded by default. To skip only this transition, check it in `emit()`.

```python
def emit(self, event):
    if event.previous_state == kanary.SILENCED and event.current_state == kanary.OK:
        return
    # Deliver the event.
```

To exclude every recovery event, extend the defaults.

```python
exclude_states = kanary.Output.exclude_states + ["OK"]
```

Specifying `exclude_states` in a decorator replaces the defaults. Set `exclude_states=[]` explicitly for debug or audit Outputs that need every state.

### Separating Destinations by Severity

A Rule can override its decorator's default severity for each evaluation with `kanary.firing(..., severity=...)` and similar helpers. For example, separate Outputs can send WARN and above to the owner, ERROR and above to the on-call group, and CRITICAL to a mailing list.

```python
@kanary.output(output_id="on-call", minimum_severity="ERROR")
class OnCallOutput:
    def emit(self, event):
        ...
```

This is notification based on state and severity changes. It is separate from followups that widen the destination because nobody has responded over time.

### Delivery Retries and Duplicates

When `emit()` raises, Kanary retries according to the Output retry / reinitialization settings. An Output that creates tickets or writes to an external service must tolerate receiving the same event more than once. When the external API supports an idempotency key, derive a stable key from values such as `rule_id`, `occurred_at`, and state / transition.

Conversely, if an Output swallows a failure and returns normally, Kanary considers delivery successful. Raise an exception with enough context when a delivery failure should be retried. Preventing repeated notifications in a short interval is a combination of Rule hysteresis or time conditions and Output routing or followups.

### Notification Followups

`kanary.OutputFollowups` is an in-process helper that invokes another action when a fixed time has passed since the first notification without an ACK, silence, or recovery.

```python
import kanary
from kanary import hour as h


@kanary.output(
    output_id="operations-followups",
    minimum_severity="WARN",
    exclude_states=[],
)
class OperationsFollowupOutput:
    def init(self):
        self.followups = kanary.OutputFollowups()

    def terminate(self):
        self.followups.close()

    def emit(self, event):
        followups = self.followups.for_event(event)

        if event.current_state == kanary.FIRING and event.previous_state != kanary.FIRING:
            followups.now(self.report_to_expert)
            followups.after(1 * h, self.post_group_discord)
            followups.after(2 * h, self.post_mailing_list)
            return

        if event.current_state in {
            kanary.ACKED,
            kanary.SILENCED,
            kanary.OK,
            kanary.SUPPRESSED,
        }:
            followups.cancel()

        if event.transition == kanary.ESCALATED:
            followups.cancel()
            followups.now(self.post_mailing_list)
```

`now()` invokes its callback synchronously with the current event. Callback exceptions propagate to `emit()`, so normal Output retry / reinitialization applies. `after()` uses an absolute offset from the first followup registration, and its callback receives the most recently received event at execution time.

To cancel followups on `SILENCED` and `SUPPRESSED`, the Output must receive those events, hence `exclude_states=[]`. Followups are stored only in memory and are lost on process restart or plugin reload. Always call `close()` from `terminate()`. See [output_followups.py](../examples/output_followups.py) for the complete example.
