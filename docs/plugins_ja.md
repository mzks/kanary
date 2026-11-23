# Plugin Model

この文書では、まずユーザーが満たすべき最小 interface を説明し、その後に組み込み helper class を説明します。

動く雛形から始めるには `kanary new --list` で template を選び、`kanary new -t <template> <id>` を使います。詳しくは [Getting Started](getting_started_ja.md) を参照してください。`src/kanary/templates/` の template は各 plugin 種別の全 option の一覧にもなっています。

## Decorator 引数と class 属性

この文書に出てくる属性は、すべて decorator 引数と class 属性のどちらにも書けます。decorator は引数を class 属性としてそのまま設定するので、次の 2 つは同じ動作になります。

```python
@kanary.source(source_id="postgres.wide", interval=30.0, description="最新の環境データ行を読む")
class WideEnvironmentSource:
    def poll(self): ...

@kanary.source(source_id="postgres.wide", interval=30.0)
class WideEnvironmentSource:
    description = "最新の環境データ行を読む"

    def poll(self): ...
```

同じ属性を両方に書いた場合は、decorator の値が使われます。

推奨する書き分け:

- decorator: plugin を識別し、いつ・どこで動くかを決める短い値。`source_id` / `rule_id` / `output_id`、`interval` / `schedule`、`source` / `inputs`、`severity`、`tags`、`include_tags`、`exclude_tags`、`exclude_states`、`minimum_severity` など。
- class 本体: 長い文や動作に関わる設定。`description`、`runbook`、`thresholds`、`field` や `timeout` などの helper 設定、method。

`kanary lint` は次の場合に warning を出します。

- 同じ属性を decorator と class 本体の両方に、違う値で書いた（class 本体の `None` は仮の値とみなし、食い違いとして扱いません）
- decorator 引数が Kanary の知らない属性で、class でも定義されていない（例: `descripton=`）。近い名前があれば候補も表示します

独自の設定を decorator から渡したい場合は、class 本体で先に宣言してください（例: `dsn: str` や `dsn = None`）。そのうえで decorator で値を設定すれば warning は出ません。

## plugin のコードが動く場所

source と output はそれぞれ専用の thread で動くので、遅いものがあっても遅れるのはそれ自身だけです。rule だけは例外です。

- **Source:** source の `init()`、`poll()`、`terminate()` はその source の thread からしか呼ばれず、2 つが同時に動くことはありません。このため `init()` で作る接続は、作った thread でしか使えないもの（`sqlite3`）でも構いません。reload はその thread を止め、thread は古い instance を `poll()` が戻ってから（途中では決して）terminate します。新しい instance には新しい thread が始まります。`kanaryctl test-poll` も、その source の `test_poll()` を同じ thread で poll と poll の間に実行します。結果は表示するだけで評価には使わず、通常の poll の予定もずらしません。
- **Output:** `init()`、`emit()`、`OutputFollowups` と `QuietHours` の callback、`terminate()` は、すべてその output の thread で 1 つずつ実行されます。output 自身に lock は要りません。event は発生順に queue で待ち、遅い送信先や応答しない送信先が止めるのはその output の queue だけです。別の output 同士は並行に動くので、output 間で変更する状態を共有するなら lock が必要です。
- **Rule:** rule は依存関係の順に 1 つずつ評価します。rule は他の rule や複数の source に依存しうるためです。1 つの評価中は他の評価がすべて待つので、`evaluate()` は速く、I/O をしないでください。データの取得は source で行います。100 ms を超えた `evaluate()` は log に出し、Rules 画面に `SLOW` と表示します。output の routing filter（`matches()`）も評価の中で判定されます。

## 1. Source

### 最小 interface

必須:

- `source_id`
- `poll()`

任意 metadata:

- `description`

`description` は API と built-in Web viewer に表示されます。

任意:

- `interval`
- `schedule`
- `init()`
- `terminate()`
- `test_poll()`
- `max_retry`
- `max_reinit`

`test_poll()` は `kanaryctl test-poll` に応えるもので、source を変えずに「今 `poll()` したら返るもの」を返します。既定では `poll()` を呼びます。`poll()` が読むだけならそれで正しく動きます。上書きするのは、`poll()` が取ると無くなるものを取る（queue から値を取り出す、log の読み位置を進める）場合だけです。取らずに同じ形の結果を返してください。そうしないと、test poll が取ったデータを次の本番の poll が取り逃します。`LogCountSource` は対応済みです。`poll()` が `kanary.Buffer` に追加したものは test poll の後に自動で捨てられるので、履歴を持つだけなら上書きは要りません。

`interval` と `schedule` を両方省略した場合は、Kanary は
`interval = 60.0` を使います。  
`schedule` を使う場合は `interval` を同時に指定しないでください。class 自身（class 本体か decorator）に書いた方が、親 class から引き継いだもう一方より優先されます。
0.8.0 より前の `poll(ctx)`、`init(ctx)`、`terminate(ctx)` は、今はエラーです。`ctx` 引数を外してください。

`interval` は秒単位の polling 間隔です。`"30s"`、`"5m"`、`"1h30m"` のような文字列でも書けます。`interval = 0` は return したらすぐ次の `poll()` を呼びます。値を待つ `poll()` 専用です（「向こうから届く値」参照）。  
`schedule` は Unix cron 互換の 5-field 文字列で、Kanary server の local time
で解釈されます。`@hourly`, `@daily`, `@weekly`, `@monthly`, `@yearly` のような
少数の macro も使えます。
他の cron 実装の書き方も受け付けます。

- 月と曜日の名前。略しても略さなくても、大文字小文字も問いません: `JAN`、`september`、`mon-fri`、`Tues`
- 日曜は 0 でも 7 でもよい（`5-7` は金曜から日曜）。`5/15` は `5-59/15`
- 端をまたぐ範囲: 時の `22-2` は 22 時から 2 時、`FRI-MON` は金曜から月曜
- 日と曜日の欄の `?`。`*` と同じです
- 日の欄の `L`（月末日）と `L-2`（その 2 日前）
- 曜日の欄の `5L` / `FRIL`（その月の最後の金曜）と `MON#1`（第 1 月曜）
- 先頭の time zone: `CRON_TZ=Asia/Tokyo 0 9 * * *` や `TZ=UTC @daily`（`kanary.to_timezone()` が読める zone なら何でも）
- `@every 5m`（`90s`、`1h30m`、`2d` も）: schedule ではなく `interval = 300` として読みます

cron と同じく、日と曜日を両方指定した場合（どちらも `*` でも `?` でもない）は、どちらかに
合う日に動きます。`0 0 1 * 1` は毎月 1 日と毎週月曜に動き、`kanary lint` が警告します。
第 1 月曜なら `0 9 * * MON#1` と書きます。一度も動かない schedule（`0 0 31 2 *`。cron で
job を止めるときの書き方）は lint の警告付きで受け付けます。その source は `init()` を行い
`kanaryctl test-poll` にも答えますが、poll はされず、Sources 画面には `NOT POLLED` と出ます。
6 個や 7 個の欄（秒、年）は読みません。実装ごとに置く位置が違うためです。

次の実行は、どれだけ先でも見つけます（2 月の第 5 月曜 `0 9 * FEB MON#5` の次は 2044 年）。
夏時間の前後でも各欄は壁時計で読み、実行は実時刻で必ず前回より後になり、同じ時刻に 2 回動くことは
ありません。時計が戻る日は、2 回ある時刻に 1 回だけ動きます。時計が進む日は、飛ばされる時刻のうち
最初の 1 つだけが 1 時間遅れて動きます（`30 2 * * *` は 03:30。その日の 03:10 に KANARY を起動した場合も同じです。`*/15 2 * * *` は 03:00 に 1 回）。
回数が大事な場合は、夏時間の無い zone（`CRON_TZ=UTC` や日本）か `interval` を使ってください。

例:

- `interval = 60.0`
- `schedule = "*/5 * * * *"`
- `schedule = "@hourly"`

例:

