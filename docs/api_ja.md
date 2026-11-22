# API

## HTTP API

既定 bind:

```text
0.0.0.0:8000
```

起動時に `--api-host` と `--api-port` を指定すると bind address を変更できます。

### Read endpoints

- `GET /health`
  sourceやruleの読み込み状況と`output_emit_enabled`を含む、小さなruntime health summaryを返します。
  `time`、`timezone`、`utc_offset` は host の時計と time zone です。time zone 無しで送った時刻（silence の期間など）はこの zone で読みます。
- `GET /peer-status`
  peer monitoring 用の compact な status payload を返します。
- `GET /alerts`
  local node の current alert 一覧を返します。
  再起動の後は、各 rule の alert を state database から止まったときの状態で戻し、その rule が評価されるまで `"restored": true` を付けます。戻した alert も ack や silence ができます。まだ alert の無い rule（一度も評価されていない、または `--state-db` なし）はこの一覧に入らず、viewer と `kanaryctl alerts` では `WAITING` と表示します。
- `GET /export-alerts`
  remote alert import 用の安定した形式で alert を返します。
- `GET /history/{rule_id}`
  1 つの rule に対する alert event、output dispatch summary、operator action を返します。
  output dispatchには、`--no-output-emit`により実行されなかったmatched outputを`emit_skipped_outputs`として含めます。
- `GET /alert-history`
  全 rule の alert event と operator action を、新しい順の統合タイムラインとして返します。
  初回requestでは各history streamから直近の行をそれぞれ最大`limit`件返します。2回目以降は`after_alert_id`と`after_action_id`を指定すると、それぞれのIDより後に追加された行だけを取得できます。`limit`はstreamごとに最大500件です。
  filterはWeb viewerが読み込み済みのhistoryに対して適用します。Web viewerはACK/UNACK操作を対応するalert eventへ統合して表示します。
  初回評価がOKだっただけのevent (`INITIAL -> OK`) は省略します。初回から非OKだったeventは返します。
- `GET /silences`
  active, scheduled, cancelled の silence を返します。
  raw API 自体には `EXPIRED` 状態は追加しません。Web viewer と `kanaryctl` では、すでに終了した silence を表示上 `EXPIRED` と導出することがあります。
- `GET /plugins`
  source, rule, output plugin の current status を返します。
  plugin の state には `DISCOVERED`, `DIRTY`, `PENDING_REMOVE`, `READY`, `RELOADING`, `FAILED` が出ます。
  rule の `evaluate()` が遅い間は `warning` が入ります。output の行には `queued`（待っている、または送信中の event 数）と `dropped`（queue が溢れたか停止時に捨てた event 数）が付きます。
- `GET /viewer`
  組み込み Web viewer を返します。
- `GET /plugins/{type}/{plugin_id}/source`
  読み込まれている、または DISCOVERED な 1 つの plugin の read-only source code を返します。

JSON の severity は名前（`"ERROR"`）です。下の request は、大文字小文字を問わない名前か、`kanary.ERROR` の数値を受け付けます。

### Write endpoints

request body は JSON object です。それ以外（JSON でないもの、list、文字列）は 400 を返します。存在しない rule や silence を指定した operator の操作は 404 を返します。その alert の今の状態には当てはまらない操作（`FIRING` でない alert の acknowledge、acknowledge されていない alert の unack、cancel 済みの silence の cancel）は 409 を返します。error の応答には `error` の文があります。

- `POST /alerts/{rule_id}/ack`
  `FIRING` の alert を 1 つ acknowledge します（再起動の後に `FIRING` として復元された alert も含む）。
- `POST /alerts/{rule_id}/unack`
  1 つの alert の acknowledgement を外します。
- `POST /silences/duration`
  10 分のような相対 duration で silence を作ります。
- `POST /silences/window`
  start/end を明示した time window で silence を作ります。
  `start_at` と `end_at` は ISO 8601 の時刻で、time zone が無ければ host の local time です。どちらの silence endpoint も、読んだ期間を offset 付きで返します（`start_at`、`end_at`）。
  `rule_patterns` と `tags` は名前の list です。1 つだけなら文字列でも書けます（`"rule_patterns": "sqlite.*"` は 1 つの pattern）。
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
  - `{"full":true}`
  `{"all":true}` は watched な Python file に変更が無くても、毎回 full discovery をやり直します。
  `{"full":true}` は discovery 成功後に in-process で Engine restart を行い、全 source/rule/output を再初期化します。
  legacy compatibility のため、空 body も受け付けます。この場合は `{"all":true}` と同じ意味です。
  reload した rule は、同じ reload の source と output が揃ってから評価するので、新しい rule の最初の alert は新しい output にも届きます。
  応答は `sources` / `rules` / `outputs` ごとに、`matched`、`reloaded`、`removed`、`failed`（class か `__init__()` が失敗したので適用せず、動いている instance がそのまま続く）、`init_failed`（適用したが最初の `init()` が失敗した。やり直し続ける）、`errors`（それぞれの理由）を返します。`status` は、すべて適用できたら `reloaded`、一部だけなら `partial`、何も適用できなかったら `reload_failed` です（plugin file が load や検査に通らない場合も `reload_failed`）。`reloaded` 以外は HTTP 500 で、各 plugin を挙げた `error` 文字列が付きます。
