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
- `RateRule`: `inputs`, `per_seconds`, `high`, `low`
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

### BufferedSource

`kanary.BufferedSource` は source plugin の中で短い履歴を扱う helper です。`history()`, `latest()`, `average_value()`, `rate()` などを使えます。

### Remote Kanary node の読み込み

他の Kanary node の alert を source として読み、local rule として mirror できます。

- [examples/peer_monitoring.py](../examples/peer_monitoring.py)
- [examples/remote_alarm_import.py](../examples/remote_alarm_import.py)

### Mail output と Mailpit

`kanary.MailOutput` を使うと SMTP 出力を短く書けます。ローカルで試すなら Mailpit が便利です。

```python
import json

@kanary.output(output_id="mail", include_tags=["getting-started"])
class MailAlert(kanary.MailOutput):
    smtp_host = "127.0.0.1"
    smtp_port = 1025
    use_starttls = False
    sender = "kanary@example.test"
    recipients = ["operator@example.test"]
    subject_prefix = "[KANARY getting-started]"

    def _subject(self, event):
        marker = event.transition.value if event.transition is not None else event.current_state.value
        return (
            f"{self.subject_prefix} "
            f"{marker} {kanary.severity_label(event.effective_severity)} {event.rule_id}"
        )

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
        if event.payload:
            lines.extend(
                [
                    "",
                    "Payload:",
                    json.dumps(event.payload, ensure_ascii=False, indent=2, sort_keys=True),
                ]
            )
        return "\n".join(lines)
```

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
