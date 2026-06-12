# Plugin Model

この文書では、まずユーザーが満たすべき最小 interface を説明し、その後に組み込み helper class を説明します。

## 1. Source

### 最小 interface

必須:

- `source_id`
- `poll()`

任意:

- `interval`
- `schedule`
- `init()`
- `terminate()`
- `max_retry`
- `max_reinit`

`interval` と `schedule` を両方省略した場合は、Kanary は
`interval = 60.0` を使います。  
`schedule` を使う場合は `interval` を同時に指定しないでください。
旧来の `poll(ctx)`, `init(ctx)`, `terminate(ctx)` も互換のため引き続き動きますが、lint では warning を出し、文書上の正式 API は引数なしの形に統一します。

`interval` は秒単位の polling 間隔です。  
`schedule` は Unix cron 互換の 5-field 文字列で、Kanary server の local time
で解釈されます。`@hourly`, `@daily`, `@weekly`, `@monthly`, `@yearly` のような
少数の macro も使えます。

例:

- `interval = 60.0`
- `schedule = "*/5 * * * *"`
- `schedule = "@hourly"`

例:

```python
@kanary.source(source_id="sqlite", interval=5.0)
class SqliteSource:
    def poll(self):
        ...
```

実行失敗からの復帰の default:

- `max_retry = 1`
- `max_reinit = 1`

`poll()` が例外を送出した場合、Kanary は即 failed に固定せず、その場で再試行します。  
N 回目の復帰試行の前には `N**2` 秒待ちます。default では次の順です。

1. 1 秒待って `poll()` を再試行
2. 4 秒待って `terminate() -> init()` を行い、その後 `poll()` を再試行

それでも失敗した場合は source は `FAILED` のままになり、次の定期 poll または明示 reload まで復帰しません。

### input を返す

通常の public API は `kanary.inputs(...)` です。

tuple/list 形式:

```python
return kanary.inputs(
    [
        ("temperature", 23.4, observed_at, {"unit": "C"}),
        ("humidity", 40.0),
    ],
    metadata={"table": "env_samples_wide"},
)
```

dict 形式も使えます:

```python
return kanary.inputs(
    {
        "temperature": (23.4, observed_at, {"unit": "C"}),
        "humidity": 40.0,
    }
)
```

規則:

- `(name, value)`, `(name, value, timestamp)`, `(name, value, timestamp, metadata)` を受け付けます
- item 側の timestamp が省略または `None` の場合、outer `timestamp=` があればそれを使い、無ければ Kanary server の current time を使います
- outer `metadata=` は `SourceResult.metadata` になります
- `kanary.no_data(reason=..., metadata=...)` は poll 自体は成功したが usable な input が 0 件だったことを表します
- 例外を送出した場合は source/plugin failure として扱われ、runtime の retry/reinit policy に入ります

`kanary.SourceResult(...)` も advanced な書き方として引き続き使えます。

plugin は、site-specific な設定が必要な場合に自分の directory 内の local file を自由に読んで構いません。  
よくある形は、plugin script の隣に `*_config.toml` を置き、`Path(__file__).with_name(...)` で読むやり方です。  
これらの local config file は auto-reload の検知対象ではないため、変更後は `kanaryctl reload ...` を明示的に実行してください。

## 2. Rule

### 最小 interface

必須:

- `rule_id`
- `inputs`
- `severity`
- `tags`
- `evaluate(ctx)`

`source="postgres"` も、`inputs="postgres:*"` の短縮として引き続き使えます。1つの source が公開する全 input に依存したい時の sugar です。

`inputs` には次を指定できます。

- 1 個の exact input
  例: `inputs="postgres:temperature"`
- list
  例: `inputs=["primary:temperature", "secondary:temperature"]`
- source 側、input 側、または両側の glob
  例: `inputs="postgres:*"`, `inputs="kernel_*:temperature"`

