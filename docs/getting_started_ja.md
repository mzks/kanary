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
    def poll(self, ctx):
        load1, _, _ = os.getloadavg()
        cpu_count = os.cpu_count() or 1
        return kanary.SourceResult(
            measurements=[
                kanary.Measurement(
                    name="load1_per_cpu",
                    value=load1 / cpu_count,
                    timestamp=datetime.now(timezone.utc),
                    metadata={"raw_load1": load1, "cpu_count": cpu_count},
                ),
            ],
            status="ok",
        )
```

最小の source interface は次です。

- `@kanary.source(source_id="...")`
- `poll(self, ctx)`
- `kanary.SourceResult(...)` を返すこと

`interval` は source の取得間隔です。省略すると 60 秒です。  
`init(self, ctx)` と `terminate(self, ctx)` も必要に応じて実装できます。

## 4. Rule を作る

次に、load average が高いときに alert を出す rule を追加します。

```python
@kanary.rule(
    rule_id="local_load.busy",
    source="local_load",
    severity=kanary.WARN,
    tags=["getting-started", "demo"],
    owner="demo_owner",
)
class LocalLoadBusy:
    description = "Alert when the 1-minute load average per CPU is high."
    runbook = "Run `uptime` or `top` on the monitored host."

    def evaluate(self, payload, ctx):
        load = ctx.value("load1_per_cpu")
        threshold = 0.50
        if load is None:
            return kanary.Evaluation(
                state=kanary.AlertState.OK,
                payload=payload,
                message="load1_per_cpu is missing",
            )
        if load > threshold:
            return kanary.Evaluation(
                state=kanary.AlertState.FIRING,
                payload=payload,
                message=f"load1_per_cpu={load:.2f} is over {threshold:.2f}",
            )
        return kanary.Evaluation(
            state=kanary.AlertState.OK,
            payload=payload,
            message=f"load1_per_cpu={load:.2f} is within the normal range",
        )
```

最小の rule interface は次です。

- `@kanary.rule(rule_id="...", source="...")`
- `severity`
- `tags`
- `evaluate(self, payload, ctx)`
- `kanary.Evaluation(...)` を返すこと

`ctx.value("load1_per_cpu")` のように measurement を名前で読めます。

## 5. helper class を使う

同じ考え方を `ThresholdRule` の helper class で短く書くこともできます。

```python
@kanary.rule(
    rule_id="local_load.busy_threshold",
    source="local_load",
    severity=kanary.WARN,
    tags=["getting-started", "demo"],
    owner="demo_owner",
)
class LocalLoadBusyThreshold(kanary.ThresholdRule):
    measurement = "load1_per_cpu"
    direction = "high"
    thresholds = [
        (0.50, kanary.WARN),
        (0.90, kanary.ERROR),
    ]
```

helper class を使うときは通常 `evaluate()` を書かず、class 変数を設定します。

- `StaleRule`: `measurement`, `timeout`
- `RangeRule`: `measurement`, `low`, `high`, `hysteresis`
- `RateRule`: `measurement`, `per_seconds`, `high`, `low`
- `ThresholdRule`: `measurement`, `direction`, `thresholds`, `hysteresis`

環境が許せば, `openssl speed -multi 8` などのコマンドで負荷をかければ, アラームの発火をテストできます.

## 6. Output を作る

state change を file に追記する output の例です。

```python
from pathlib import Path
import json

import kanary


@kanary.output(output_id="file", include_tags=["getting-started"])
class FileOutput:
    output_path = Path("getting_started_alerts.jsonl")

    def init(self, ctx):
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self.output_path.touch(exist_ok=True)

    def emit(self, event, ctx):
        record = {
            "rule_id": event.rule_id,
            "previous_state": event.previous_state.value if event.previous_state else None,
            "current_state": event.current_state.value,
            "severity": kanary.severity_label(int(event.alert.severity)),
            "message": event.alert.message,
            "occurred_at": event.occurred_at.isoformat(),
        }
        with self.output_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
```

## 7. 何が起きるか

ここまで保存すると、Kanary は plugin 定義を自動 reload します。  
viewer では source, rules, output が見えるようになります。state change が起きると `getting_started_alerts.jsonl` に 1 行ずつ追記されます。

```bash
kanaryctl --base-url http://127.0.0.1:8000 alerts
```

viewer と `kanaryctl` は同じ API を使っています。

## 8. 進んだ機能

### BufferedSource

`kanary.BufferedSource` は source plugin の中で短い履歴を扱う helper です。`history()`, `latest()`, `average_value()`, `rate()` などを使えます。

### Remote Kanary node の読み込み

他の Kanary node の alert を source として読み、local rule として mirror できます。

- [examples/peer_monitoring.py](../examples/peer_monitoring.py)
- [examples/remote_alarm_import.py](../examples/remote_alarm_import.py)

### Mail output と Mailpit

`kanary.MailOutput` を使うと SMTP 出力を短く書けます。ローカルで試すなら Mailpit が便利です。

```bash
docker run --rm -p 1025:1025 -p 8025:8025 axllent/mailpit
```

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