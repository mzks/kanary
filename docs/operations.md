# Operations

## 起動方法

基本実行:

```bash
uv sync
uv run python -m kanary ./rules
```

複数 directory を読む場合:

```bash
uv run python -m kanary ./rules ./local-rules
```

API / viewer の port を変える場合:

```bash
uv run python -m kanary ./rules --api-port 18000
```

ログレベルを変える場合:

```bash
uv run python -m kanary ./rules --log-level DEBUG
```

plugin を除外する場合:

```bash
uv run python -m kanary ./rules --exclude 'sqlite.*.stale' --exclude 'discord'
```

主な `kanary` 引数:

- `rule_directories...`
  - 読み込む directory を 1 個以上指定します。
- `--api-port`
  - HTTP API と Web viewer の port を指定します。
- `--log-level`
  - `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`
- `--state-db`
  - SQLite 永続化先を指定します。
- `--exclude`
  - `rule_id`, `source_id`, `output_id` の glob pattern で plugin を除外します。

環境変数として指定できるもの:

- `KANARY_SQLITE_PATH`
  - SQLite 永続化先
- `KANARY_API_URL`
  - `kanaryctl` の接続先

KANARY 本体の起動に必須の環境変数はありません。監視対象ごとの接続情報は、各 `Source` 実装側で定義します。

## 実行時の挙動

- 起動時に 1 個以上の rule directory を読み込みます
- `@kanary.source`, `@kanary.rule`, `@kanary.output` が登録対象になります
- 各 `Source` は `interval` ごとに独立スレッドで poll されます
- `Rule` は、対応する source の最新結果を使って評価されます
- rule directory は継続監視され、変更時に自動 reload されます

## Web viewer

Web viewer は、既定では次の URL で利用できます。

```text
http://127.0.0.1:8000/viewer
```

機能:

- dashboard
- alerts
- plugins
- outputs
- silences
- admin page
- plugin source の read-only 表示

Web からできる write operation は、すべて `kanaryctl` でも実行できます。  
また、viewer は HTTP API を使う標準 UI であり、必要に応じて API ベースで独自実装することもできます。

## CLI

`kanaryctl` は API の thin client です。

```bash
./kanaryctl health
./kanaryctl alerts
./kanaryctl alerts --json
./kanaryctl history sqlite.value1.stale
./kanaryctl plugins
./kanaryctl silences
./kanaryctl ack sqlite.value1.stale --operator operator_name --reason "investigating"
./kanaryctl unack sqlite.value1.stale --operator operator_name --reason "re-open"
./kanaryctl silence-for --operator operator_name --minutes 10 --rule 'sqlite.*'
./kanaryctl silence-until --operator operator_name --start-at 2026-03-19T10:00:00+09:00 --end-at 2026-03-19T12:00:00+09:00 --tag sqlite
./kanaryctl unsilence <silence_id> --operator operator_name
./kanaryctl reload
```

## 永続化

履歴を SQLite に保存するには、`--state-db` か `KANARY_SQLITE_PATH` を使います。

```bash
uv run python -m kanary ./rules --state-db ./var/kanary.db
```

または:

```bash
export KANARY_SQLITE_PATH=./var/kanary.db
uv run python -m kanary ./rules
```

保存対象:

- alert state change
- operator action
- silences

history API と viewer の History 表示は、SQLite 永続化が有効なときだけ内容が残ります。

## Examples と Demo

最小構成:

- [demo/basic_monitoring.py](../demo/basic_monitoring.py)

より実用的な例:

- [examples/sqlite_monitoring.py](../examples/sqlite_monitoring.py)
- [examples/sqlite_console_output.py](../examples/sqlite_console_output.py)
- [examples/discord_webhook_output.py](../examples/discord_webhook_output.py)
- [examples/latest_postgres.py](../examples/latest_postgres.py)
- [examples/peer_monitoring.py](../examples/peer_monitoring.py)
- [examples/remote_alarm_import.py](../examples/remote_alarm_import.py)

開発用 SQLite demo:

```bash
uv sync
export KANARY_SQLITE_PATH=dev_data.db
uv run python dev/emulate_sqlite.py --init
uv run python -m kanary ./examples
```
