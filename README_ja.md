# Kanary

Kanary は、アラーム、通知、信頼性監視のための Python ベースの実行環境です。  
監視対象から値を読む `Source`、その値を評価する `Rule`、状態変化を外部へ送る `Output` を Python で定義します。

`Source`, `Rule`, `Output` を分けることで、値の取得、異常判定、通知の責務が混ざりにくくなります。監視対象や通知先が増えても、監視定義を整理しやすいのが Kanary の基本的な考え方です。

## インストール

通常のインストール方法は PyPI です。

```bash
pip install kanary
```

`uv` を使う場合は次でも構いません。

```bash
uv tool install kanary
```

`kanary` と `kanaryctl` の実行ファイルがインストールされます.

sourceからの実行は以下です。

```bash
git clone https://github.com/mzks/kanary
cd kanary
uv sync
uv run python -m kanary ./demo
```

## 最初にやること

最初は `demo/` の最小例を動かしてください。

```bash
kanary ./demo
```

その後は次の順がおすすめです。

1. [demo/basic_monitoring.py](demo/basic_monitoring.py) を読んで、最小の `Source`, `Rule`, `Output` を把握する
2. [docs/getting_started_ja.md](docs/getting_started_ja.md) と [examples/getting_started.py](examples/getting_started.py) を読み、手を動かしながら流れを確認する
3. `examples/` で PostgreSQL、Discord、peer monitoring、remote alert import の例を確認する
4. 自分の `plugins/` directory を作り、最初は `Source` を 1 つ、`Rule` を 1 つだけ書く

## 最小例

最小の実行例は [demo/basic_monitoring.py](demo/basic_monitoring.py) にあります。
プラグインをおくディレクトリを作成して, 以下スクリプトをおいてください.

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

この例で実装しているのは最低限の interface だけです。

- 値を返す source
- 値を評価する rule
- 状態変化を受け取る output

内部では Kanary が plugin の読み込み、source の定期実行、rule の評価、alert state の管理、HTTP API と Web viewer の提供を行います。

あとから短く書きたくなったら、`RangeRule`, `StaleRule`, `ThresholdRule` などの組み込み helper class に置き換えられます。
また, ユーザーは独自のclass factory関数を実装できます.

## 実行方法

基本実行:

```bash
kanary ./demo
```

API / Web viewer の port を明示する場合:

```bash
kanary ./demo --api-port 8000
```

別マシンからアクセスできるようにする場合:

```bash
kanary ./demo --api-host 0.0.0.0 --api-port 8000
```

SQLite に履歴を保存する場合:

```bash
kanary ./demo --state-db ./var/kanary.db
```

Web viewer:

```text
http://<host>:8000/viewer
```

CLI 引数の確認:

```bash
kanary --help
kanaryctl help
```

## 環境変数

Kanary 本体に必須の環境変数はありません。必要に応じて次を使えます。

- `KANARY_SQLITE_PATH`
  SQLite の保存先を環境変数で指定したいときに使います。
- `KANARY_API_URL`
  `kanaryctl` の接続先を既定化したいときに使います。
- `KANARY_API_HOST`
  API と Web viewer の bind host を変えたいときに使います。既定は `0.0.0.0` です。
- `KANARY_NODE_ID`
  peer export/import に使う node identifier を指定したいときに使います。未指定時は hostname を使います。

実際の監視対象ごとの接続情報は、各 `Source` 実装側で定義します。たとえば PostgreSQL の source は `KANARY_POSTGRES_DSN` を使えます。

## Demo と Examples

最小構成:

- [demo/basic_monitoring.py](demo/basic_monitoring.py)

より実運用に近い例:

- [examples/getting_started.py](examples/getting_started.py)
- [examples/factory_patterns.py](examples/factory_patterns.py)
- [examples/fake_alarm_monitoring.py](examples/fake_alarm_monitoring.py)
- [examples/fake_alarm_target.py](examples/fake_alarm_target.py)
- [examples/sqlite_monitoring.py](examples/sqlite_monitoring.py)
- [examples/sqlite_console_output.py](examples/sqlite_console_output.py)
- [examples/discord_webhook_output.py](examples/discord_webhook_output.py)
- [examples/latest_postgres.py](examples/latest_postgres.py)
- [examples/peer_monitoring.py](examples/peer_monitoring.py)
- [examples/self_plugin_monitoring.py](examples/self_plugin_monitoring.py)
- [examples/remote_alarm_import.py](examples/remote_alarm_import.py)

`demo/` は最初の 1 回を動かすための短い例です。`examples/` は helper class、remote monitoring、PostgreSQL、webhook output などを含む、より実運用に近い例です。

## Web viewer

Kanary には組み込みの Web viewer が含まれています。  
ただし、運用上の本体は HTTP API です。viewer はその API を使う標準 UI であり、必要なら API を使って独自の運用画面を実装できます。

## 文書

- [docs/getting_started_ja.md](docs/getting_started_ja.md)
  hands-on で流れを追う文書です。
- [docs/plugins_ja.md](docs/plugins_ja.md)
  plugin interface と組み込み helper class を説明します。
- [docs/operations_ja.md](docs/operations_ja.md)
  実行方法、viewer、CLI、永続化を説明します。
- [docs/api_ja.md](docs/api_ja.md)
  HTTP API と `kanaryctl` を説明します。
- [docs/development_ja.md](docs/development_ja.md)
  開発、lint、tests を説明します。
- [docs/deployment_ja.md](docs/deployment_ja.md)
  deployment layout と `systemd` を説明します。