- `POST /test-poll/{source_id}`
  その source の `test_poll()` を、source の thread で poll と poll の間に実行し、その結果を fully-qualified input name で返します: `{"source_id": "postgres", "status": "ok", "metadata": {...}, "inputs": {"postgres:temperature": {"value": 21.5, "timestamp": "...", "metadata": {}}}}`（`reason` と `metadata` は poll が与えたときだけ）。これはそのまま `POST /test-evaluate/{rule_id}` の payload になります。結果は評価に使わず、source の状態と status も変わらず、通常の poll の予定もずれません。source が動いていない（`init()` に失敗した）場合や、30 秒以内に終わらない場合は 409 を返します。
- `POST /test-evaluate/{rule_id}`
  明示した payload に対して 1 つの rule を dry-run し、normalized evaluation result を返します。
  payload は次のどれかです。
  - `postgres:temperature` のような fully-qualified input name を key にした `inputs` object。必要なら `status`、`reason`、`metadata` を並べて書きます。`POST /test-poll/{source_id}` が返すのはこの形です。複数の source を読む rule では、それぞれの test poll の `inputs` を合わせます。input には `prev_value`、`prev_timestamp`、`prev_metadata` も書けます。`inputs` と並んだ `source_id` は payload の出どころの source です。`source_id:` の無い名前はその source の input になり、input が空ならその source の空の snapshot になります。`source_id` が無くても、source を 1 つだけ読む rule なら、alert の payload のように `source_id:` を省いた名前を受け付けます。0.8.0 より前の `channels` も `inputs` と同じに読みます。
  - input が 1 つの rule なら `{"value": ..., "timestamp": ..., "prev_value": ...}`。

  通常の rule 実装では、`ctx.value()` や `ctx.inputs()` などの accessor を使ってください。

  判定には live rule の copy を使うので、`evaluate()` が `self` に加えた変更（counter や前回値）は live rule に届きません。copy できない rule（lock や接続を持つもの）は新しい instance で判定します。`kanary.Buffer` に加えたものは、test poll と同じく捨てます。
- `GET /test-evaluate-template/{rule_id}`
  1 つの rule 向けの canonical な `inputs` payload template を返します。単一 input rule の場合は shorthand template も含まれることがあります。
- `POST /test-fire/{rule_id}`
  live alert state を変更せず、synthetic な state change を output pipeline に流します。
  body には `"state"` か `"evaluation"` のどちらかを書きます。`"evaluation"` は `POST /test-evaluate/{rule_id}` の結果で、event はその state、severity、message、値を持ちます。どちらでも message は `[TEST-FIRE]` で始まります。
  `--no-output-emit`の場合はOutputを呼ばず、matching結果を`emit_skipped_outputs`に返します。
  event は `event.synthetic = True` を持ちます。`OutputFollowups` と `QuietHours` は、test fire の followup や保留中の event を rule の本物とは分けて持つので、試験の `OK` が live alert の followup を取り消すことはありません。

## API の考え方

- Web viewer と `kanaryctl` は同じ API を使います
- history は SQLite 永続化が有効なときだけ残ります
- `/plugins/{type}/{plugin_id}/source` は loaded plugin と DISCOVERED plugin の source code を返します
- `dirty` は完全な依存解析ではなく、実用上の reload ヒントです。Kanary は plugin 定義本体の変更と watched root 内の静的 import を見ますが、same-file helper の全変更や動的依存を完全には追いません。
- raw file path は受け取りません
- `/export-alerts` は remote import 用の endpoint です

## kanaryctl

`kanaryctl` は API の thin client です。

`kanary` 本体の CLI では `run` は省略可能です。たとえば `kanary ./plugins` は `kanary run ./plugins` と同じ意味です。`lint` は subcommand を明示してください。

主な subcommand:

- `health`
  runtime health summary を表示します。
