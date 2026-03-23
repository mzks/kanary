# API

## HTTP API

既定 bind:

```text
0.0.0.0:8000
```

起動時に `--api-host` と `--api-port` を指定すると bind address を変更できます。

### Read endpoints

- `GET /health`
- `GET /peer-status`
- `GET /alerts`
- `GET /export-alerts`
- `GET /history/{rule_id}`
- `GET /silences`
- `GET /plugins`
- `GET /viewer`
- `GET /plugins/{type}/{plugin_id}/source`

### Write endpoints

- `POST /alerts/{rule_id}/ack`
- `POST /alerts/{rule_id}/unack`
- `POST /silences/duration`
- `POST /silences/window`
- `POST /silences/{silence_id}/cancel`
- `POST /reload`

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
- `alerts`
- `history`
- `plugins`
- `silences`
- `ack`
- `unack`
- `silence-for`
- `silence-until`
- `unsilence`
- `reload`

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