```python
@kanary.source(source_id="sqlite", interval=5.0)
class SqliteSource:
    description = "最新の sqlite sample row を読み、Kanary input として公開します。"

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

再試行するのは `poll()` の例外だけです。`poll()` が poll 結果でないもの（たとえば `return object()`）を返した場合は source の bug なので再試行せず、`poll() returned an invalid result: ...` とともに `FAILED` と表示し、次の interval で再び poll します。

`init()` も source 自身の thread で動きます。失敗すると（起動時、停電の後でまだ起動中の database など）、source は `FAILED` になり、その thread が `init()` を同じ `max_reinit` 回の試行で、`interval` ごと（最長でも 1 分おき）に成功するまでやり直し、その後は通常どおり poll します。その間も他の source は動き、KANARY はそれを待たずに起動します。

### input を返す

`poll()` は `kanary.inputs(...)`, `kanary.no_data(...)`, `kanary.no_update(...)`, `kanary.skip(...)` のどれかを返します。

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
- timestamp は `datetime`、ISO 8601 の文字列、unix time のどれかです。[時刻と time zone](#時刻と-time-zone) を見てください
- `Decimal`（database driver が `NUMERIC` に返す型）や numpy の数は普通の `float` / `int` になるので、rule でも JSON でも数として扱われます
- NaN と無限大は測定値ではないので、その input は値なし（`None`。API と履歴では `null`）になり、`metadata["error"]` に理由（`value is nan`）が入ります。rule は欠測として扱います: `RangeRule(missing_fires=True)` は `no value: x (value is nan)` で発報します
- outer `metadata=` は snapshot の `metadata` として input と並んで入ります。`ctx.source_payload()["metadata"]` で読め、`kanaryctl test-poll` の出力にも出ます
- `kanary.no_data(reason=..., metadata=...)` は poll 自体は成功し、結果として空の snapshot を返すことを表します。source snapshot は更新され、rule も評価されます
- `kanary.no_update(reason=..., metadata=...)` は poll 自体は成功したが新しい snapshot は無いことを表します。直前の snapshot を保持したまま、その snapshot に対して rule を再評価します
- `kanary.skip(reason=..., metadata=...)` は今回の poll を完全に無視したい時に使います。source snapshot は更新されず、rule も評価されません
- 例外を送出した場合は source/plugin failure として扱われ、runtime の retry/reinit policy に入ります

helper source が返すものに input を足すには、その結果を `kanary.inputs()` に渡します:

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

結果の input と metadata が先に入り、その後に他の item が入ります。同じ名前の input は後の item で置き換わります。`kanary.no_update()` と `kanary.skip()` の結果には足す先の新しい snapshot が無いので、そのまま返ります。`kanary.no_data()` には input がありません。結果は `.status`（`"ok"`、`"empty"`、`"no_update"`、`"skip"`）と `.inputs` を持ち、`.inputs` の各要素は `kanary.Buffer` の sample と同じく `.name`、`.value`、`.timestamp`、`.metadata` を持ちます。

#### 時刻と time zone

time zone の無い時刻は、**KANARY を動かしている host の local time** として読みます。KANARY が時刻を受け取るところはすべて同じです: input の timestamp、`kanaryctl silence-until --start-at 2026-11-21T22:00`、HTTP API、state database。unix time は常に絶対時刻です。

できるだけ time zone を付けてください。どの host で読んでも同じ意味になります:

```python
from datetime import datetime, timezone

datetime.now().astimezone()      # 今。この host の offset 付き
datetime.now(timezone.utc)       # 今。UTC
kanary.to_datetime("2026-11-21 22:00", naive_timezone="Asia/Tokyo")  # 別の time zone の system から来た文字列
"2026-11-21T22:00:00+09:00"      # offset 付きの ISO 8601 文字列（UTC なら "...Z"）
```

SQL では `timestamp with time zone`（PostgreSQL の `timestamptz`）を勧めます。time zone 無しで UTC（または host と違う zone）の時刻を入れている table には、SQL helper の source に `timestamp_timezone = "UTC"` を設定してください。`StaleRule` は 30 分より先の未来の timestamp を警告します。time zone の間違いはたいていこの形で現れます。

典型的な使い分け:

- 通常の成功時は `kanary.inputs(...)`
- 「空であること自体が正しい現在値」の時は `kanary.no_data(...)`
- `StaleRule` などに最後の有効 snapshot を見せ続けたい時は `kanary.no_update(...)`
- warm-up や maintenance などの明示的な no-op にだけ `kanary.skip(...)` を使います。`skip` では stale 判定も進みません

plugin は、site-specific な設定が必要な場合に自分の directory 内の local file を自由に読んで構いません。  
Kanary にはそのための小さな helper があります。

```python
import kanary

config = kanary.load_toml()
dsn = kanary.load_toml("dsn")
role_id = kanary.load_toml("mention.role_id", filename="discord_config.toml")
plugin_root = kanary.plugin_dir()
```

- `kanary.load_toml()` の既定 file 名は `config.toml` です
- `kanary.load_json()` の既定 file 名は `config.json` です
- 相対 `filename=` は、その helper を呼んだ script file の directory 基準で解決されます
- 絶対 path もそのまま使えます
- `key` を省略すると file 全体の辞書を返します
- `"mention.role_id"` のような dotted key で nested な TOML table / JSON object を辿れます
- file が無い時や key が無い時は `RuntimeError` を送出します（`kanary lint` のときは warning を出して `None` を返します）
- JSON file には同じ仕様の `kanary.load_json(...)` が使えます

`Path(__file__).with_name(...)` を直接使う書き方も、細かく制御したい時には引き続き有効です。  
これらの local config file は auto-reload の検知対象ではないため、変更後は `kanaryctl reload ...` を明示的に実行してください。

secret（password、token、webhook URL）は plugin file に書きません。よく使う置き場所は環境変数で、systemd の unit、`EnvironmentFile`、kanary を起動する shell などで設定します:

```python
import os

@kanary.output(output_id="slack")
class Slack(kanary.SlackOutput):
    url = os.environ.get("SLACK_WEBHOOK_URL")
```

`os.environ.get` は変数が無ければ `None` を返すので、その output が自分の `init()` で失敗し（"url is not set"）、他の plugin は動き続けます。同じ場所で `kanary.load_json()` を使えば、plugin の隣の file から読めます:

```python
    url = kanary.load_json("slack_webhook_url", filename="secrets.json")
