# Getting Started

この文書では、Kanary を使って小さな監視を 1 つ作り、`Source -> Rule -> Output` の流れを手を動かしながら理解します。

ここで使うコードは [examples/getting_started.py](../examples/getting_started.py) にまとまっています。

## 1. Install と起動

```bash
pip install kanary
kanary ./examples
```

別マシンから見たい場合:

```bash
kanary ./examples --api-host 0.0.0.0 --api-port 8000
```

## 2. Viewer

```text
http://127.0.0.1:8000/viewer
```

## 3. Source を作る

この文書では、ローカルマシンの load average を読む source を例にします。  
対応する実コードは [examples/getting_started.py](../examples/getting_started.py) にあります。

```python
from datetime import datetime, timezone
import os

import kanary


@kanary.source(source_id="local_load", interval=10 * kanary.second)
class LocalLoadSource:
    def poll(self):
        load1, _, _ = os.getloadavg()
        cpu_count = os.cpu_count() or 1
        return kanary.inputs([
            (
                "load1_per_cpu",
                load1 / cpu_count,
                datetime.now(timezone.utc),
                {"raw_load1": load1, "cpu_count": cpu_count},
            ),
        ])
```

最小の source interface は次です。

- `@kanary.source(source_id="...")`
- `poll(self)`
- 通常は `kanary.inputs(...)` を返すこと
- 実際に空の snapshot を返したい時は `kanary.no_data(...)`、最後の snapshot で rule を再評価したい時は `kanary.no_update(...)`、明示的な no-op は `kanary.skip(...)`

`interval` は source の取得間隔です。省略すると 60 秒です。wall-clock に
合わせたい場合は、`*/5 * * * *` のような Unix cron 互換 5-field の
`schedule` も使えます。ただし `interval` と `schedule` の同時指定はしません。  
`init(self)` と `terminate(self)` も必要に応じて実装できます。

## 4. Rule を作る

次に、load average が高いときに alert を出す rule を追加します。

```python
@kanary.rule(
    rule_id="local_load.busy",
    inputs="local_load:load1_per_cpu",
    severity=kanary.WARN,
    tags=["getting-started", "demo"],
)
class LocalLoadBusy:
    description = "Alert when the 1-minute load average per CPU is high."
    runbook = "Run `uptime` or `top` on the monitored host."

    def evaluate(self, ctx):
        load = ctx.value()
        threshold = 0.50
        if load is None:
            return kanary.ok("load1_per_cpu is missing")
        return kanary.error_if(
            load > threshold,
            f"load1_per_cpu={load:.2f} is over {threshold:.2f}",
        ) or kanary.ok(
            f"load1_per_cpu={load:.2f} is within the normal range",
        )
```

最小の rule interface は次です。

- `@kanary.rule(rule_id="...", inputs="source_id:input_name")`
- `severity`
- `tags`
- `evaluate(self, ctx)`
- 通常は `kanary.ok(...)` または `kanary.firing(...)` を返すこと
- `owner`, `description`, `runbook` は任意 metadata

単一 input なら `ctx.value()`、複数 input なら `ctx.inputs()` を使えます。

## 5. helper class を使う

同じ考え方を `ThresholdRule` の helper class で短く書くこともできます。

```python
@kanary.rule(
    rule_id="local_load.busy_threshold",
    inputs="local_load:load1_per_cpu",
    severity=kanary.WARN,
    tags=["getting-started", "demo"],
)
class LocalLoadBusyThreshold(kanary.ThresholdRule):
    direction = "high"
    thresholds = [
        (0.50, kanary.WARN),
        (0.90, kanary.ERROR),
    ]
```

helper class を使うときは通常 `evaluate()` を書かず、class 変数を設定します。

- `StaleRule`: `inputs`, `timeout`
- `RangeRule`: `inputs`, `low`, `high`, `hysteresis`
- `RateRule`: `inputs`, `high`, `low`（毎秒あたり。例: `1.0 / kanary.minute`）
- `ThresholdRule`: `inputs`, `direction`, `thresholds`, `hysteresis`

