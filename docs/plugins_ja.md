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

前回pollingした値を取得する際は, `previous=True`を引数に追加してください.

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

`include_tags` と `exclude_tags` は glob pattern を使えます。  
たとえば `include_tags=["expert_*"]` とすると、`expert_db` や `expert_shift` のような tag に一致します。

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

#### RemoteKanarySource

- 他の Kanary の `/export-alerts` を読む
- remote 側の `ack`, `unack`, `silence`, `unsilence` を helper として転送できる

#### RemoteAlarm

- 1 個の remote alert を local rule に mirror する
- `propagate_ack`, `propagate_silence` により operator action を remote へ転送できる

#### import_remote_alarms

- 複数の `RemoteAlarm` rule をまとめて生成する factory
- `prefix`, `suffix`, `add_tags`, `include_rule_ids`, `exclude_rule_ids`, `include_tags`, `exclude_tags` を使える
- `include_rule_ids`, `exclude_rule_ids`, `include_tags`, `exclude_tags` は glob pattern を使える

### Output 側

#### MailOutput

- SMTP でメールを送る helper class
- `smtp_host`, `sender`, `recipients` を class 属性か環境変数で指定する

## 5. user-defined factory

Kanary では、繰り返しパターンごとに本体側へ built-in factory を追加しなくても、ユーザーが plain Python で factory 関数を書けます。

自然な書き方は次です。

1. `type(...)` で class を動的に作る
2. 必要な class 変数や method を入れる
3. `kanary.source(...)`, `kanary.rule(...)`, `kanary.output(...)` を適用して登録する

こうして生成した plugin は、登録後は普通の独立した plugin として扱われます。

たとえば:

- measurement の dict から 1 個の source を生成する
- measurement の list から複数の `ThresholdRule` を生成する

といった使い方ができます。

具体例は [examples/factory_patterns.py](../examples/factory_patterns.py) にあります。

この example には次が入っています。

- `make_constant_source(...)`
  measurement の dict から単純な source class を生成します。
- `make_threshold_rule(...)`
  `ThresholdRule` ベースの rule class を 1 つ生成します。

ある project の中だけで使う繰り返しなら、この方法で十分なことが多いです。複数 deployment で同じパターンが繰り返し必要になった時点で、built-in helper を追加するかを検討するのが自然です。

## 状態と依存関係

rule 間関係:

- `depends_on`
- `suppressed_by`

`depends_on` は上位のルールが守られていない時, そもそも評価されません.
`suppressed_by` は評価されますが, アラームは`SUPRESSED`状態になるので, これらが通知されないようなOutputを書くことが可能です.

alert state:

- `OK`
- `FIRING`
- `ACKED`
- `SILENCED`
- `SUPPRESSED`
- `RESOLVED`
