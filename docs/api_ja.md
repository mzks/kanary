# API

## HTTP API

既定 bind:

```text
0.0.0.0:8000
```

起動時に `--api-host` と `--api-port` を指定すると bind address を変更できます。

### Read endpoints

- `GET /health`
  source や rule の読み込み状況を含む、小さな runtime health summary を返します。
- `GET /peer-status`
  peer monitoring 用の compact な status payload を返します。
- `GET /alerts`
  local node の current alert 一覧を返します。
- `GET /export-alerts`
  remote alert import 用の安定した形式で alert を返します。
- `GET /history/{rule_id}`
  1 つの rule に対する alert event、output dispatch summary、operator action を返します。
- `GET /silences`
  active, scheduled, cancelled の silence を返します。
  raw API 自体には `EXPIRED` 状態は追加しません。Web viewer と `kanaryctl` では、すでに終了した silence を表示上 `EXPIRED` と導出することがあります。
- `GET /plugins`
  source, rule, output plugin の current status を返します。
  plugin の state には `DISCOVERED`, `DIRTY`, `PENDING_REMOVE`, `READY`, `RELOADING`, `FAILED` が出ます。
- `GET /viewer`
  組み込み Web viewer を返します。
- `GET /plugins/{type}/{plugin_id}/source`
  読み込まれている、または DISCOVERED な 1 つの plugin の read-only source code を返します。

### Write endpoints

- `POST /alerts/{rule_id}/ack`
  1 つの alert を acknowledge します。
- `POST /alerts/{rule_id}/unack`
  1 つの alert の acknowledgement を外します。
- `POST /silences/duration`
  10 分のような相対 duration で silence を作ります。
- `POST /silences/window`
  start/end を明示した time window で silence を作ります。
- `POST /silences/{silence_id}/cancel`
  既存の silence を cancel します。
- `POST /reload`
  発見済み plugin の変更を適用します。
  JSON body には次のいずれか 1 つだけを入れます。
  - `{"rule":"postgres.*"}`
  - `{"source":"postgres*"}`
  - `{"output":"discord*"}`
  - `{"dirty":true}`
  - `{"all":true}`
  legacy compatibility のため、空 body も受け付けます。この場合は `{"all":true}` と同じ意味です。
- `POST /test-poll/{source_id}`
  1 つの source を poll し、normalized source payload を返します。
- `POST /test-evaluate/{rule_id}`
  明示した payload に対して 1 つの rule を dry-run し、normalized evaluation result を返します。
- `POST /test-fire/{rule_id}`
  live alert state を変更せず、synthetic な state change を output pipeline に流します。

## API の考え方

- Web viewer と `kanaryctl` は同じ API を使います
- history は SQLite 永続化が有効なときだけ残ります
- `/plugins/{type}/{plugin_id}/source` は loaded plugin と DISCOVERED plugin の source code を返します
- `dirty` は完全な依存解析ではなく、実用上の reload ヒントです。Kanary は plugin 定義本体の変更と watched root 内の静的 import を見ますが、same-file helper の全変更や動的依存を完全には追いません。
- raw file path は受け取りません
- `/export-alerts` は remote import 用の endpoint です

## kanaryctl

`kanaryctl` は API の thin client です。

主な subcommand:

- `health`
  runtime health summary を表示します。
- `alerts`
  current alert を表示します。
  `--filter` で text または glob matching が使えます。
- `history`
  1 つの rule の保存済み history を表示します。
  `--since` と `--limit` は history payload を取得した後に client-side で適用します。
  SQLite 永続化が有効な場合、output dispatch summary も含まれます。
- `plugins`
  source, rule, output plugin の状態を表示します。
  `--filter` で text または glob matching が使えます。
- `silences`
  設定済み silence を表示します。
  `--filter` で text または glob matching が使えます。
  `--since` と `--limit` は silence 一覧を取得した後に client-side で適用します。
- `ack`
  1 つの alert を acknowledge します。
- `unack`
  1 つの alert の acknowledgement を外します。
- `silence-for`
  duration 指定で silence を作ります。
- `silence-until`
  start/end 指定で silence を作ります。
- `unsilence`
  1 つの silence を cancel します。
- `reload`
  発見済み plugin の変更を適用します。
  `--rule`, `--source`, `--output`, `--dirty`, `--all` のいずれか 1 つを指定します。
  legacy compatibility のため、HTTP の `POST /reload` に空 body を送った場合は `--all` 相当で動きます。
- `test-poll`
  1 つの source を poll し、normalized payload を JSON で表示します。
- `test-evaluate`
  `--payload-json`, `--payload-file`, `--payload-stdin` のいずれかで与えた payload に対して 1 つの rule を dry-run します。
- `test-fire`
  synthetic な alert event を output pipeline に流し、dispatch summary を JSON で表示します。

共通引数:

- `--base-url`
  接続先の Kanary API URL を指定します。

例:

```bash
kanaryctl alerts
kanaryctl test-poll sqlite
kanaryctl test-evaluate sqlite.value1.range --payload-json '{"channels":{"value1":{"value":120,"timestamp":"2026-05-29T00:00:00+00:00"}},"status":"ok"}'
kanaryctl test-fire sqlite.value1.range --state FIRING --reason "output check"
kanaryctl ack sqlite.value1.stale --operator operator_name --reason "investigating"
kanaryctl unack sqlite.value1.stale --operator operator_name --reason "re-open"
kanaryctl silence-for --operator operator_name --minutes 10 --rule 'sqlite.*'
kanaryctl reload --rule 'sqlite.*'
kanaryctl reload --dirty
```
