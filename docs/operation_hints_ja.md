# Plugin の実装ヒント

Kanary を通知・警報システムとして使う時に、plugin を安全に追加し、運用で困りやすい点を避けるための補足です。正式な API の一覧は [plugins_ja.md](plugins_ja.md)、運用コマンドは [operations_ja.md](operations_ja.md) を参照してください。

## Test 環境

plugin は Source / Rule / Output ごとに独立して load・実行されます。ある plugin の失敗が直ちに他の plugin を停止させることはありません。ただし、同じ外部 API、state DB、通知先、共有ファイルを使う plugin 同士は干渉し得ます。特に、誤った Rule が短い周期で発火し、Output が通知先(の人間)を過負荷にすることは Kanary 自身では防げません。

まず plugin directory を load できるか確認します。

```bash
kanary lint ./plugins
```

`lint ok` は「directory を import して定義を検査できた」ことを示します。外部接続、通知内容、閾値、decorator の typo を含むすべての動作を保証するものではありません。plugin module の import 時には任意の Python code が実行されるため、外部接続や書込みは `init()` / `poll()` / `emit()` に置き、module top level では行わないでください。

本番と同じ plugin と入力を使って routing を確認するには、別 port・別 node ID で shadow instance を起動します。

```bash
kanary ./plugins \
  --api-port 8001 \
  --node-id production-shadow \
  --no-output-emit
```

`--no-output-emit` では Output plugin の load、`init()`、routing filter の評価は行いますが、`emit()` は呼びません。Source poll、Rule 評価、state change、operator action は通常どおり動きます。本番と並べる場合は、同じ state DB を共有しないでください。`--state-db` に別 path を指定するか、state DB を指定せず `KANARY_SQLITE_PATH` も設定しない状態で起動します。

個別に確認する時は `kanaryctl` を使えます。

```bash
kanaryctl test-poll source_id
kanaryctl test-evaluate rule_id --print-template
kanaryctl test-fire rule_id --state FIRING --reason "output check"
```

`test-fire` は synthetic event を Output routing に流します。本番 API に対して実行すれば、通常の Output は実際に通知するため注意してください。

## Plugin での時間単位

Kanary の時間は秒の `float` です。単位定数を使うと設定値の意図を読みやすくできます。

```python
import kanary
from kanary import minute as m


@kanary.source(source_id="example", interval=10 * m)
class ExampleSource:
    ...
```

`nanosecond`、`microsecond`、`millisecond`、`second`、`minute`、`hour`、`day` が利用できます。

## 設定と lifecycle

接続先、timeout、通知先のような環境ごとの設定は Python source に埋め込まず、`kanary.load_toml()`、環境変数、systemd の `EnvironmentFile` などに置きます。token や password は repository に置かないでください。`.py` 以外の config file は auto reload の監視対象ではないため、変更後は明示 reload または process restart が必要です。

Source と Output では、接続や thread を `init()` で作り、`terminate()` で閉じます。custom Rule の `__init__()`、Source / Output instance の `self` に置いた cache や timer は process restart と plugin reload で失われます。永続化が必要な運用上の状態は、外部 store に明示的に保存します。

## Kanary 自体の監視

Kanary の監視には、別 node が peer の health を監視する構成が使えます。[peer_monitoring.py](../examples/peer_monitoring.py) は peer の heartbeat、API latency、failed plugin 数を通常の input として取り込む例です。

主系 node は業務 Source / Rule / Output を持ち、監視系 node は少数の peer 監視 Rule と、その通知用 Output だけを持つ構成が扱いやすくなります。相互監視にする場合も、監視経路が同じ network・電源・通知先へ依存しないかを確認してください。Kanary は peer の自動多重化や合意形成を行いません。

他 node の alert 自体を Rule として取り込む用途には `RemoteKanarySource` と `RemoteAlarm` があります。監視経路が必要なら、transport failure を Source failure として扱う peer monitoring と分けて考えると原因が追いやすくなります。

## Source

### input の返し方

通常は `kanary.inputs(...)` を返します。input 名、値、観測時刻を明示すると、StaleRule や履歴を正しく使えます。

```python
def poll(self):
    return kanary.inputs(
        ("temperature", 23.4, measured_at),
        ("fan_rpm", 1200, measured_at, {"unit": "rpm"}),
    )
```

`dict[name, value]`、tuple/list の input 形式も使えますが、時刻・metadata・空データ時の意味を扱うなら `kanary.inputs(...)` が明示的です。

- `kanary.no_data(reason=...)`: poll は成功したがデータが無い
- `kanary.no_update(reason=...)`: 前回値を更新しない
- `kanary.skip(reason=...)`: 今回の poll を評価対象にしない

外部 API の一時的な通信失敗は、通常は例外として送出します。Kanary が Source の retry / reinit と plugin status を扱います。失敗を `no_data()` に変換すると、通信失敗と正常な空結果を区別できなくなります。

### 外部 I/O、timestamp、poll 間隔

HTTP、DB、socket などの外部 I/O には必ず timeout を設定します。timeout が無いと 1 回の poll が不定に止まり、次回の評価も遅れます。

`Measurement.timestamp` には、可能なら取得時刻ではなく監視対象が実際に観測した時刻を渡します。Kanary が poll した時刻を入れると、古いデータを取得しても新しい値に見え、StaleRule や rate の判断が不正確になります。観測時刻を取得できない場合だけ、poll 時刻を使います。