```

class の中に書いた場合は、plugin file を読み込むときに読みます。file や key が無ければ KANARY は起動せず、reload では reload が拒否されます（動いている plugin はそのまま）。`kanary lint` は設定を使わないので、lint のときだけは file や key が無くても warning と `None` になるだけです。`secrets.json` は plugin の隣に置いて `chmod 600` にし、version 管理には入れません（`.gitignore` など）。組み込み helper は secret を普通の設定（`url`、`password`、`smtp_password`、`headers`）として受け取るので、test では直接設定できます。

## 2. Rule

### 最小 interface

必須:

- `rule_id`
- `inputs`
- `severity`
- `evaluate(ctx)`

`tags` は tag の list か、1 つなら文字列（`tags="infra"`）です。省略できますが、書いてください。tag の無い rule は `include_tags` を持たない output にしか届かず、tag を指定した silence の対象にならず、tag で絞り込む remote の取り込みからも漏れます。lint はそのような rule に warning を出します。

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

どれも alert API と viewer の detail に出ます。`runbook`（まず何を確かめるか、dashboard への link など）は rule の通知にも付きます: Slack、Mattermost、Discord、mail では「Runbook」の field（URL は link になる）、webhook の JSON では `runbook`。

`evaluate(ctx)` は速く、I/O をしないでください。rule は 1 つずつ評価され（「plugin のコードが動く場所」参照）、100 ms を超えると `SLOW` と表示されます。

`severity` は default / fallback severity として使われます。  
`kanary.firing(..., severity=...)` または `kanary.Evaluation(severity=...)` を返すと、その評価だけ上書きできます。
0.8.0 より前の `evaluate(payload, ctx)` は、今はエラーです。値は `ctx` から読みます（`ctx.value()`、`ctx.source_payload()`）。

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

これらを組み合わせた小さな helper もあります:

- `ctx.age(selector=None)`: input の timestamp から `ctx.now` までの秒数
- `ctx.delta(selector=None)`: 今回の値 - 前回の値。どちらかが数値でなければ `None`
- `ctx.changed(selector=None)`: 今回と前回の値が両方あり、異なれば `True`

「前回」は source の前回の poll の snapshot です。data の更新より速く poll する source では同じ sample を再び読むので、`delta()` は `0`、`changed()` は `False` になります。それが問題になる場合は、新しいものが無いとき `poll()` が `kanary.no_update()` を返すようにするか（snapshot が回らないので、前回は常に前の sample になります）、rule で timestamp を比べます。読めた値をそのまま返す source でも構いません。そこは source を書く人の判断です。
- `ctx.missing(*selectors)`: 何にも一致しない selector（省略時は rule の `inputs`）と、一致したが値が無い input

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
`ctx.source_payload("source-id")` は 1 つの source の snapshot 全体で、input 名は source 自身の名前です。source を 1 つだけ読む rule では id を省けます。cross-source の参照は `ctx.inputs()` を使ってください。

```python
{
    "status": "ok",              # kanary.no_data() の後は "empty"
    "reason": "...",             # poll が与えたときだけ
    "metadata": {"rows": 12},    # poll が与えたときだけ
    "inputs": {"temperature": {"value": 21.5, "timestamp": datetime(...), "metadata": {}}},
}
```

0.8.0 より前は、input は `"channels"` の下にありました。

### 評価結果を返す

通常の public API は次のどれかです。

- `kanary.ok(message, extra=...)`
- `kanary.firing(message, severity=..., extra=...)`
- `kanary.warn(...)`, `kanary.error(...)`, `kanary.critical(...)`
- `kanary.fire_if(...)`, `kanary.warn_if(...)`, `kanary.error_if(...)`, `kanary.critical_if(...)`
- `kanary.fire(...)` は `kanary.firing(...)` と、`kanary.firing_if(...)` は `kanary.fire_if(...)` と同じです。読みやすい方を使ってください

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
- severity はどこでも同じ書き方ができます（rule の `severity`、`thresholds`、`kanary.firing(severity=...)`、output の `minimum_severity`）: `kanary.ERROR`、大文字小文字を問わない名前（`"ERROR"`、`"Error"`、`"error"`）、数値（`30`）。指定しない rule は `ERROR` です

payload を明示しない場合、Kanary は current source payload を自動で引き継ぎます。追加情報を入れたい時は `extra={...}` を使って merge します。  
`kanary.Evaluation(...)` も advanced な書き方として引き続き使えます。`state` は `kanary.FIRING` / `kanary.OK` か、大文字小文字を問わない名前（`"firing"`）で、`severity` は他と同じ書き方です。それ以外の値ではその rule が理由とともに失敗します。

## 3. Output

### 最小 interface

必須:

- `output_id`
- `emit(event)`

任意 metadata:

- `description`

`description` は API と built-in Web viewer に表示されます。

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

0.8.0 より前の `emit(event, ctx)`、`init(ctx)`、`terminate(ctx)` は、今はエラーです。`ctx` 引数を外してください。

`emit()` はその output 専用の thread で動きます。engine は event をどの output に送るかを決めて queue に入れるだけなので、評価、他の output、operator の操作が送信を待つことはありません。各 output は event の順序を保ちます。output ごとに最大 1000 件まで待たせ、それを超えると最も古いものを捨てて log に出し、未送信として記録します。Outputs 画面に待ち件数と捨てた件数を表示します。停止時、queue に残った event は最大 5 秒まで送ります。

`include_tags` と `exclude_tags` は glob pattern を使えます。  
たとえば `include_tags=["expert_*"]` とすると、`expert_db` や `expert_shift` のような tag に一致します。pattern は tag に対して一致を見るので、`include_tags=["*"]` でも tag の無い rule には一致しません。

`exclude_states` は current state が指定値に一致する event を除外します。default は `["SUPPRESSED", "SILENCED"]` です。

tag の pattern、state、transition が 1 つだけなら list でなく文字列でも書けます（`include_tags="infra"`）。state と transition の大文字小文字は問いません（`"ok"`）。不明な state や transition の名前は file の読み込み時にエラーになります。default に `OK` を追加して recovery event も除外する場合は、`exclude_states=kanary.Output.exclude_states + ["OK"]` と書けます。指定したリストは default との merge ではなく置き換えになるため、すべての state event を受け取るデバッグ・監査用 output では `exclude_states=[]` を明示します。

`OK` は、その rule について前回の `OK` 以降に `OK` 以外の event（`FIRING`、`ACKED`、または `exclude_states` が通す state）を送った output にだけ届きます。`OK` は、その output に伝えたことを閉じるためのものだからです。「送った」とは `emit()` が成功したことです。`FIRING` が送れなかった（送り先が落ちていて、retry もすべて失敗した）場合、その output にはその `OK` も送りません。このため、silence や suppression の前から `OK` だった rule は、それが終わっても `OK` を送りません。一方、発報してから silence され、silence 中に治った rule は、silence が終わったときに `OK` を送ります。再起動の後は、`OK` 以外で復元された alert を「送った」とみなします。こうして送らなかった `OK` は、output dispatch の履歴に filtered として記録されます。

`OK` は、その output にその alert について最後に送った severity を持ちます（`minimum_severity` が見る `event.effective_severity`）。`minimum_severity=kanary.ERROR` の output に `CRITICAL` の alert を送っていれば、alert が `WARN` に下がってから治った場合も、その output に `OK` が届きます。`--state-db` があれば、各 output に何を送ったかを保存するので、再起動を挟んでも同じです。

filter を通る遷移を落としたい場合は、`emit()` 内で `previous_state` と `current_state` を確認します。

```python
def emit(self, event):
    if event.previous_state == kanary.SILENCED and event.current_state == kanary.OK:
        return
    ...
```

`exclude_transitions` も default は空です。severity 低下を通知したくないなら `DEESCALATED` を明示的に追加します。

`exclude_states` によく入る値:

- `OK`
  復旧通知。
- `FIRING`
  異常発火中の通知。
- `ACKED`
  operator が確認したことを示す通知 (`FIRING -> ACKED`)。
- `SILENCED`
  active な silence の対象になり、通常の alert state が一時的に mute されている状態。
- `SUPPRESSED`
  `suppressed_by` か `depends_on` にある rule が発報しているため、抑えられている alert。

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
    minimum_severity=kanary.ERROR,
)
class DiscordOutput:
    def emit(self, event):
        ...
```

### 時間を置いた followup

`kanary.OutputFollowups` は、最初の出力後も alert が対応されていなければ別の処理を呼ぶための、process 内 helper です。

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

`now()` は現在の event を渡して callback を同期実行します。callback の例外は `emit()` へ伝播するため、Output の retry/reinit の対象になります。

それ以外の callback は、その output の thread で `emit()` と `emit()` の間に実行され、`emit()` と同時には走りません。`emit()` が遅いとその分遅れます。そこで起きた例外は log に出し、output の last error として表示します。retry はしません。

`after()` の時刻は、同じ `rule_id` で最初に followup を登録した時刻からの絶対 offset です。上の例では mailing list は group の実行から 20 分後ではなく、最初の出力から 20 分後に呼ばれます。callback には登録時の event ではなく、`for_event(event)` で最後に確認した event が渡されます。

`cancel(callback)` とすると指定 callback の登録だけを取り消せます。callback がすでに実行を始めている場合は停止できません。登録は memory 上だけに保持されるため process restart 後には引き継がれず、`terminate()` では `close()` を呼びます。

`SILENCED` と `SUPPRESSED` を見て followup を取り消す Output は、default routing でそれらを除外させず、decorator に `exclude_states=[]` を指定する必要があります。`kanary new -t followups` で完全な例が出てきます。

rule に `fire_after` の設定はありません。遅らせる目的によって置き場所が変わります:

- 短い逸脱はそもそも異常ではない（測定ノイズ）: source で決めます。UI と全 output で判断が揃います。たとえば `kanary.Buffer` の Series から window 内の最小値を input として出し、`RangeRule` を掛けます。window の全 sample が上限を超えたときだけ上限超えになります（`buffered` template）。
- alert として見えてよいが、この output ではまだ呼び出さない: `after(delay, ...)` で通知を予約し、復帰したら `cancel()` します。復帰の通知は発報を送った alert にだけ送ります（`delayed` template）。

実行失敗からの復帰の default:

- `max_retry = 1`
- `max_reinit = 1`

`emit()` が例外を送出した場合、Kanary は即 failed に固定せず、output の thread で再試行します。その間待つのは、この output の後続の event だけです。  
N 回目の復帰試行の前には `N**2` 秒待ちます。default では次の順です。

1. 1 秒待って `emit()` を再試行
2. 4 秒待って `terminate() -> init()` を行い、その後 `emit()` を再試行

それでも失敗すると output は `FAILED` になります。`FAILED` の間は、後続の event を待ちなしで 1 回だけ試します。送信先が落ちているときに、queue の event がすべて再試行の全手順を待つことはありません。1 回でも成功すれば `READY` に戻ります。失敗した event は再送しません。

`init()` に失敗した output（送信先にまだ届かない）にも event は届きます。その thread は event を送る前に `init()` を呼び、何もしていない間も、10 秒後、その後は倍ずつ、最長 5 分おきに `init()` をやり直します。送信先が落ちている間に来た event は後から送られず、dispatch の履歴に失敗として残ります。

## 4. 組み込み helper class

helper class は engine とは別の `src/kanary/helpers/` にあり、上で説明した plugin の API だけで書かれているので、それぞれが書き方の見本として読めます。plugin からは module ではなく `kanary` から使います（`kanary.SQLWideSource`、`kanary.Buffer`）。

