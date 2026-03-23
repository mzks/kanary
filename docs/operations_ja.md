# Operations

## 起動方法

基本実行:

```bash
kanary ./rules
```

複数 directory を読む場合:

```bash
kanary ./rules ./local-rules
```

標準の port を明示する場合:

```bash
kanary ./rules --api-port 8000
```

別マシンからアクセスできるようにする場合:

```bash
kanary ./rules --api-host 0.0.0.0 --api-port 8000
```

主な引数:

- `rule_directories...`
- `--api-port`
- `--api-host`
- `--log-level`
- `--state-db`
- `--node-id`
- `--exclude`

主な環境変数:

- `KANARY_SQLITE_PATH`
- `KANARY_API_URL`
- `KANARY_API_HOST`
- `KANARY_NODE_ID`

Kanary 本体に必須の環境変数はありません。接続情報は各 `Source` 実装側で定義します。

## 実行時の挙動

- 起動時に 1 個以上の rule directory を読み込みます
- `@kanary.source`, `@kanary.rule`, `@kanary.output` が登録対象です
- 各 `Source` は `interval` ごとに独立スレッドで poll されます
- `Rule` は対応する source の最新結果で評価されます
- rule directory は継続監視され、変更時に自動 reload されます

## Web viewer

```text
http://<host>:8000/viewer
```

組み込み viewer では次を確認できます。

- dashboard
- alerts
- plugins
- outputs
- silences
- admin page
- plugin source の read-only 表示

viewer からできる write 操作は、すべて `kanaryctl` でも実行できます。

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

## 永続化

SQLite 履歴を有効にするには `--state-db` か `KANARY_SQLITE_PATH` を使います。

```bash
kanary ./rules --state-db ./var/kanary.db
```

保存されるもの:

- alert state change
- operator action
- silence
