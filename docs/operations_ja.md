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

本番の入力と設定で Output routing だけを確認する shadow instance:

```bash
kanary ./plugins \
  --api-port 8001 \
  --node-id production-shadow \
  --no-output-emit
```

`--no-output-emit` を指定しても Output plugin のloadと`init()`、routing filterの評価は通常どおり行われますが、`emit()`は呼ばれません。dispatch summaryでは対象Outputを`emit_skipped_outputs`に記録し、`delivered_outputs`とOutputのEmit Countは増えません。Source poll、Rule評価、state change、operator action、remote action propagationは停止しません。本番と並べて起動する場合は、別の`--state-db` pathを使うか、state DBを指定しないでください。

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
- `--viewer-title`
- `--no-output-emit`

主な環境変数:

- `KANARY_SQLITE_PATH`
- `KANARY_API_URL`
- `KANARY_API_HOST`
- `KANARY_NODE_ID`
- `KANARY_VIEWER_TITLE`

Kanary 本体に必須の環境変数はありません。接続情報などは各 `Source` 実装側で定義します。

## 実行時の挙動

- 起動時に 1 個以上の plugin directory を読み込みます
- `@kanary.source`, `@kanary.rule`, `@kanary.output` が登録対象です
- `init()` に失敗した source や output は `FAILED` になりますが、engine / API / viewer 自体は起動を継続し、その plugin は自分の thread で `init()` をやり直し続けます（[plugins_ja.md](plugins_ja.md) の失敗からの復帰を参照）。停電の後、KANARY が database や mail server より先に起動しても、それらが戻った時点で使い始めます
- 各 `Source` は `interval` ごとに独立スレッドで poll されます
- `Rule` は対応する source の最新結果で評価されます
- plugin directory は継続監視され、Python file の変更は自動検知されます
- local TOML config のような `.py` 以外の file は監視対象ではないので、変更後は `kanaryctl reload ...` を明示的に実行してください
- default の `--auto-reload off` では、反映は `kanaryctl reload ...` で明示的に行います

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
kanaryctl test-poll sqlite
kanaryctl test-evaluate sqlite.value1.range --payload-file payload.json
kanaryctl test-fire sqlite.value1.range --state FIRING --reason "output check"
kanaryctl reload --dirty
kanaryctl reload --all
kanaryctl reload --full
kanaryctl ack sqlite.value1.stale --operator operator_name --reason "investigating"
kanaryctl unack sqlite.value1.stale --operator operator_name --reason "re-open"
kanaryctl silence-for --operator operator_name --minutes 10 --rule 'sqlite.*'
```

- `reload --dirty`
  `DISCOVERED`, `DIRTY`, `PENDING_REMOVE` の plugin だけを適用します。
- `reload --all`
  watched な Python file に変更が無くても full discovery をやり直してから適用します。
- `reload --full`
  in-process で Engine restart を行います。全 source/rule/output が再初期化されます。`init()` に失敗する plugin があっても restart は止まりません（やり直し続けます）。plugin がそもそも作れない場合は止まり、動いている engine がそのまま続きます。

## 履歴の永続化

SQLite 履歴を有効にするには `--state-db` か `KANARY_SQLITE_PATH` を使います。

```bash
kanary ./plugins --state-db ./var/kanary.db
```

保存されるもの:

- alert event。state change と severity transition を含みます
- output dispatch summary
- operator action
- silence

Kanary は新しく作る SQLite state DB に schema version を記録します。  
この versioning は意図的に最小限で、0.3.x では認識できない旧 legacy DB を自動 migrate しません。古い DB を使う場合は、新しい `--state-db` path を使って作り直してください。

### 古い履歴を消す

database が増えるのは何かが起きたときだけです。状態の変化、配信、operator の操作ごとに 1 行で、評価のたびではありません。1 日 100 回の状態変化で年に数十 MB です。poll のたびに状態が入れ替わる（flapping する）rule はずっと多く増やすので、その rule 自体を直す価値があります。ある日付より古い履歴を消すには、KANARY を止め、file を backup してから次を実行します:

```bash
sqlite3 ./var/kanary.db <<'SQL'
-- rule ごとの最新の event は残す: 再起動の後、KANARY はそこから alert を復元します。
DELETE FROM alert_events
 WHERE occurred_at < '2024-01-01'
   AND id NOT IN (SELECT MAX(id) FROM alert_events GROUP BY rule_id);
DELETE FROM output_dispatches WHERE occurred_at < '2024-01-01';
-- acknowledge は残す: まだ acknowledge されている alert は、ここから ack を取り戻します。
DELETE FROM operator_actions WHERE created_at < '2024-01-01' AND action_type NOT IN ('ack', 'unack');
DELETE FROM silences WHERE end_at < '2024-01-01';
VACUUM;
SQL
```

時刻は ISO 8601 の文字列で保存されているので、`'2024-01-01'` のような日付と比べられます。

## Demo と Examples

- [demo/basic_monitoring.py](../demo/basic_monitoring.py)
- [examples/getting_started.py](../examples/getting_started.py)
- [examples/factory_patterns.py](../examples/factory_patterns.py)
- [examples/sqlite_monitoring.py](../examples/sqlite_monitoring.py)
- [examples/sqlite_console_output.py](../examples/sqlite_console_output.py)
- template: `kanary new --list`、[templates_ja.md](templates_ja.md)