### Source 側

#### 履歴を持つ: kanary.Buffer

`kanary.Buffer` は最近の sample を貯めておくもので、source はそこから計算した値（平均、傾向、spike など）を出せます。普通の object なので、`init()` で作り、`poll()` で追加して読みます。

```python
@kanary.source(source_id="vacuum", interval=10 * kanary.second)
class Vacuum(kanary.Source):
    def init(self):
        # input ごとに最大 1024 sample、1 時間より古いものは捨てる（最新の sample から数える）。
        # 先に当たった方の制限で古いものから捨てる
        self.buffer = kanary.Buffer(size=1024, max_age=1 * kanary.hour)

    def poll(self):
        self.buffer.add(("pressure", read_gauge()))  # the same items as kanary.inputs()
        pressure = self.buffer["pressure"]
        if pressure.span < 10 * kanary.minute:  # 10 分ぶん貯まるまで待つ
            return kanary.skip(reason="filling the buffer")
        last_10m = pressure.window(10 * kanary.minute)
        return kanary.inputs(
            ("pressure", pressure.last.value),
            ("pressure_avg_10m", last_10m.mean()),
            ("pressure_min_5m", pressure.window(5 * kanary.minute).min()),
        )
```

buffer は input 名ごとに、追加された sample を古い順に持ちます。各 sample は `.name`、`.value`、`.timestamp`、`.metadata` を持ちます。

追加:

- `add(...)` は `kanary.inputs()` と同じ形を受け取ります: `add(("pressure", value))`、tuple を複数、`("pressure", value, observed_at)` や `("pressure", value, observed_at, metadata)`、dict。timestamp は datetime か unix time で、無ければ今の時刻（または `timestamp=` の時刻）になります。読み値に自分の時刻があるときは渡してください。window が正しくなります。
- `add(result)` は poll の結果をまるごと追加します: `kanary.inputs(...)` や helper の `super().poll()`。これでどの helper class にも履歴を足せます。たとえば `HTTPJSONSource` の subclass の `poll()` で `self.buffer.add(super().poll())` してから buffer で計算します。ok でない結果（`no_update()`、`skip()`、error）は何も追加しません。自分の `init()` を持つ helper（`SQLSource`、`LogCountSource`）では、あなたの `init()` で `super().init()` を呼んでください。
- その input の最新の sample と同じ timestamp の sample はそれを置き換え（同じ読み値をもう一度読んだもの）、古い sample は無視します。値が更新されない source でも統計は歪みません。

`self.buffer["name"]` は `kanary.Series` で、その input の sample の読み取り専用のコピーを古い順に持ちます。一度も追加されていない名前は `KeyError` になり、buffer にある input の名前を示すので、打ち間違いはすぐ分かります。まだ来ていないかもしれない input は `"name" in self.buffer` で確かめます。

- sample: `first`、`last`（`.value`、`.timestamp`、`.metadata` を持つ sample。無ければ `None`）、`values`、`timestamps`、`len()`、反復、`span`（最初から最後までの秒数）
- 絞り込み: `window(duration)` は最新の sample から数えて直近 `duration`（`10 * kanary.minute`）の sample。`tail(n)` は最新の `n` 個
- 統計: `mean()`、`median()`、`min()`、`max()`、`sum()`、`stddev()`、`delta()`（最後 − 最初）、`rate()`（最初と最後の sample から）、`slope()`（全 sample の最小二乗。ノイズ 1 点の影響を受けにくい。`rate()` とも毎秒あたりで、`* kanary.minute` で毎分）、`zscore()`（最新値が平均から標準偏差何個分離れているか）、`eta(target)`（傾向が `target` に届くまでの秒数。平坦か離れていく場合は `None`）、`changes(tolerance=)`（隣り合う sample の間で値が変わった回数。バタつき、flapping）、`unchanged_seconds(tolerance=)`（値が変わっていない秒数。固まった sensor は timestamp だけ新しくして同じ値を送り続ける）

`values` と `timestamps` は普通の list なので、ほかの計算には `numpy.array(series.values)` などがそのまま使えます。

複数の input をまとめて扱うには、`self.buffer.select("temp_*")` で一致する名前の `{name: Series}`、`self.buffer.last_values("temp_*")` で `{name: 最新値}` が得られます。`self.buffer.names` は全部の名前です。`kanary.inputs(self.buffer.last_values())` で最新値をそのまま出せます。`print(self.buffer)` で、input ごとの sample 数、いつからいつまでか、最新値が表示されます。

buffer が貯まるまでに何を出すかは source が決めます。`poll()` で `len(series)` や `series.span` を見て、たとえば上の例のように十分貯まるまで `kanary.skip()` を返します（新しい snapshot を作らず、rule も評価しません）。途中までの結果をすぐに出してもかまいません。データが無い統計は `None` を返します（window が空、rate に sample が 1 個、標準偏差に値が 2 個未満など）。その input は値なしになります。統計には数値だけを使い、`None` や文字列は除きます。

`kanaryctl test-poll` も `poll()` を実行します。そのとき buffer に追加したものは test poll が終わると捨てられるので、次の本番の poll から見える履歴は変わりません。buffer は lock を持たないので、`poll()` から使ってください（test poll も同じ thread で動きます）。listener など別の thread から追加する場合は、自分で lock を掛けてください。

履歴は rule ではなく source が持ちます。rule が見られるのは今回と前回の snapshot だけです。結果は `poll()` から input として出し、普通の rule で判定します。例えば `eta` の input に `direction = "low"` の `ThresholdRule` をかけます。比べるのは取得した sample だけなので、2 回の取得の間に変わって戻った値は見えません。それより速い変化は上流で数えてください。buffer は memory 上にあり、reload や再起動の後は空から始まります。`buffered`、`trend`、`spike`、`frozen` の template に統計の使い方があります。

#### CommandSource と SSHCommandSource

`kanary.CommandSource` は poll ごとに `command` を有限の `timeout` で実行します（list はそのまま実行し、文字列は shell と同じ規則で分割しますが shell は使いません）。stdout は `output_format` に従って数値の input に変換します:

- `"number"`: 出力全体が 1 つの数値。名前は `value_name`
- `"keyvalue"`: 空白か改行で区切った `name=value`
- `"json"`: 数値か object。入れ子の key は `a.b` になる
- `"auto"`（既定）: 出力の形から上のどれかを選ぶ

input には任意の Python object を入れられますが、rule が比べるのは数値なので、helper は `kanary.to_number()` で変換します（`"85%"` は `85.0`、`true`/`false` は `1.0`/`0.0`）。変換できない値は `metadata["text"]` に残します。result の metadata には program 名だけを入れ、引数は入れません（token を含むことがあるため）。exit code は `exit_code` input として出し（`report_exit_code = False` で省略）、timeout は poll の失敗になります。独自形式は `parse(stdout)` を override します。`env` の値は文字列として渡し（`{"RETRIES": 3}` でもよい）、相対パスの `cwd` は他の helper と同じく plugin file の隣を基準にします。

`kanary.SSHCommandSource` は `ssh` 経由で `host` 上で command を実行します。`BatchMode=yes` と `ConnectTimeout` を付け、`user`、`port`、`identity_file`、`ssh_options` を使います。remote の command は subshell で実行し、続けて exit code を書いた marker 行を出すので、ssh 自体の失敗（marker が無い）は poll の失敗として raise し、command 自身の exit code は 255 でも `exit_code` として出します。remote の login shell は POSIX 系が前提です。`wrap_exit_code = False` にすると、exit code 255 は常に ssh の失敗として扱います。他のマシンの disk、load、GPU 状態を読むときの基本形です。

#### PingSource、TCPSource、HTTPStatusSource

どれも poll ごとに対象を並列に確認し、測ったものを出します:

- `PingSource`: `hosts` で input 名 → host（host の list でもよく、その場合は host 名がそのまま input 名）。`<name>.rtt_ms` は system の `ping` command で ICMP echo を 1 回送った往復時間です。
- `TCPSource`: `targets` で input 名 → `"host:port"`（list でもよく、`"db1:5432"` は `db1_5432`）。`<name>.connect_ms` は TCP 接続にかかった時間です。接続後は何も送りません。
- `HTTPStatusSource`: `urls` で input 名 → URL（list でもよく、`"https://example.org/health"` は `example.org/health`）。`<name>.status` は HTTP status code、`<name>.response_ms` は応答時間です。`503` などの error status は rule で判定する値であり、失敗ではありません。