内部では `inputs="postgres:temperature"` も `["postgres:temperature"]` に正規化されます。  
Kanary は load/reload 時にこれらの selector から `resolved_sources` を計算し、その source のどれかが更新されるたびに rule を再評価します。

任意 metadata:

- `owner`
- `description`
- `runbook`

`severity` は default / fallback severity として使われます。  
`kanary.firing(..., severity=...)` または `kanary.Evaluation(severity=...)` を返すと、その評価だけ上書きできます。
旧来の `evaluate(payload, ctx)` も互換のため引き続き動きますが、lint では warning を出し、正式 API は `evaluate(ctx)` です。

### RuleContext

input を扱う accessor を使います:

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

`ctx.inputs()` は fully-qualified input name の昇順で `InputView` を返し、複数 selector に同じ input が当たっても重複を除去します。

各 `InputView` が持つもの:

- `name`
- `source_id`
- `input_name`
- `value`
- `timestamp`
- `metadata`

単一 input に解決される rule では `ctx.value()` のように selector を省略できます。複数 input を扱う rule では通常 `ctx.inputs()` を反復します。  
selector が複数 input に一致しうる場合、`ctx.value()` などの scalar helper は推測せず error にします。  
今回の評価を trigger した source の normalized payload が必要な時は `ctx.source_payload()` を使います。cross-source の参照は `ctx.inputs()` を使ってください。

### 評価結果を返す

通常の public API は次のどれかです。

- `kanary.ok(message, extra=...)`
- `kanary.firing(message, severity=..., extra=...)`
- `kanary.warn(...)`, `kanary.error(...)`, `kanary.critical(...)`
- `kanary.fire_if(...)`, `kanary.warn_if(...)`, `kanary.error_if(...)`, `kanary.critical_if(...)`

例:

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

短縮形として次も受け付けます:

- `None` は `OK`
- `True` は `FIRING`, `False` は `OK`
- `(severity, message)` はその severity で `FIRING`
- `(None, message)` は `OK`
- `severity` には `kanary.ERROR` のような定数だけでなく `"ERROR"` のような文字列も使えます

payload を明示しない場合、Kanary は current source payload を自動で引き継ぎます。追加情報を入れたい時は `extra={...}` を使って merge します。  
`kanary.Evaluation(...)` も advanced な書き方として引き続き使えます。

## 3. Output

### 最小 interface

必須:

- `output_id`
- `emit(event)`

任意:

- `init()`
- `terminate()`
- `include_tags`
- `exclude_tags`
- `exclude_states`
- `exclude_transitions`
- `minimum_severity`
- `max_retry`
- `max_reinit`

この repository の example が主に使う「local plugin config を読む」流儀に対して、組み込み SMTP output は例外です。  
SMTP output は利便性のために、引き続き `KANARY_SMTP_*` 環境変数を読みます。
旧来の `emit(event, ctx)`, `init(ctx)`, `terminate(ctx)` も互換のため引き続き動きますが、lint では warning を出し、正式 API では `ctx` を省きます。

`include_tags` と `exclude_tags` は glob pattern を使えます。  
たとえば `include_tags=["expert_*"]` とすると、`expert_db` や `expert_shift` のような tag に一致します。

`exclude_states` は「全 state を許可してから除外する」設定です。  
`exclude_transitions` も default は空です。severity 低下を通知したくないなら `DEESCALATED` を明示的に追加します。

`exclude_states` によく入る値:

- `OK`
  復旧通知。
- `FIRING`
  異常発火中の通知。
- `ACKED`
  operator が確認したことを示す通知 (`FIRING -> ACKED`)。
- `SILENCED`
  active な silence に覆われている `FIRING`。
- `SUPPRESSED`
  `suppressed_by` により別 rule に抑制されている `FIRING`。

`exclude_transitions` に入る値:

- `UNACK`
  `ACKED -> FIRING` を表す派生 transition。
- `ESCALATED`
  `FIRING(WARN) -> FIRING(ERROR)` のような、同じ state の severity 上昇。
