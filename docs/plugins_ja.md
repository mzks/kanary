# Plugin Model

この文書では、まずユーザーが満たすべき最小 interface を説明し、その後に組み込み helper class を説明します。

## 1. Source

### 最小 interface

必須:

- `source_id`
- `poll(ctx) -> kanary.SourceResult`

任意:

- `interval`
- `init(ctx)`
- `terminate(ctx)`

`interval` は任意で、既定値は `60.0` 秒です。

例:

```python
@kanary.source(source_id="sqlite", interval=5.0)
class SqliteSource:
    def poll(self, ctx):
        ...
```

### SourceResult

`SourceResult` では複数の `Measurement` を返せます。

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

### 最小 interface

必須:

- `rule_id`
- `source`
- `severity`
- `tags`
- `evaluate(payload, ctx) -> kanary.Evaluation`

任意 metadata:

- `owner`
- `description`
- `runbook`

`severity` は default / fallback severity として使われます。  
`kanary.Evaluation(severity=...)` を返すと、その評価だけ上書きできます。

### RuleContext

高レベル accessor:

- `ctx.measurement(name)`
- `ctx.value(name)`
- `ctx.timestamp(name)`
- `ctx.metadata(name)`

低レベル accessor:

- `ctx.get_current(path)`
- `ctx.get_previous(path)`

## 3. Output

### 最小 interface

必須:

- `output_id`
- `emit(event, ctx)`

任意:

- `init(ctx)`
- `terminate(ctx)`
- `include_tags`
- `exclude_tags`
- `include_states`
- `exclude_states`

例:

```python
@kanary.output(output_id="discord", include_tags=["sqlite"])
class DiscordOutput:
    def emit(self, event, ctx):
        ...
```

## 4. 組み込み helper class

### Source 側

#### BufferedSource

`kanary.BufferedSource` は source 側で短い履歴を持つ helper です。

使える helper:

- `history()`
- `latest()`
- `average_value()`
- `min_value()`
- `max_value()`
- `count()`
- `rate()`

### Rule 側

#### RangeRule

- 単一範囲
- 単一 severity
- `lower_inclusive` / `upper_inclusive`
- `hysteresis`

#### StaleRule

- measurement の timestamp の古さを判定

#### RateRule

- current / previous から rate を計算して範囲評価

#### ThresholdRule

- 多段階 severity
- `direction = "high" | "low"`
- `thresholds = [(value, severity), ...]`
- `hysteresis`

#### RemoteKanarySource

- 他の Kanary の `/export-alerts` を読む
- remote 側の `ack`, `unack`, `silence`, `unsilence` を helper として転送できる

#### RemoteAlarm

- 1 個の remote alert を local rule に mirror する
- `propagate_ack`, `propagate_silence` により operator action を remote へ転送できる

#### import_remote_alarms

- 複数の `RemoteAlarm` rule をまとめて生成する factory
- `prefix`, `suffix`, `add_tags`, `include_rule_ids`, `exclude_rule_ids`, `include_tags`, `exclude_tags` を使える

### Output 側

#### MailOutput

- SMTP でメールを送る helper class
- `smtp_host`, `sender`, `recipients` を class 属性か環境変数で指定する

## 状態と依存関係

rule 間関係:

- `depends_on`
- `suppressed_by`

alert state:

- `OK`
- `FIRING`
- `ACKED`
- `SILENCED`
- `SUPPRESSED`
- `RESOLVED`
