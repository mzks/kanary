# Plugin Model

この文書では、まずユーザーが満たすべき最小 interface を説明し、その後に組み込み helper class を説明します。

## 1. Source

### 最小 interface

最低限必要なもの:

- `source_id`
- `poll(ctx) -> kanary.SourceResult`

任意:

- `interval`
- `init(ctx)`
- `terminate(ctx)`

`interval` は任意です。指定しない場合は既定で `60.0` 秒です。

例:

```python
@kanary.source(source_id="sqlite", interval=5.0)
class SqliteSource:
    def poll(self, ctx):
        ...
```

### SourceResult

`SourceResult` は複数の `Measurement` を返せます。

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

最低限必要なもの:

- `rule_id`
- `source`
- `severity`
- `tags`
- `evaluate(payload, ctx) -> kanary.Evaluation`

`severity` は必須です。default / fallback として使われます。  
`kanary.Evaluation(severity=...)` を返せば、その評価だけ override できます。

### RuleContext

高レベル accessor:

- `ctx.measurement(name)`
- `ctx.value(name)`
- `ctx.timestamp(name)`
- `ctx.metadata(name)`

低レベル accessor:

- `ctx.get_current(path)`
- `ctx.get_previous(path)`

### Rule ID

基本形:

```text
<source_id>.<variable>.<rule_type>
```

例:

- `sqlite.value1.stale`
- `postgres.temperature.range`

## 3. Output

### 最小 interface

最低限必要なもの:

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

最小 interface を自前で実装する代わりに、組み込み helper class を使えます。

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
- `lower_inclusive` / `upper_inclusive` で `[]` / `()` を明示
- `hysteresis` を指定すると、いったん発報したあとに解除境界を少しずらせます

#### StaleRule

- measurement の timestamp の古さを判定

#### RateRule

- current / previous から rate を計算して範囲評価

#### ThresholdRule

- 多段階 severity
- `direction = "high" | "low"`
- `thresholds = [(value, severity), ...]`
- `hysteresis` を指定すると、severity が下がるときに戻り幅を持たせられます

例:

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

`RangeRule` と `ThresholdRule` の `hysteresis` は組み込み helper class のための簡潔な実装です。  
low 側と high 側で別の戻り幅が欲しい場合や、もっと特殊な解除条件が欲しい場合は、custom rule として実装します。

#### RemoteKanarySource

- 他の KANARY の `/alerts` を読みます
- 各 remote alert を measurement として返します
- `ack`, `unack`, `silence`, `unsilence` を remote API に転送する helper も持ちます
- 通常は `base_url` と `interval` を設定して使います

#### RemoteAlarm

- `remote_alarm_id` で 1 個の remote alert を local rule に mirror します
- remote の state と severity を local alert に写します
- `propagate_ack`, `propagate_silence` を `True` にすると、local operator action を remote に転送できます

#### import_remote_alarms

- 複数の `RemoteAlarm` rule をまとめて生成する factory です
- `prefix`, `suffix`, `add_tags`, `include_rule_ids`, `exclude_rule_ids`, `include_tags`, `exclude_tags` を使えます
- factory が生成した rule も、それぞれ独立した rule として扱われます

## 状態と依存関係

rule 間関係:

- `depends_on`
- `suppressed_by`

`depends_on` は、その rule が有効に評価されるための前提条件です。  
たとえば「network が正常なときだけ instrument の timeout を評価したい」という場合に使います。

`suppressed_by` は、上位障害が起きている間は下位の alert を自動的に抑制したい場合に使います。  
たとえば「database connection failed が出ている間は、その database にぶら下がる stale alert を `SUPPRESSED` にしたい」という場合に使います。

alert state:

- `OK`
- `FIRING`
- `ACKED`
- `SILENCED`
- `SUPPRESSED`
- `RESOLVED`

意味:

- `SILENCED`: operator action
- `SUPPRESSED`: rule dependency による自動抑制