- `alerts [RULE_ID]`
  current alert を、alert 中のものから表示します。すべての rule が載り、まだ alert の無い rule は `WAITING`、再起動前の状態を戻したものには `*` が付きます。
  terminal では message を幅に合わせて切ります（`…`）。`--wide` は outputs と file の列を足して何も切りません。pipe に流すときも切りません。
  `RULE_ID`（または glob）を付けると、その alert をすべて表示します: message、値、ack、silence、tags、outputs、runbook、file、最近の history。
  `--filter` で text または glob matching が使えます。
- `history`
  1 つの rule の保存済み history を表示します。
  `--since` と `--limit` は history payload を取得した後に client-side で適用します。
  SQLite 永続化が有効な場合、output dispatch summary も含まれます。
- `plugins [PLUGIN_ID]`
  source、rule、output の順に plugin の状態を表示します。最終更新の時刻は local time です。
  terminal では last error を幅に合わせて切ります。`--wide` は file の列を足して何も切りません。
  `PLUGIN_ID`（または glob）を付けると、その plugin をすべて表示します: 実行回数、last error の全文、rule なら inputs と outputs、output なら routing と queue。`--traceback` で last error の起きた場所も出します。
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
  `--start-at 2026-11-21T22:00` は KANARY の host の local time です（kanaryctl を動かす machine ではありません）。明示するなら offset を付けます（`2026-11-21T22:00+09:00`）。KANARY が読んだ期間を stderr に表示し、stdout は silence id です。
- `unsilence`
  1 つの silence を cancel します。
- `reload`
  発見済み plugin の変更を適用します。
  `--rule`, `--source`, `--output`, `--dirty`, `--all`, `--full` のいずれか 1 つを指定します。
  `--all` は local の Python file に変更が無くても full discovery をやり直します。
  `--full` は in-process で Engine restart を行い、全 source/rule/output を再初期化します。
  legacy compatibility のため、HTTP の `POST /reload` に空 body を送った場合は `--all` 相当で動きます。
  status（`reloaded`、`partial`、`reload_failed`）と、適用できなかった plugin や `init()` が失敗した plugin ごとの行を表示します。status が `reloaded` でなければ終了コードは 1 です。
  reload した rule は、その source の最後の値ですぐに評価されます。閾値を変えたら次の値を待たずに反映されます。起動後まだ値を出していない source を読む rule は、値が来るまで待ちます。
- `test-poll`
  source の `test_poll()` を実行し（その source の thread で poll と poll の間に。評価には使いません）、その結果を `source_id:input` の名前で JSON 表示します。
- `test-evaluate`
  `--payload-json`, `--payload-file`, `--payload-stdin` のいずれかで与えた payload に対して 1 つの rule を dry-run します。`test-poll` の出力もそのまま payload になります。
  canonical な `inputs` payload を先に見たい時は `--print-template` を使います。
- `test-fire`
  synthetic な alert event を output pipeline に流し、dispatch summary を JSON で表示します。queue で待っている event より先に送り、最大 30 秒待ちます。それまでに送れなかった output は `pending_outputs` に出ます。
  `--state` か、`test-evaluate` の出力を `--evaluation-stdin` / `--evaluation-file` で与えます。後者では、rule が判定した state、severity、message、値が通知に出ます。

共通引数:

- `--base-url`
  接続先の Kanary API URL を指定します。

例:

```bash
kanaryctl alerts
kanaryctl test-poll sqlite
kanaryctl test-evaluate sqlite.value1.range --print-template
kanaryctl test-evaluate sqlite.value1.range --payload-json '{"inputs":{"sqlite:value1":{"value":120,"timestamp":"2026-05-29T00:00:00+00:00"}},"status":"ok"}'
kanaryctl test-fire sqlite.value1.range --state FIRING --reason "output check"
kanaryctl test-poll sqlite | kanaryctl test-evaluate sqlite.value1.range --payload-stdin \
  | kanaryctl test-fire sqlite.value1.range --evaluation-stdin
kanaryctl alerts sqlite.value1.range
kanaryctl plugins sqlite --traceback
kanaryctl ack sqlite.value1.stale --operator operator_name --reason "investigating"
kanaryctl unack sqlite.value1.stale --operator operator_name --reason "re-open"
kanaryctl silence-for --operator operator_name --minutes 10 --rule 'sqlite.*'
kanaryctl silence-until --operator operator_name --start-at 2026-11-21T22:00 --end-at 2026-11-22T06:00 --rule 'daq.*'
kanaryctl reload --rule 'sqlite.*'
kanaryctl reload --dirty
```

追加の HTTP endpoint:

- `GET /test-evaluate-template/{rule_id}`
  canonical な `inputs` payload template を返します。可能なら single-input shorthand template も含みます。