環境が許せば, `openssl speed -multi 8` などのコマンドで負荷をかければ, アラームの発火をテストできます.

## 6. Output を作る

alert event を file に追記する output の例です。

```python
from pathlib import Path
import json

import kanary


@kanary.output(output_id="file", include_tags=["getting-started"])
class FileOutput:
    output_path = Path("getting_started_alerts.jsonl")

    def init(self):
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self.output_path.touch(exist_ok=True)

    def emit(self, event):
        record = {
            "rule_id": event.rule_id,
            "previous_state": event.previous_state.value if event.previous_state else None,
            "current_state": event.current_state.value,
            "previous_severity": (
                kanary.severity_label(event.previous_severity)
                if event.previous_severity is not None else None
            ),
            "current_severity": kanary.severity_label(event.current_severity),
            "transition": event.transition.value if event.transition else None,
            "owner": event.owner,
            "tags": list(event.tags),
            "message": event.message,
            "occurred_at": event.occurred_at.isoformat(),
        }
        with self.output_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
```

## 7. 何が起きるか

ここまで保存すると、Kanary は file の変更を検知して plugin を dirty にします。  
default の `--auto-reload off` では、反映は明示的に行います。

```bash
kanaryctl --base-url http://127.0.0.1:8000 reload --dirty
```

`--auto-reload dirty` または `--auto-reload all` で起動した場合だけ、自動で反映されます。  
viewer では source, rules, output が見えるようになります。alert event が起きると `getting_started_alerts.jsonl` に 1 行ずつ追記されます。

```bash
kanaryctl --base-url http://127.0.0.1:8000 alerts
```

viewer と `kanaryctl` は同じ API を使っています。

簡単な診断には、次のような CLI も使えます。

```bash
kanaryctl --base-url http://127.0.0.1:8000 test-poll local_load
kanaryctl --base-url http://127.0.0.1:8000 test-evaluate local_load.busy --print-template
kanaryctl --base-url http://127.0.0.1:8000 test-evaluate local_load.busy --payload-json '{"inputs":{"local_load:load1_per_cpu":{"value":0.95,"timestamp":"2026-05-29T00:00:00+00:00"}},"status":"ok"}'
kanaryctl --base-url http://127.0.0.1:8000 test-fire local_load.busy --state FIRING --reason "mail output check"
kanaryctl --base-url http://127.0.0.1:8000 reload --dirty
```

`test-evaluate` は fully-qualified input name を key にした `inputs` map を受け取ります。通常の rule 実装では、`ctx.value()` や `ctx.inputs()` などの accessor を使ってください。

## 8. 進んだ機能

### 履歴を持つ（kanary.Buffer）

10 分平均や傾向など、最近の sample から計算した値を出すには、`init()` で `kanary.Buffer` を作り（`self.buffer = kanary.Buffer(size=1024, max_age=3 * kanary.hour)`）、`poll()` で生の値を追加して（`self.buffer.add(("pressure", value))`。`kanary.inputs()` と同じ形）、`self.buffer["name"]` から計算した input を返します。例: `self.buffer["pressure"].window(10 * kanary.minute).mean()`。helper class を含むどの source でも使えます（`self.buffer.add(super().poll())`）。`kanary new --show buffered` を参照してください。

### Remote Kanary node の読み込み

他の Kanary node の alert を source として読み、local rule として mirror できます。

- [examples/peer_monitoring.py](../examples/peer_monitoring.py)
- [examples/remote_alarm_import.py](../examples/remote_alarm_import.py)

### Mail output と Mailpit

`kanary.MailOutput` を使うと SMTP 出力を短く書けます。ローカルで試すなら Mailpit が便利です。

```python
@kanary.output(output_id="mail", include_tags=["getting-started"])
class MailAlert(kanary.MailOutput):
    smtp_host = "127.0.0.1"
    smtp_port = 1025
    use_starttls = False
    sender = "kanary@example.test"
    recipients = ["operator@example.test"]
    subject_prefix = "[KANARY getting-started]"

```

