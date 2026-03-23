# Operations

## 起動方法

基本実行:

```bash
kanary ./plugins
```

複数 directory を読む場合:

```bash
kanary ./plugins ./local-plugins
```

標準の port を明示する場合:

```bash
kanary ./plugins --api-port 8000
```

別マシンからアクセスできるようにする場合:

```bash
kanary ./plugins --api-host 0.0.0.0 --api-port 8000
```

主な引数:

- `rule_directories...`
  読み込む plugin directory を指定します。
- `--api-port`
- `--api-host`
- `--log-level`
- `--state-db`
- `--node-id`
- `--exclude`
- `--disable-default-viewer`

主な環境変数:

- `KANARY_SQLITE_PATH`
- `KANARY_API_URL`
- `KANARY_API_HOST`
- `KANARY_NODE_ID`

Kanary 本体に必須の環境変数はありません。接続情報などは各 `Source` 実装側で定義します。

## 実行時の挙動

- 起動時に 1 個以上の plugin directory を読み込みます
- `@kanary.source`, `@kanary.rule`, `@kanary.output` が登録対象です
- 各 `Source` は `interval` ごとに独立スレッドで poll されます
- `Rule` は対応する source の最新結果で評価されます
- plugin directory は継続監視され、変更時に自動 reload されます

## Web viewer

```text
http://<host>:8000/viewer
```

組み込み viewer では次を確認できます。

- dashboard
- alerts
- sources
- rules
- outputs
- silences
- admin page
- plugin source の read-only 表示

viewer からできる write 操作は、すべて `kanaryctl` でも実行できます。
API と CLI だけを使いたい場合は、`--disable-default-viewer` を指定すると `/viewer` が `404` になります。

## CLI

`kanaryctl` は API の thin client です。

```bash
kanaryctl health
kanaryctl alerts
kanaryctl alerts --json
kanaryctl history sqlite.value1.stale
kanaryctl ack sqlite.value1.stale --operator operator_name --reason "investigating"
kanaryctl unack sqlite.value1.stale --operator operator_name --reason "re-open"
kanaryctl silence-for --operator operator_name --minutes 10 --rule 'sqlite.*'
kanaryctl reload
```

## 履歴の永続化

SQLite 履歴を有効にするには `--state-db` か `KANARY_SQLITE_PATH` を使います。

```bash
kanary ./plugins --state-db ./var/kanary.db
```

保存されるもの:

- alert state change
- operator action
- silence

## Demo と Examples

- [demo/basic_monitoring.py](../demo/basic_monitoring.py)
- [examples/getting_started.py](../examples/getting_started.py)
- [examples/sqlite_monitoring.py](../examples/sqlite_monitoring.py)
- [examples/sqlite_console_output.py](../examples/sqlite_console_output.py)
- [examples/discord_webhook_output.py](../examples/discord_webhook_output.py)
- [examples/latest_postgres.py](../examples/latest_postgres.py)
- [examples/peer_monitoring.py](../examples/peer_monitoring.py)
- [examples/remote_alarm_import.py](../examples/remote_alarm_import.py)