- `DEESCALATED`
  `FIRING(CRITICAL) -> FIRING(ERROR)` のような、同じ state の severity 低下。

`emit()` に渡される `event` には次が入ります。

- `previous_state`
- `current_state`
- `previous_severity`
- `current_severity`
- `transition`

通常の state change では `transition` は `None` です。派生 transition の場合は `UNACK`, `ESCALATED`, `DEESCALATED` のいずれかになります。

例:

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

実行失敗からの復帰の default:

- `max_retry = 1`
- `max_reinit = 1`

`emit()` が例外を送出した場合、Kanary は即 failed に固定せず、その場で再試行します。  
N 回目の復帰試行の前には `N**2` 秒待ちます。default では次の順です。

1. 1 秒待って `emit()` を再試行
2. 4 秒待って `terminate() -> init()` を行い、その後 `emit()` を再試行

それでも失敗した場合は output は `FAILED` のままになり、次の alert event または明示 reload まで復帰しません。

## 4. 組み込み helper class

### Source 側

#### BufferedSource

`kanary.BufferedSource` は source 側で短い履歴を持つ helper です。
`fetch(self)` を実装し、そこから通常の source data を返します。`BufferedSource.poll()` がその結果を自動で記録します。

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
- 複数 input に一致した場合は各 input を独立に評価し、どれか 1 つでも範囲外なら `FIRING`

#### StaleRule

- measurement の timestamp の古さを判定
- 複数 input に一致した場合は、どれか 1 つでも stale または timestamp 欠落なら `FIRING`

#### RateRule

- current / previous から rate を計算して範囲評価
- 複数 input に一致した場合は各 input の rate を独立に評価

#### ThresholdRule

- 多段階 severity
- `direction = "high" | "low"`
- `thresholds = [(value, severity), ...]`
- `hysteresis`
- 複数 input に一致した場合は、どれか 1 つでも threshold に一致すれば `FIRING` し、その中の最高 severity を採用

例:

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

例:

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

## 6. self-monitoring pattern

Kanary は自分自身の runtime を HTTP API 経由で監視することもできます。
自然な形は次です。

- local Kanary の `GET /plugins` を読む `Source`
- failed な source / rule / output plugin を普通の alert に変換する `Rule`

具体例は [examples/self_plugin_monitoring.py](../examples/self_plugin_monitoring.py) にあります。
この example では `kanary.source.failure` のように rule_id は粗く保ち、実際に何が失敗しているかは alert の message と metadata に寄せています。

ある project の中だけで使う繰り返しなら、この方法で十分なことが多いです。複数 deployment で同じパターンが繰り返し必要になった時点で、built-in helper を追加するかを検討するのが自然です。

## 状態と依存関係

rule 間関係:

- `depends_on`
- `suppressed_by`

`depends_on` は上位のルールが守られていない時, そもそも評価されません.
`suppressed_by` は評価されますが, アラームは`SUPRESSED`状態になるので, これらが通知されないようなOutputを書くことが可能です.

alert state:

- `OK`
  現在の評価結果が正常。
- `FIRING`
  現在の評価結果が異常。
- `ACKED`
  異常は継続しているが、operator が確認済み。
- `SILENCED`
  本来は `FIRING` だが、active な silence により一時的に mute されている。
- `SUPPRESSED`
  本来は `FIRING` だが、`suppressed_by` にある別 rule が active なため抑制されている。

派生 transition:

- `UNACK`
  acknowledged された alert を再オープンした時 (`ACKED -> FIRING`)。
- `ESCALATED`
  state を変えずに severity が上がった時。
- `DEESCALATED`
  state を変えずに severity が下がった時。

実運用では:

- `SILENCED` は「意図的に止めている」ことを viewer や output で示したい時に役立ちます。
- reload 中の rule removal は alert state では表しません。履歴上は `action_type = "rule_removed"` の operator action として残ります。
