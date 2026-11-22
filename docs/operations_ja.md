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
- plugin directory は Python の import path に入ります（Python 自身の module と install 済み package より後ろ）。そのため plugin は隣の helper module を import できます（`from shared import LIMIT`）。そのような helper も plugin file と同じく、plugin file を読み直すたびに file から読み直します。`reload --dirty` は、それを import している plugin を、別の helper を経由する場合も含めて reload します。`__init__.py` のある directory は package です。その中の file は package の module として（`pkg/limits.py` は `pkg.limits`、`from .values import LIMIT` も使えます）読み直すたびに 1 回だけ実行され、plugin を定義することもできます。helper は plugin file の先頭で import してください。動いている plugin は、reload されるまで読み込んだときの helper を使い続けます。一方 `evaluate()` や `poll()` の中の `import` は、最後に読み直した helper を使います（`--auto-reload off` で変更をまだ適用していなくても）
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
  新しい engine は、動いている engine が state database に書いた内容から始まります。切り替えの間、acknowledge と silence は HTTP 503 で断られます（`kanaryctl` は error を表示します。少し待ってからやり直してください）。こうして、2 つの engine の間で操作が失われることはありません。動いている engine の最後の書き込みを state database が 10 秒以内に受け取れない場合（別の process が lock している、disk が一杯など）、restart は取りやめになり（`reload_failed`）、動いている engine がそのまま続きます。
  切り替えの間も source は poll を続けます（読んだ値は待たせます）。source の thread を止めるのは新しい engine が動き出してからなので、restart を取りやめたときは元のままです。動いている engine の event は切り替えの前にすべて送り終える（または失敗する）ので、受信側に `OK` の後からその前の `FIRING` が届くことはありません。output が 5 秒たっても送り終えない場合（送り先が遅い、届かないなど）は、同じように restart を取りやめ、error にその output を示します。送り終えてからやり直すか、process を再起動してください。
  file から無くなった plugin は `reload --all` と同じように削除し、`removed` に載せます。削除した rule の ack と判定は残らないので、後で同じ id の rule を追加しても新しく始まります。

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
- 各 output に送り、まだ `OK` を送っていない alert と、最後に送った severity（再起動の後の `OK` が、閉じるべき output に届くように）
- rule ごとの、rule 自身の最後の判定（`FIRING` か `OK`）と、その message と値。表示とは別に保存します（判定が変わったとき、および停止時と `reload --full` の前に書きます。戻した alert は最後に評価した値を持ちます）。再起動の後も、`SILENCED` だった親はその後ろの rule を抑え続け、KANARY が止まっている間に始まった・終わった silence は起動時に反映されます

database への書き込みは専用の thread で順番に行うので、評価と通知がそれを待つことはありません。database が今は受け付けない書き込み（他の process が SQLite の busy timeout を超えて lock している、disk が一杯など）は、1 秒後から最長 30 秒おきに、入るまでやり直します。履歴は遅れても欠けず、再起動でも実際に起きたことを復元します。その間は `GET /health` と `GET /meta` の `state_db_error`、viewer の「State DB Write Error」に表示します。停止時にまだ待っている書き込みには 10 秒を与え、そこで諦めたもの（または 100,000 件を超えて待っているもの）は log に出します。

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
-- acknowledge と rule の削除は残す: まだ acknowledge されている alert は、ここから ack を取り戻します。
DELETE FROM operator_actions WHERE created_at < '2024-01-01' AND action_type NOT IN ('ack', 'unack', 'rule_removed');
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