応答が無いことは結果であって poll の失敗ではありません。その input は値なし（`None`）になり、error の文面が metadata に入ります。rule に `missing_fires = True` を付けるとこれで発報します。たとえば `*.rtt_ms` に `high = 200.0` の `RangeRule`、`*.status` に `expected = 200` の `ExpectedValueRule` です。JSON の応答から値を読むなら `HTTPJSONSource` を使います。

#### FileSource と LogCountSource

`kanary.FileSource` は `paths` の各項目について `<name>.exists`、`<name>.age_seconds`、`<name>.size_bytes` を出し、`read_last_line = True` なら最終行を `<name>.value` として出します。相対 path は plugin file の隣として解決します。

項目には `"/data/runs/*.root"` のような glob pattern も書けます（`**` は下の folder にも一致）。このとき各 input は一致した中で最も新しい file を表し、加えて `<name>.count`（一致した file 数）と `<name>.total_size_bytes` を出します。`<name>.exists` は何か一致すれば `1` です。`paths` は単なる list でもよく、その場合 input 名は file 名（`"daily.tar.gz"`）、pattern なら固定部分（`"*.root"` なら `root`）になり、名前が重なると例外になります。

`kanary.LogCountSource` は `patterns` の正規表現ごとに、前回の poll 以降に `path` に追記された行のうち一致した行数を出します。つまり `interval` が集計期間です。rotation や truncate の後は file の先頭から読みます。`copytruncate` で切り詰められた後に元の位置を超えて書き込まれた場合も、最後に読んだ数 byte を比べて検出します。`start_at_end = True` なら起動時点で file にあった内容は無視します。

#### HTTPJSONSource

`kanary.HTTPJSONSource` は `url` を `headers` と `timeout` 付きで GET し、`fields`（input 名 → `data.sensors.0.value` のような dotted path）を数値の input にします。`fields` が空なら数値の葉をすべて使います。見つからない path と数値でない値は result の metadata に入ります。別の形の document は `select(document)` を override し、token は `init()` で環境変数から設定します。

#### PrometheusSource

`kanary.PrometheusSource` は node_exporter の `/metrics` や application の metrics など、Prometheus の text 形式の page を `url`、`headers`、`timeout` で読みます。`metrics` は input 名 → series で、Prometheus と同じ書き方をします:

```python
metrics = {
    "load1": "node_load1",
    "root_free_bytes": 'node_filesystem_avail_bytes{mountpoint="/"}',
    "temp.{sensor}": 'node_hwmon_temp_celsius{chip="platform_coretemp_0"}',
}
```

書いた label は完全に一致する必要があり、書かなかった label は何でも構いません。series selector が複数の series に一致するときは、input 名に変わる label を書きます。`{sensor}` は各 series の label に置き換わり、`temp.temp1`、`temp.temp2` になります。書かないと poll が失敗し、どの label かを示します。page に無い series は結果の `metadata["missing"]` に並び、`NaN` は値なしになります。page 自体が応答しないのは poll の失敗です。counter（`..._total`）は増える一方なので、`RateRule` を掛けると毎秒あたりの rate になります。

#### SlowDashSource