`interval` は外部システムへの負荷と検知遅延の両方を決めます。たとえば 1 分 interval の Source では、5 分継続を判定する Rule は最短で 5 分後、通常はその次の poll まで遅れて発火します。監視対象の異常は通常の input と Rule で表し、監視 plugin 自身が取得できない時は例外として Source failure に分けます。

## Rule

### Rule をまとめる粒度

「1 変数 = 1 Rule」にする必要はありません。共通の owner、runbook、発火条件、通知方針を持つ input は、custom Rule でまとめると読みやすくなります。一方、個別に ACK、silence、suppression、通知先を変えたい対象は別 Rule にします。

複数 input を扱う custom Rule では `ctx.value()` を推測で使わず、`ctx.inputs()` を反復します。selector が複数 input に一致する場合、`ctx.value()` は error になります。

### owner、tag、runbook

`owner` は対応責任を持つ人または team、`runbook` は対応手順、tag は routing・検索・silence・分類のための属性です。たとえば `owner="expert_db"`、`tags=["database", "production"]`、`runbook="https://..."` のように役割を分けます。

通知先ごとに tag を無制限に増やすより、少数の安定した分類 tag と Output の `include_tags` / `exclude_tags` を組み合わせる方が保守しやすくなります。個別の連絡先は Output 側の設定として持つのが基本です。

### Rule の hysteresis

`RangeRule` と `ThresholdRule` の `hysteresis` は、境界付近の揺れによる `FIRING` と `OK`、または severity の上下の反復を抑える値の余裕幅です。発火する境界は変えず、すでに異常になった後だけ、値が正常側へ `hysteresis` 分だけ戻るまで復帰・severity 低下を保留します。

たとえば上限 `20`、`hysteresis = 1` の RangeRule では、値が `20` を超えると発火し、発火後は値が `19` 以下になるまで `FIRING` を維持します。これは時間待ちではありません。

```python
class Temperature(kanary.RangeRule):
    high = 20
    hysteresis = 1
```

asymmetric な復帰境界や複雑な recovery 条件が必要なら、helper の実装を参考に custom Rule として書きます。

### 時間条件を custom Rule で書く

「異常が 5 分続いたら発火する」「正常が 2 分続いたら復旧する」は、現在は custom Rule の instance state と `ctx.now` で書けます。次の poll で評価するため、5 分ちょうどではなく、その後の最初の poll で遷移します。

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

`raw_firing` を自前で持つのは、ACKED / SILENCED / SUPPRESSED が外側から適用される state であり、入力が異常かという判定とは別だからです。この timer は process restart / plugin reload でリセットされます。また待機中に専用の `PENDING` state はありません。必要なら `OK` / `FIRING` の message に待機理由を残します。

## Output

### 状態遷移の通知を絞る

Output plugin は default で `exclude_states=["SUPPRESSED", "SILENCED"]` です。したがって通常は `FIRING`、`OK`、`ACKED` を受け取り、復旧も通知します。

`SILENCED -> OK` は current state が `OK` なので default では除外されません。この遷移だけ不要なら `emit()` で判定します。

```python
def emit(self, event):
    if event.previous_state == kanary.SILENCED and event.current_state == kanary.OK:
        return
    # Deliver the event.
```

すべての復旧通知を除外するなら、default を拡張して書きます。

```python
exclude_states = kanary.Output.exclude_states + ["OK"]
```

`exclude_states` を decorator で指定した場合は置き換えです。debug / audit 用に全 state を受け取りたい時は `exclude_states=[]` を明示します。

### Severity による通知先の分離

Rule は decorator の default severity を、評価ごとに `kanary.firing(..., severity=...)` などで上書きできます。たとえば WARN 以上を担当者へ、ERROR 以上を当番 group へ、CRITICAL を mailing list へ送る Output を別々に用意できます。

```python
@kanary.output(output_id="on-call", minimum_severity="ERROR")
class OnCallOutput:
    def emit(self, event):
        ...
```

これは状態・severity の変化に応じた通知です。誰も対応しない時間に応じて通知先を広げる followup とは別の仕組みです。

### delivery の再試行と重複

`emit()` が例外を送出すると、Kanary は Output の retry / reinit 設定に従って再試行します。外部 service へ書込みや ticket 作成を行う Output は、同じ event が複数回届いても問題が起きないようにします。外部 API が idempotency key を受け取れるなら、`rule_id`、`occurred_at`、state / transition などから安定した key を作るのが安全です。

一方、失敗を握り潰して `emit()` が正常終了すると Kanary は delivery 成功として扱います。通知不能を retry したい時は、十分な context を付けて例外を送出します。短時間に同じ通知を繰り返さない設計は、Rule の hysteresis・時間条件と Output の routing / followup を組み合わせて行います。

### 通知の followup

`kanary.OutputFollowups` は、最初の通知から一定時間後も ACK / silence / recovery が無ければ別の action を呼ぶ、process 内の helper です。

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

`now()` は現在の event を渡して同期実行します。callback の例外は `emit()` に伝播するため、通常の Output retry / reinit の対象です。`after()` は最初の followup 登録時刻からの絶対 offset で実行され、callback にはその時点で最後に受け取った event が渡されます。

`SILENCED` と `SUPPRESSED` で followup を取り消すには、それらの event を受け取る必要があるため `exclude_states=[]` を指定しています。followup は memory 上だけに保持され、process restart / plugin reload では失われます。`terminate()` で必ず `close()` を呼びます。完全な例は [output_followups.py](../examples/output_followups.py) にあります。
