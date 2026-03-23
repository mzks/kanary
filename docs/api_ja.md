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
  1 つの rule に対する alert event と operator action を返します。
- `GET /silences`
  active, scheduled, cancelled の silence を返します。
- `GET /plugins`
  source, rule, output plugin の current status を返します。
- `GET /viewer`
  組み込み Web viewer を返します。
- `GET /plugins/{type}/{plugin_id}/source`
  読み込まれている 1 つの plugin の read-only source code を返します。

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
  watch 対象 directory の manual reload を実行します。

## API の考え方

- Web viewer と `kanaryctl` は同じ API を使います
- history は SQLite 永続化が有効なときだけ残ります
- `/plugins/{type}/{plugin_id}/source` は loaded plugin に紐づく source code だけを返します
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
- `plugins`
  source, rule, output plugin の状態を表示します。
  `--filter` で text または glob matching が使えます。
- `silences`
  設定済み silence を表示します。
  `--filter` で text または glob matching が使えます。
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
  manual reload を実行します。

共通引数:

- `--base-url`
  接続先の Kanary API URL を指定します。

例:

```bash
kanaryctl alerts
kanaryctl ack sqlite.value1.stale --operator operator_name --reason "investigating"
kanaryctl unack sqlite.value1.stale --operator operator_name --reason "re-open"
kanaryctl silence-for --operator operator_name --minutes 10 --rule 'sqlite.*'
kanaryctl reload
```