`kanary.SlowDashSource` は [SlowDash](https://github.com/slowproj/slowdash) の channel の最新点を web API から `url`、`headers`、`timeout` で読みます。`url` は browser で開く SlowDash の address（`http://localhost:18881`）で、`.../slowplot.html?config=...` のような page の address でも構いません。`channels` は input 名 → channel で、channel の list なら各 channel 名がそのまま input 名になります:

```python
channels = {
    "oil_temperature": "oil.temperature",
    "run_state": "daq.run_state",
    "pmt_hv.*": "hv.pmt.*",  # hv.pmt.<x> の channel 全部を pmt_hv.<x> として
}
```

`*`、`?`、`[...]` を含む channel は pattern で、SlowDash が一覧に出す channel（`GET /api/channels`、pattern があるときだけ問い合わせる）のうち一致するものをすべて取ります。list に書いた pattern は各 channel 名をそのまま input 名にします（`channels = ["hv.pmt.*"]`）。input 名の `*` は pattern の `*` に一致した部分に置き換わるので、上の `hv.pmt.17` は `pmt_hv.17` になります。複数の channel に一致する pattern は input 名に `*` が必要で、無いと poll が失敗し、一致した channel を示します。何にも一致しない pattern は `slowdash.missing`（後述）に数えます。

input `slowdash.up` は SlowDash が `GET /api/ping` に応答する間 1 です。応答しない（接続拒否、timeout、前段の proxy の error）ときは `slowdash.up` が 0 になって理由が `metadata["error"]` に入り、channel の input はありません。応答が無いことは rule で判定する値であって、source の失敗ではありません。`expected = 1` の `ExpectedValueRule` で判定し、channel の rule には `suppressed_by` を付けます（`slowdash` template のとおり）。

続いて poll のたびに全 channel の直近 `lookback` 秒（10 分）を 1 回の request（`GET /api/data/CH0,CH1?length=...&prior_data=1`）で取り、各 channel の最新点を使います。input の timestamp はその点自身の時刻なので、`StaleRule` を掛けると channel の更新が止まったときに発報します。その間に点が無い channel も、SlowDash の SQL data source（`prior_data`、SlowDash 2026-02 以降）ならその前の最後の点が返るので、変化したときだけ書かれる channel にも使えます。それ以外の data source では返らず、その channel の input はありません。窓の中の点はすべて送られるので、速い channel が多いときは `lookback` を短くします。数値と状態（`"ON"` は 1）は `kanary.to_number()` と同じく変換し、`"RUN"` のようなその他の text は `ExpectedValueRule` 用にそのまま残し、object（histogram、table、tree）は `metadata["objects"]` に並びます。SlowDash が ping には応答するのに data の request に応答しないのは poll の失敗です。

input `slowdash.missing` は点が得られなかった `channels` の項目の数です。channel 名の typo、SlowDash から消えた channel、`prior_data` が無いときに `lookback` の間に点が無い channel、何にも一致しない pattern が数に入ります。その名前は `metadata["missing"]`（input と結果の両方）にあります。`*` の rule は今ある input しか判定しないので、input の無い channel は見落とします。`slowdash.missing` に `high = 0` の `RangeRule` を掛けるとこれを捕まえられます（`slowdash` template のとおり）。

#### SQLWideSource と SQLLongSource

通常は設定だけで済みます。接続と SELECT は設定から組み立てます。どちらの class を使うかは table の形で決まります:

**wide** な table は、時刻ごとに 1 行で、量ごとに column を持ちます。`SQLWideSource` は最も新しい行を読み、各 column が input になります。

| observed_at | temperature | humidity | pump_state |
|---|---|---|---|
| 2026-11-20 10:00:00 | 21.5 | 40.1 | 1 |
| 2026-11-20 10:00:30 | 21.6 | 40.3 | 1 |

**long** な table は、量と時刻ごとに 1 行で、名前の column と値の column を持ちます。`SQLLongSource` は名前ごとに最も新しい行を読み、各名前が input になります。

| observed_at | metric_name | metric_value |
|---|---|---|
| 2026-11-20 10:00:00 | room_a.temperature | 21.5 |
| 2026-11-20 10:00:00 | room_b.temperature | 19.8 |
| 2026-11-20 10:00:30 | room_a.temperature | 21.6 |

決まった量をまとめて書き込むなら wide、量が多い・増減する・別々に書き込むなら long が向いています。

- 接続: `database_type`（`"sqlite"`、`"postgresql"`、`"mysql"`。MariaDB も `"mysql"`）、`database`（SQLite は file path で、相対なら plugin file の隣。それ以外は database 名）、`host`、`port`、`user`、`password`（secret なので `password = os.environ.get("DB_PASSWORD")`）、`timeout`（接続と statement の timeout）。PostgreSQL は KANARY と一緒に入る `psycopg` を使います。MySQL には `pymysql` package が必要で、KANARY を動かす Python 環境に入れてください（[Deployment: plugin が追加 module を要求する場合](deployment_ja.md#plugin-が追加-module-を要求する場合)）。接続は autocommit で、poll が失敗すると通常の retry/reinit で再接続します。
- `SQLWideSource`: `table`、`timestamp_column`、`columns`（input 名 → column。空なら全 column）。最も新しい行を読みます: `SELECT ... ORDER BY timestamp_column DESC LIMIT 1`。
- `SQLLongSource`: `table`、`name_column`、`value_column`、`timestamp_column`。name ごとに最も新しい行を読みます。`lookback` で最近の行だけを走査します。`names` で上流の名前 → input 名を対応させ、それ以外は無視します。

table 名と column 名は単純な識別子に限ります（`schema.table` は可）。join、view、`WHERE` など別のものを読むときは `query`（と driver の parameter style の `params`）を書きます。その場合は書いたとおりに実行し（新しい行を先頭に）、table の設定は使いません。別の driver を使うなら `connect()` を override して DB-API connection を返します。

`timestamp_column` で各 input の timestamp を決めます。timezone の無い timestamp は `timestamp_timezone`（`kanary.to_timezone()` が読める time zone: `"Asia/Tokyo"` などの IANA 名、`"+09:00"`・`"+9"`・`"UTC+9"` などの offset、`"UTC"`）で、未設定ならこの host の local time として読みます。offset は変わらないので、夏時間のある地域では IANA 名を使ってください。どちらの class も意図して短くしてあるので、別の形の source を書くときは `src/kanary/helpers/sql.py` を読んでください。

#### 向こうから届く値（webhook、MQTT、callback）

値が届いた瞬間に評価したいときは、`poll()` の中で値を待ちます。その source の rule は `poll()` が return した直後に評価され、`interval = 0` は return したらすぐ次の `poll()` を呼ぶので、source は常に待ち受けている状態になります。

- `kanary.HTTPPushSource` は HTTP の POST を受け取ります（`host`、`port`、`path`、`token`）。body は input 名 → 値、または → `{"value": ..., "timestamp": ...}`（unix time か ISO 8601）の JSON object です。`token` を設定すると `Authorization: Bearer <token>` が必要になります。標準 library の `http.server` を使うので、信頼できる network 向けです。internet から受けるときは前段に TLS 付きの reverse proxy を置いてください。別の形式は `parse(body)` を上書きします。
- `kanary.MQTTSource` は `topics`（topic filter → input 名）を subscribe します。`None` にすると（wildcard を含む filter は必ず `None`）、topic の名前がそのまま input 名になります（`plant/pumps/p1/flow` → `plant.pumps.p1.flow`）。設定は `host`、`port`、`username`、`password`、`tls`、`qos` です。payload は数値、状態（`"ON"` や `"true"` は `kanary.to_number()` と同じく 1 になります）、その他の文字列（そのまま）、JSON で、JSON object は key ごとに 1 つの input になります。input `mqtt.connected` は接続中 1 です。client は自分で再接続し、接続のたびに subscribe し直します。`paho-mqtt` package を KANARY を動かす Python 環境に入れる必要があります（[Deployment: plugin が追加 module を要求する場合](deployment_ja.md#plugin-が追加-module-を要求する場合)）。`init()` の中で import するので、無ければその source だけが失敗します。別の形式は `parse(payload)` を上書きします。
- それ以外の受け口（装置の library の callback、socket、serial など）には `kanary.Inbox` を使います:

```python
def init(self):
    self.inbox = kanary.Inbox()
    ...  # 受け口を起動する。受け口は自分の thread で self.inbox.put(("temperature", 21.5)) を呼ぶ

def poll(self):
    return self.inbox.wait(timeout=1.0)
```

  - `put()` は `kanary.inputs()` と同じ形を受け取り、どの thread からでも呼べて、待たされることはありません。時刻の無い値には、`poll()` が取り出した時刻ではなく届いた時刻が付きます。
  - `wait(timeout)` は何か届くまで最大 `timeout` 秒待ち、届いたら全 input の最新値を返します。今回届かなかった input は前回の値のままなので「無い」扱いにならず、一度に大量に届いても評価は 1 回です。何も届かなければ `kanary.skip()` を返します（新しい snapshot を作らず、rule も評価しません）。
  - `take(timeout)` は代わりに届いた sample を全部返します。一度に届いた値をすべて見たい `poll()` 用で、たとえば `kanary.Buffer` に入れます。
  - 待てるのは最大 `size`（10000）件で、溢れた分は古いものから捨てて `dropped` に数えます。結果の metadata にも出ます。
  - `kanaryctl test-poll` は待たず、何も取り出さずに、`wait()` が返すものを表示するので、`test_poll()` は要りません。
- KANARY は poll と poll の間でしか待ち受け中の source を止めたり reload したりできず、それを 2 秒待つので、1 回の待ちは 1 秒程度にします。`terminate()` で受け口を止めてください。古い instance の `terminate()` は新しい instance の `init()` より先に動くので、reload で同じ port を取り直せます。
- 何も届かないことは source にとって error ではありません。その input に `evaluation_interval` 付きの `StaleRule` を掛けます。
- `interval = 0` は待つ `poll()` 専用です。すぐ return する `poll()` に使うと CPU を 1 コア使い切ります。

`HTTPPushSource` と `MQTTSource` は `interval = 0` でしか動きません。`interval` と `schedule` は書かないでください。それ以外を書くと `init()` が理由付きで失敗します。`http-push`、`mqtt`、`push`（受け口を自分で書く）の template にそれぞれの例があります。

### Rule 側

組み込みの rule は `inputs` で選んだ input の値だけを見ます（source payload の path を指す `field` 系の設定はありません）。どれも短く書かれているので、継承して上書きする前に `kanary/helpers/value_rules.py` を読むとそのまま動きが分かります。

#### ValueRule

`kanary.ValueRule` は `RangeRule`、`ThresholdRule`、`StaleRule`、`RateRule` の基底です。`evaluate()` は選んだ各 input を独立に判定し、最も重い結果で発報します。継承する側は小さな method だけを上書きします:

- `check(value, ctx)`: 発報なら `True` か severity、正常なら `False` か `None` を返す
- `value_of(item, ctx)`: 判定する数値（既定は input の値）。`None` ならその input は `missing_inputs` に入る
- `phrase(fired)`: message の末尾（`"out of range [0, 100]"` など）。数値の書式は `format_value(value)`

message は `<source:input>=<value> <phrase>` の形です。payload には `values`（input 名から判定した値）、`matched_inputs`、`missing_inputs` が入ります。`missing_fires = True` なら判定する値の無い input も発報し、message には metadata の `error` が入ります。`StaleRule` はこれを設定しています。応答の無かった probe を検知する方法でもあります。

```python
@kanary.rule(rule_id="pump.mode.even", inputs="pump:*", severity=kanary.WARN, tags=["pump"])
class EvenMode(kanary.ValueRule):
    def check(self, value, ctx):
        return value % 2 == 0
```

#### RangeRule

- 単一範囲
- 単一 severity
- `low` と `high` はどちらも省略できます。片方を省くと片側だけの判定になります（例: `high` だけなら上限超えでのみ発報）。両方省くと決して発報しないため、`kanary lint` が warning を出します
- `lower_inclusive` / `upper_inclusive`
- `hysteresis`: 発報中だけ正常範囲を `hysteresis` 分狭めます。上限 `100`、`hysteresis = 5` なら、`100` を超えて発報した後は `95` 以下に戻るまで `FIRING` のままです。`high - low` の半分を超える値は、alert が戻れなくなるため例外になります
- `fire_when = "outside"`（既定）は `[low, high]` を正常とし、`fire_when = "inside"` は禁止帯域として扱い、入ったら発報します（共振帯域など）。このとき inclusive は端が禁止帯域に含まれるかを表し、hysteresis は発報中に帯域を広げます
- 複数 input に一致した場合は各 input を独立に評価し、どれか 1 つでも範囲外なら `FIRING`。hysteresis は rule 単位で、発報中は全 input に狭めた範囲を使います
- 数値でない値の input は `missing_inputs` に入り、発報しません
- 継承時に使える method: `check(value, ctx)`、`breaches(value, margin)`、`range_text()`

#### DifferenceRule、KOfNRule、AggregateRule、CountRule

複数の input（通常は glob で選ぶ）を扱う rule です:

- `DifferenceRule`: 最大 - 最小が `max_difference` を超えたら発報（冗長な sensor の突き合わせ）。値のある input が `min_inputs`（既定 2）未満なら OK のまま。
- `KOfNRule`: RangeRule と同じ範囲（`low`、`high`、`fire_when`、inclusive）を外れた input が `k` 個以上なら発報。`count_missing = True` で値の無い input も数える。
- `AggregateRule`: input の `"sum"`、`"mean"`、`"median"`、`"min"`、`"max"` を RangeRule と同じ範囲で判定（hysteresis 付き）。
- `CountRule`: 値のある input の数が `min_count` 未満か `max_count` 超なら発報。

#### ExpectedValueRule

input の値が `expected` でなければ発報します。`expected` は 1 つの値か `[200, 204]` のような list です。数値は値で比べ（`1 == 1.0 == True`）、`"RUNNING"` のような文字列も使えるので、status code、0/1 の flag、状態など範囲では読みにくいものに向きます。`fire_when = "equal"` にすると、逆に列挙した値で発報します（error code など）。

#### StaleRule

- 各 input の timestamp から古さを判定
- 複数 input に一致した場合は、どれか 1 つでも stale または timestamp 欠落なら `FIRING`
- timestamp が `future_tolerance` 秒（default は `30 * kanary.minute`）より先の未来なら、"(time zone?)" 付きの `WARN` で `FIRING`。そういう input は決して stale に見えず、たいていは time zone の無い時刻が原因です。`future_tolerance = None` で切れます
- payload の `values` は input 名から経過秒数への dict

通常、Ruleは関連Sourceの更新時に評価されます。正の秒数（`"30s"`や`"5m"`のような文字列でもよい）を`evaluation_interval`へ指定すると、それとは独立した固定間隔でも評価します。

```python
@kanary.rule(
    rule_id="kanary.peer.heartbeat.stale",
    inputs="kanary.peer:heartbeat",
    evaluation_interval=30.0,
)
class PeerHeartbeatStale(kanary.StaleRule):
    timeout = 120.0
```

定期評価は保存済みSource snapshotを使い、Sourceのpollや状態更新を行いません。複数SourceのRuleでは`ctx.inputs()`または`ctx.source_payload("source-id")`を使います。id なしの`ctx.source_payload()`は、何がきっかけの評価でもエラーです。wildcardはsnapshotに存在するinputだけを選択し、必須input集合を宣言しません。必須inputはcustom Rule内で明示的に確認してください。

#### RateRule

- 各 input の今回と前回の値・timestamp から rate を計算し、RangeRule と同じ範囲（hysteresis 含む）で判定
- rate は毎秒あたり。範囲は時間の単位で書く（例: 毎分 1 なら `high = 1.0 / kanary.minute`）。message は大きい方の境界の単位で rate を表示する
- 前回の sample が無い、数値でない input は `missing_inputs` に入り、発報しません
- 「前回」は source の前回の poll です。poll が同じ sample をもう一度返した（timestamp が進んでいない）場合は前回判定した rate をそのまま使うので、新しい値が来ないだけで alert が戻ることはありません。新しいものが無いとき source が `kanary.no_update()` を返してもよいです
- 2 点からの rate は noise を増幅します。分解能 `q` の値を `Δt` 秒ごとに読むと rate は `q / Δt` 刻みになり、ランダムな noise は約 `√2 / Δt` 倍になります。範囲はそれより十分外に取るか、`kanary.Buffer` の Series の window `slope()` を input として出して `RangeRule` を掛けます
- payload の `values` は input 名から rate への dict

#### ThresholdRule

- 多段階 severity
- `direction = "high" | "low"`
- `thresholds = [(value, severity), ...]`
- `hysteresis`: 発報中だけ、今の severity 以下の threshold を `hysteresis` 分戻します。severity の低下と復帰は遅れますが、より高い severity へ早く上がることはありません
- 複数 input に一致した場合は、どれか 1 つでも threshold に一致すれば `FIRING` し、その中の最高 severity を採用
- 継承時に使える method: `check(value, ctx)`、`level(value, margin)`

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
- remote alert を 1 つずつ input として返す（OK でない間は 1。metadata に alert そのもの）
- remote 側の `ack`, `unack`, `silence`, `unsilence` を helper として転送できる
- 通常は `base_url` と `interval` を設定します: `base_url = "http://remote-kanary.example:8000"`（node の address。`url` は同じものの別名で、`/export-alerts` で終わる URL はそれを除いて使います）

#### RemoteAlarm

- 1 個の remote alert を local rule に mirror する
- `propagate_ack`, `propagate_silence` により operator action を remote へ転送できる。ローカルの ack や silence は必ず成立します。相手に届かない場合（障害中は相手こそ落ちていることがある）は、API の応答、`kanaryctl`、viewer に warning が出ます。相手側で cancel できなかった silence は、相手側では end time に終わります

#### import_remote_alarms

- `remote_alarm_ids`（必須）の id ごとに `RemoteAlarm` rule を 1 つ生成する factory
- id は意図して明示する形にしています。相手の node から読むと、file の読み込みが相手の稼働に左右されます。停電の後は両方の node が同時に起動します
- `prefix`、`suffix`、`add_tags`、`owner`、`severity`、`include_rule_ids` / `exclude_rule_ids`（列挙した id に対する glob pattern）を使える
- local の rule id は `prefix.remote_id.suffix` です: `prefix="remote"` なら `remote.daq.link`（`prefix="remote."` でも同じ）
- 生成された rule はそれぞれ独立した local rule として扱われる。remote の alert が発報している間は、その severity と message が表示される

### Output 側

#### MailOutput

- SMTP でメールを送る helper class
- `smtp_host`、`smtp_port`、`sender`、`recipients`（list か、1 つの文字列 `"ops@example.org, oncall@example.org"`）を設定し、login が必要なら `smtp_username` と `smtp_password` も設定する
- port 587 や 25 では STARTTLS を使います（TLS の無い relay なら `use_starttls = False`）。port 465 では最初から TLS で接続します（他の port でそうするなら `use_ssl = True`）
- `smtp_password` は plugin file に書かない: `smtp_password = os.environ.get("SMTP_PASSWORD")`（または `kanary.load_json()`）
- mail には plain text と、chat の output と同じ内容（色付きの見出し、message、field の表）の HTML が入る。`subject(event)`、`body(event)`、`html(event)`（`None` を返すと plain text だけ）を上書きでき、`viewer_url` で viewer への link を付けられる

例:

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

`recipients_for(event)` を override すると event ごとに宛先を選べます（例: `OnCallSchedule`）。その場合 `recipients` は空のままで構いません。

#### WebhookOutput、SlackOutput、MattermostOutput、DiscordOutput

`kanary.WebhookOutput` は `payload(event)` を JSON として `url` に `headers` と `timeout` 付きで POST し、HTTP error なら raise して retry させます。既定の payload は `rule_id`、`state`、`previous_state`、`transition`、`severity`、`message`、`owner`、`tags`、`occurred_at`、`active_since`、`acked_by` です。`text(event)` は 1 行の要約です。

webhook URL は通常 secret なので、上の plugin config の節のように `url = os.environ.get("WEBHOOK_URL")` と書きます。

`SlackOutput`、`MattermostOutput`、`DiscordOutput` は、各 service の形式で rich な message を送ります: 色の帯（OK は緑、silence / suppress 中は灰色、それ以外は severity ごとに INFO 青、WARN 黄、ERROR 赤、CRITICAL 紫）、`🔴 FIRING ERROR oil.temperature` のような title、rule の message、state・severity・since・ack・owner・tag・判定した値の field です。再起動の後、rule がまだ評価される前に送る event（KANARY が止まっている間に silence が終わった場合など）には、値の時刻と「再起動の前」を示す `Evaluated` field が付きます。通知や preview には 1 行の `text(event)` が出ます。`viewer_url` を設定すると title が KANARY viewer への link になります。`MattermostOutput` と `DiscordOutput` は `username`、`MattermostOutput` は `icon_url` も設定できます。別の形にするなら `payload()` を上書きします。

`kanary.describe(event)` はこれらの部品を `kanary.Notice`（`title`、`headline`、`symbol`、`color`、`color_hex`、`message`、`fields`、`time`、`text`、`link`）として返します。Teams や pager など自分の output や、上書きした `payload()` で使えます。

#### FileOutput と CommandOutput

- `FileOutput` は event ごとに 1 行の JSON を `path`（plugin file からの相対）に追記します。`include_payload = True` で rule の payload も書きます。形を変えるなら `record(event)` を上書きします。既定の中身は `kanary.event_record(event)` です。
- `CommandOutput` は event ごとに `command` を実行します。stdin に event の JSON、環境変数に `KANARY_RULE_ID`、`KANARY_STATE`、`KANARY_PREVIOUS_STATE`、`KANARY_TRANSITION`、`KANARY_SEVERITY`、`KANARY_MESSAGE`、`KANARY_OWNER`、`KANARY_TAGS` を渡し、exit code が 0 以外なら raise します。

#### QuietHours

`kanary.QuietHours` は静かな時間帯の alert event を保留し、後で送る helper です。`OutputFollowups` と同じく、output が `init()` で作り、`terminate()` で閉じ、`emit()` から呼びます。使わない output には影響しません。

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

- `send(event, callback)` は、event が時間帯（`("HH:MM", "HH:MM")`。日付をまたいでもよい。`timezone` で解釈。`"24:00"` はその日の終わりの 0 時。1 つだけなら list にしなくてよい）に入っていて `minimum_severity`（他と同じ書き方: `kanary.CRITICAL`、`"critical"`、`40`）未満でない限り、すぐ `callback(event)` を呼びます。一日中を覆う時間帯はエラーです。それには output の `minimum_severity` を使います。
- 該当する event は静かな時間帯が終わるまで保留します。続けて並んだ時間帯は 1 つとして扱います。終わったときに何を送るかは `release` で選びます: `"all"`（既定）は保留した event を全部、元の順番で `callback` に渡します。`"latest"` は rule ごとに最新の 1 件だけ。`"summary"` は全部をまとめて `summary(events)` を 1 回呼び、1 通の message にします（`SlackOutput`、`MattermostOutput`、`DiscordOutput`、`WebhookOutput`、`MailOutput` は `emit_summary(events)` を持っています。保留が 1 件だけならいつもどおり `callback` に渡します）。`defer=False` にすると保留せず捨てます。
- ある rule の event をすぐ送るとき（夜間の `CRITICAL` への escalation など）、`"all"` ではその rule の保留分を先に送るので、受け手から順番が逆に見えることはありません。`"latest"` と `"summary"` では、今送る event の方が新しいので保留分は捨てます。
- `timezone` は `kanary.to_timezone()` が読めるものです: IANA 名（`"Asia/Tokyo"`）、offset（`"+09:00"`、`"+9"`、`"UTC+9"`）、`"UTC"`。省略すると host の local time です。
- 保留分は output の thread で、`emit()` と `emit()` の間に送ります。そこで失敗した場合は log に出すだけで retry しません。すぐ送るときの失敗はそのまま例外になるので、engine が通常どおり `emit()` を retry します。
- 保留は memory 上にあり、`close()`、reload、再起動で失われます（log に warning を出します）。`held()` で一覧でき、`applies(event)` で保留対象かどうかを判定できます。

`quiet-hours` template を参照してください。

#### OnCallSchedule

`kanary.OnCallSchedule(shifts, timezone=None, default=())` は当番を返します: `schedule.recipients(at)`。各 shift は mapping で、通常は `kanary.load_toml()` で読みます:

- 曜日指定: `weekdays`（略しても略さなくてもよい名前 `["mon", "Tuesday"]`、範囲 `"mon-fri"`、`"mon,wed,fri"`。不明な名前はエラー）、`from`、`to`（日付をまたいでもよい。`"24:00"` は 0 時まで。一日中なら両方とも書かない）、`recipients`（list か、カンマ区切りの 1 つの文字列）
- 日付指定: `start`、`end`、`recipients`。曜日指定より優先

どの shift にも当たらないときは `default`（list か、カンマ区切りの 1 つの文字列）を返します。`oncall` template が `MailOutput.recipients_for()` と `QuietHours` を組み合わせた例です。

### 不正な設定

helper class は、設定が不正なら、その設定を使う時点で設定名入りの `ValueError` を投げます。source は `init()` か `poll()`、output は `init()` か `emit()`、rule は `evaluate()` です。その plugin は他の plugin error と同じく失敗扱いになり、message が status に出ます。他の plugin は動き続けます。そのため、設定を `init()` で（例えば `kanary.load_toml()` から）入れることもできます。

## 5. user-defined factory

Kanary では、繰り返しパターンごとに本体側へ built-in factory を追加しなくても、ユーザーが plain Python で factory 関数を書けます。

自然な書き方は次です。

1. `type(...)` で class を動的に作る
2. 必要な class 変数や method を入れる
3. `kanary.source(...)`, `kanary.rule(...)`, `kanary.output(...)` を適用して登録する

こうして生成した plugin は、登録後は普通の独立した plugin として扱われます。

たとえば:

- input 名と値の dict から 1 個の source を生成する
- input の list から複数の `ThresholdRule` を生成する

といった使い方ができます。

具体例は [examples/factory_patterns.py](../examples/factory_patterns.py) にあります。

この example には次が入っています。

- `make_constant_source(...)`
  input 名と値の dict から単純な source class を生成します。
- `make_threshold_rule(...)`
  `ThresholdRule` ベースの rule class を 1 つ生成します。

## 6. self-monitoring pattern

`kanary new -t plugin-health` で、この KANARY 自身の plugin を見張る source と rule 2 つが出てきます。普通の plugin なので、他と同じように置いて編集します。

- `kanary.PluginHealthSource` は自分が動いている engine を直接読み（HTTP も設定も不要）、他のすべての plugin について `<type>.<plugin_id>.failed`（`FAILED` の間 1。error は metadata）を出します。output には `output.<id>.queued` と `output.<id>.dropped`（前回の poll 以降に捨てた event 数）、rule には `rule.<id>.slow` も出します。
- template の rule は、どれかの plugin が失敗したとき（それぞれの error を列挙）と、output が event を捨てたときに発報します。

失敗した output についての alert は、他の output から届きます。output が 1 つだけなら viewer にしか出ません。KANARY 自体が止まったことは内側からは分からないので、別の KANARY から見張るか（`peer` template）、止まると知らせてくれる外部の service に heartbeat を送ります（`heartbeat` template）。

ある project の中だけで使う繰り返しなら、この方法で十分なことが多いです。複数 deployment で同じパターンが繰り返し必要になった時点で、built-in helper を追加するかを検討するのが自然です。

## 状態と依存関係

rule 間の関係: `suppressed_by` と `depends_on` には、この rule の異常を説明する rule を指定します（id 1 つか list）。そのどれかが発報している間、この rule は `SUPPRESSED` になります。`SUPPRESSED` は default の `exclude_states` に含まれるので、通常の output には届きません。DAQ の link が落ちたときに、届くのは link の alert だけで、その先の channel ごとの alert は届きません。

| | 親が発報中の `evaluate()` | 表示 |
|---|---|---|
| `suppressed_by` | 呼ぶ | `SUPPRESSED`。rule 自身の判定も付く: `suppressed by daq.link; would fire: daq:hv1=0 out of range [1000, 2000]` |
| `depends_on` | 呼ばない | `SUPPRESSED`: `blocked by daq.link (not evaluated)` |

値を 1 つずつ判定する rule（`RangeRule`、`ThresholdRule` など）には `suppressed_by` を使います。viewer に、その rule が何と言うかが出ます。親が発報している間は `evaluate()` が動かない、または値を見せたくない rule には `depends_on` を使います。自分で履歴を持つ rule（rule の中の `kanary.Buffer` など）は、障害中に読んだ値を持ち続けて、障害の後に発報してしまうからです。

- 「発報している」は親自身の判定です。親を silence や ACK しても、子は抑えられません。
- 親は、それが抑える rule より先に評価されます。同じ source なら、rule を書いた順によらず、届くのは親の alert だけです。
- 親が発報すると、別の source の rule もすぐに `SUPPRESSED` になります。
- 親が復旧すると、rule は、読んでいるすべての source から復旧後の値が来た時点で戻ります。同じ source なら同じ poll で、別の source ならそれらの次の poll で戻ります（それまでは `recovered; waiting for a new value` と表示）。source A と B を読む rule は、A だけが新しい値を返した時点ではまだ抑制されたままです。障害中に読んだ値で判定し直すことはしません。まだ一度も値を返していない source には、障害中の値もありません。
- rule 自身の source が、親の source より先に異常を拾った場合は、rule の alert が先に届きます。親は、それが抑える rule と同じかそれ以上の頻度で読まれるようにすると確実です。
- 親が発報中に `evaluate()` が例外を出しても、rule は `SUPPRESSED` のままです（status に error が出ます）。
- 自分自身を指定した rule や、互いに指定し合う循環はエラーです。存在しない rule 名は lint の警告になります。

alert state:

- `OK`
  現在の評価結果が正常。
- `FIRING`
  現在の評価結果が異常。
- `ACKED`
  異常は継続しているが、operator が確認済み。ack は 1 つの alert に属します。rule 自身がその alert の終わりを判定した時点で、silence や親に隠れていても終わり（履歴には `system` による unack が残ります）、次の alert が前回の ack のまま表示されることはありません。
- `SILENCED`
  active な silence の対象になり、期間中は通常の alert state が一時的に mute されている。
- `SUPPRESSED`
  `suppressed_by` か `depends_on` にある rule が発報しているため、この alert は抑えられている。

派生 transition:

- `UNACK`
  acknowledged された alert を再オープンした時 (`ACKED -> FIRING`)。
- `ESCALATED`
  state を変えずに severity が上がった時。
- `DEESCALATED`
  state を変えずに severity が下がった時。

実運用では:

- `SILENCED` は viewer や history で「意図的に止めている」ことを示します。Output で受け取りたい場合は `exclude_states` から明示的に外します。
- reload 中の rule removal は alert state では表しません。履歴上は `action_type = "rule_removed"` の operator action として残ります。
