# KANARY

KANARY は、KamLAND Slow Control および関連システムのための、ルールベースのアラーム・通知・信頼性監視システムです。  
監視対象から値を取得する `Source`、値を評価する `Rule`、状態変化を外部へ送る `Output` を、Python で記述して運用できます。

## KANARY の考え方

KANARY は、次の分離を大切にしています。

- `Source`
  - どこから値を取るかを記述します。
- `Rule`
  - どの値を見て、どの条件で異常とみなすかを記述します。
- `Output`
  - 状態変化をどこへ送るかを記述します。

この分離により、データ取得、評価、通知の責務が混ざりにくくなり、監視対象や通知先が増えても構成を整理しやすくなります。

## まず何をすればよいか

最初は、`demo/` の最小構成をそのまま動かすのがおすすめです。

```bash
uv sync
uv run python -m kanary ./demo
```

そのあとに次の順で進めると、実際の監視定義へ早くたどり着けます。

1. `demo/basic_monitoring.py` を読んで、`Source`, `Rule`, `Output` の最小形を把握します。
2. `examples/` を読んで、複数 measurement や PostgreSQL、Discord、peer monitoring の例を確認します。
3. 自分の `rules/` directory を作り、最初は `Source` を 1 つ、`Rule` を 1 つだけ書きます。
4. 必要に応じて `RangeRule`, `StaleRule`, `ThresholdRule` などの組み込み helper class を使います。

## 最小構成

最小構成は [demo/basic_monitoring.py](demo/basic_monitoring.py) にあります。

```python
from datetime import datetime, timezone

import kanary


@kanary.source(source_id="demo", interval=10.0)
class DemoSource:
    def poll(self, ctx):
        return kanary.SourceResult(
            measurements=[
                kanary.Measurement(
                    name="temperature",
                    value=23.4,
                    timestamp=datetime.now(timezone.utc),
                )
            ],
            status="ok",
        )


@kanary.rule(
    rule_id="demo.temperature.high",
    source="demo",
    severity=kanary.WARN,
    tags=["demo"],
    owner="demo_owner",
)
class DemoTemperatureHigh:
    threshold = 25.0

    def evaluate(self, payload, ctx):
        temperature = ctx.value("temperature")
        if temperature is None:
            return kanary.Evaluation(
                state=kanary.AlertState.OK,
                payload=payload,
                message="temperature is missing",
            )
        if temperature > self.threshold:
            return kanary.Evaluation(
                state=kanary.AlertState.FIRING,
                payload=payload,
                message=f"temperature={temperature} is higher than {self.threshold}",
            )
        return kanary.Evaluation(
            state=kanary.AlertState.OK,
            payload=payload,
            message=f"temperature={temperature} is within limit",
        )


@kanary.output(output_id="console")
class ConsoleOutput:
    def emit(self, event, ctx):
        print(event.rule_id, event.current_state.value, event.alert.message)
```

この例では、まず最低限必要な interface だけで書いています。

- 値の取得
- 異常判定
- 状態変化の出力

内部では KANARY が、plugin の読み込み、source の定期実行、rule の評価、alert state 管理、HTTP API と Web viewer の提供を行います。

より短く書きたい場合は、あとから `RangeRule`, `StaleRule`, `ThresholdRule` などの組み込み helper class を使えます。

## 実行例

基本実行:

```bash
uv run python -m kanary ./demo
```

API / Web viewer の port を変更する場合:

```bash
uv run python -m kanary ./demo --api-port 18000
```

履歴を SQLite に保存する場合:

```bash
uv run python -m kanary ./demo --state-db ./var/kanary.db
```

Web viewer は、既定では次の URL で利用できます。

```text
http://127.0.0.1:8000/viewer
```

起動引数の一覧は `uv run python -m kanary --help` で確認できます。  
`kanaryctl` の引数と subcommand は `kanaryctl help` で確認できます。

## 環境変数

KANARY 本体の実行に必須の環境変数はありません。  
必要に応じて、次を使えます。

- `KANARY_SQLITE_PATH`
  - SQLite 永続化先を環境変数で指定したい場合に使います。
- `KANARY_API_URL`
  - `kanaryctl` の接続先を変えたい場合に使います。

監視対象ごとの接続情報は、各 `Source` 実装側で別途定義します。  
たとえば PostgreSQL の例では `KANARY_POSTGRES_DSN` を使います。

## Demo と Examples

最小構成:

- [demo/basic_monitoring.py](demo/basic_monitoring.py)

より実運用に近い例:

- [examples/sqlite_monitoring.py](examples/sqlite_monitoring.py)
- [examples/sqlite_console_output.py](examples/sqlite_console_output.py)
- [examples/discord_webhook_output.py](examples/discord_webhook_output.py)
- [examples/latest_postgres.py](examples/latest_postgres.py)
- [examples/peer_monitoring.py](examples/peer_monitoring.py)
- [examples/remote_alarm_import.py](examples/remote_alarm_import.py)

`demo/` は最初の一歩のための短い例です。  
`examples/` は PostgreSQL、Discord、peer monitoring、helper class などを含む、より実運用に近い例です。

## Web viewer について

KANARY には組み込みの Web viewer が含まれています。  
ただし、運用上の本体は HTTP API です。viewer はその API を使う標準 UI であり、必要であれば API を使って独自の viewer や運用画面を実装できます。

## ドキュメント

詳細は `docs/` に分けています。

- [docs/operations.md](docs/operations.md)
  - 実行方法、viewer、CLI、永続化、examples
- [docs/plugins.md](docs/plugins.md)
  - plugin の仕様。最小 interface から組み込み helper class まで
- [docs/api.md](docs/api.md)
  - HTTP API と `kanaryctl`
- [docs/development.md](docs/development.md)
  - 開発、lint、tests
- [docs/deployment.md](docs/deployment.md)
  - installable CLI と deployment の考え方