```bash
docker run --rm -p 1025:1025 -p 8025:8025 axllent/mailpit
```

mail には plain text と、色付きの見出し、rule の message、state・severity・owner・tag・判定した値の表を持つ HTML の両方が入ります。変えるときは `subject(event)`、`body(event)`、`html(event)`（`None` を返すと plain text だけ）を上書きします。`viewer_url` を設定すると viewer への link が付きます。

Web UI:

```text
http://127.0.0.1:8025
```

### ACK / Silence

kanaryはアラームを誰かが見たという情報を `ACK`というステータスで管理します.
また, 一時的にアラームをオフにしたい時は`SILENCED` というステータスをつけ, `Output` pluginなどの抑制に用いることができます.
thin clientからは, 以下のように実行できます.

```bash
kanaryctl --base-url http://127.0.0.1:8000 ack local_load.busy --operator operator_name --reason "investigating"
kanaryctl --base-url http://127.0.0.1:8000 silence-for --operator operator_name --minutes 10 --rule 'local_load.*'
```
`ACK`されていても, アラームの状態がOKにもどれば自動的に解除されます.

scheduled silence は、Source の次回 poll を待たずに開始・終了時刻で `SILENCED` を更新します。境界では poll を追加実行せず、silence 中を含む最後の通常 Rule 評価結果へ戻します。process を再起動してから対象 Rule がまだ一度も評価されていない場合だけ、最初の poll まで state は更新されません。

### Template から plugin を作る

`kanary new` は template から plugin file を作ります。各 template は動く plugin で、すべての option を comment 付きで示しているので、reference としても使えます。`kanary new --list`（または `kanary new --help`）で template の一覧表が出ます。Source / Rule / Output のどれを作るかは template 名で決まります。

```bash
kanary new --list
kanary new -t source postgres.wide -o ./plugins/
kanary new -t range oil.temperature.high -o ./plugins/postgres_wide.py -a
kanary new -t followups oncall --stdout
kanary new --show ping
```

- 一覧は BASIC（よく使う出発点で、helper class 1 つでほぼ設定だけ: `command`、`ping`、`tcp`、`http-status`、`file`、`http-json`、`prometheus`、`sql-wide`、`sql-long`、`http-push`、`mqtt`、`plugin-health`、`range`、`threshold`、`expected`、`stale`、`webhook`、`slack`、`mattermost`、`discord`、`file-log`、`mail`、素の `source` / `rule` / `output`）と ADVANCED（使う場面が限られる helper、独自 logic、複数機能の組み合わせ: `ssh`、`log-count`、`push`、`buffered`、`trend`、`spike`、`frozen`、`remote`、`rate`、`difference`、`k-of-n`、`aggregate`、`count`、`multi-source`、`command-output`、`followups`、`delayed`、`quiet-hours`、`oncall`）に分かれています。
- `--show TEMPLATE` は何も書き出さずに template を表示します。全 template を目次付きで 1 ページにまとめたものが [docs/templates_ja.md](templates_ja.md) にあります。
- template の実 file は `src/kanary/templates/` にあります。
- `-o` には file か、末尾 `/` の directory を指定します。省略時は current directory の `<plugin_id>.py` です。PLUGIN_ID を `.py` で終えるとそれが file 名になり、拡張子を除いた部分が id になります。
- 既存 file を黙って上書きすることはありません。`-a` で追記（import は統合）、`--force` で上書きします。

### Lint
単純なlinterが用意されています. pluginをpushする前に, 単純な確認を行うことが可能です.
```bash
kanary lint ./examples
```

## 開発用インストール

Kanary 自体を開発するときは source checkout を使います。

```bash
git clone https://github.com/mzks/kanary
cd kanary
uv sync
uv run python -m kanary ./examples
```
## 9. 次に読むもの
- [README_ja.md](../README_ja.md)
- [plugins_ja.md](plugins_ja.md)
- [operations_ja.md](operations_ja.md)
- [api_ja.md](api_ja.md)
