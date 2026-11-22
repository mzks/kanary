# Template 一覧

`kanary new` の全 template を、書き出されるとおりに載せています。`kanary new -t TEMPLATE PLUGIN_ID` で `TEMPLATE` から `PLUGIN_ID` を作り、`-o FILE -a` で既存の file に追記します。`kanary new --list` は同じ一覧を terminal に出します。

この page は `src/kanary/templates/` から `kanary new --catalog ja > docs/templates_ja.md` で生成しています。直すときはこの page ではなく template を直してください。

## 目次

**[BASIC](#basic)**: よく使う出発点。helper class 1 つで、ほぼ設定だけで使えます。

| 種類 | Template | 内容 |
|---|---|---|
| source | [source](#source) | 外部の system を一定間隔か cron の schedule で poll する。 |
| source | [command](#command) | 手元の command を実行し、出力から数値を読む (CommandSource)。 |
| source | [ping](#ping) | host に ping し、往復時間を出す。応答が無ければ値なし (PingSource)。 |
| source | [tcp](#tcp) | host:port に接続し、接続にかかった時間を出す。拒否や timeout なら値なし (TCPSource)。 |
| source | [http-status](#http-status) | URL に request し、HTTP status code と応答時間を出す。例: API の health check (HTTPStatusSource)。 |
| source | [file](#file) | file や glob pattern を見張る: ファイルの有無、最終更新からの時間、大きさ、個数、最終行の数値 (FileSource)。 |
| source | [http-json](#http-json) | JSON の API を GET し、path で選んだ値を数値にして出す (HTTPJSONSource)。 |
| source | [prometheus](#prometheus) | Prometheus の /metrics page から series を読む。例: node_exporter や application の metrics (PrometheusSource)。 |
| source | [sql-wide](#sql-wide) | wide 形式（量ごとに column）の table から最新の行を SQL で読む (SQLWideSource)。 |
| source | [sql-long](#sql-long) | long 形式の table から、名前ごとに最新の名前/値の行を SQL で読む (SQLLongSource)。 |
| source | [http-push](#http-push) | HTTP の POST で値を受け取り、届いた瞬間に評価する (HTTPPushSource)。 |
| source | [mqtt](#mqtt) | MQTT の topic を subscribe し、message が届いた瞬間に評価する (MQTTSource。paho-mqtt が必要)。 |
| source | [plugin-health](#plugin-health) | オススメ! この KANARY 自身の plugin を見張る: source、rule、output の失敗や、output が event を捨てたときに発報する (PluginHealthSource)。 |
| source | [heartbeat](#heartbeat) | この KANARY が生きていることを外部の service に伝える（healthchecks.io、Uptime Kuma の push など）。ping が止まると、その service が知らせてくれる。 |
| source | [peer](#peer) | 別の KANARY を見張る: 応答しない、plugin が失敗している、で発報する（source と rule が 1 file）。両方の node に置けば相互監視になる。 |
| rule | [rule](#rule) | evaluate(ctx) を自分で書く。input、履歴、severity をすべて使える。 |
| rule | [range](#range) | 値が [low, high] を外れたとき、または禁止帯に入ったときに発報する (RangeRule)。 |
| rule | [threshold](#threshold) | 値が複数の閾値を越えるたびに severity を段階的に上げる (ThresholdRule)。 |
| rule | [expected](#expected) | 値が期待どおりでないときに発報する。例: status code、0/1 の flag、状態 (ExpectedValueRule)。 |
| rule | [stale](#stale) | input が timeout より長く更新されなければ発報する (StaleRule)。 |
| output | [output](#output) | alert event をどこかへ届ける。routing の filter をすべて示す。 |
| output | [webhook](#webhook) | 各 alert event を JSON で HTTP の endpoint に POST する。body の形は payload() で変える (WebhookOutput)。 |
| output | [slack](#slack) | incoming webhook で Slack の channel に alert を投稿する (SlackOutput)。 |
| output | [mattermost](#mattermost) | incoming webhook で Mattermost に alert を投稿する (MattermostOutput)。 |
| output | [discord](#discord) | webhook で Discord の channel に alert を投稿する (DiscordOutput)。 |
| output | [file-log](#file-log) | すべての alert event を JSON Lines の file に追記する (FileOutput)。 |
| output | [mail](#mail) | SMTP で alert event を mail で送る (MailOutput)。 |
| output | [startup](#startup) | KANARY の起動時にメッセージを 1 つ送る。例: 停電の後に監視が戻ったことを知る（ここでは Slack。どの chat output でもよい）。 |

**[ADVANCED](#advanced)**: 使う場面が限られる helper、独自の logic、複数の機能の組み合わせ。

| 種類 | Template | 内容 |
|---|---|---|
| source | [ssh](#ssh) | ssh で別の host の command を実行する。例: disk、load、GPU の状態 (SSHCommandSource)。 |
| source | [log-count](#log-count) | 前回の poll 以降に増えた log の行のうち、正規表現に一致するものを数える (LogCountSource)。 |
| source | [push](#push) | 自分で書いた受け口（装置の callback、socket など）に届いた値を、届いた瞬間に評価する: kanary.Inbox。 |
| source | [buffered](#buffered) | 最近の履歴を kanary.Buffer に貯め、window の平均や最小値を出す。 |
| source | [trend](#trend) | 値の傾向と、上限に届くまでの時間を、最近の履歴から出す (kanary.Buffer)。 |
| source | [spike](#spike) | 最新の値が最近の履歴に比べてどれだけ外れているかを出し、spike や落ち込みを捉える (kanary.Buffer)。 |
| source | [frozen](#frozen) | 値が変わらない時間（固まった sensor）と変わる頻度（flapping）を、最近の履歴から出す (kanary.Buffer)。 |
| source | [remote](#remote) | 別の Kanary node の alert を映す (RemoteKanarySource + import_remote_alarms)。 |
| rule | [rate](#rate) | 2 つの snapshot の間で値が速く変わりすぎたら発報する (RateRule)。 |
| rule | [difference](#difference) | 冗長な input どうしの差が上限を超えたら発報する (DifferenceRule)。 |
| rule | [k-of-n](#k-of-n) | 多数の input のうち k 個以上が範囲外なら発報する。例: 「3 channel が死んでいる」 (KOfNRule)。 |
| rule | [aggregate](#aggregate) | 多数の input の合計や平均をまとめて範囲で判定する。例: 全 channel の電流の合計 (AggregateRule)。 |
| rule | [count](#count) | 値を返す input が期待より少ない（または多い）ときに発報する (CountRule)。 |
| rule | [multi-source](#multi-source) | 複数の source の input を 1 つの rule で組み合わせる（例: 2 つの sensor を比べる）。 |
| output | [command-output](#command-output) | event ごとに script を実行する。例: 電話の自動発信や警告灯 (CommandOutput)。 |
| output | [followups](#followups) | 時間とともに通知を広げる: すぐ通知し、時間を置いて再通知し、ack で取り消す (OutputFollowups)。 |
| output | [delayed](#delayed) | 一定時間たっても alert が続いているときだけ通知する。"for" / fire_after に相当 (OutputFollowups)。 |
| output | [quiet-hours](#quiet-hours) | 急ぎでない alert を夜間は保留し、朝にまとめて送る (QuietHours)。 |
| output | [oncall](#oncall) | 当番表の file から今の当番に mail し、夜間は mail を保留する (MailOutput + OnCallSchedule + QuietHours)。 |

## BASIC

### Source 系

#### source

source · 外部の system を一定間隔か cron の schedule で poll する。

```console
$ kanary new -t source my.source
```

```python
import kanary


@kanary.source(
    source_id="my.source",
    interval=1 * kanary.minute,
    # Or a cron-like schedule instead of interval, in the host's local time
    # ("minute hour day month weekday"):
    # schedule="*/5 * * * *",
    # Shortcuts: "@hourly" (minute 0), "@daily" = "@midnight" (00:00),
    #            "@weekly" (Sunday 00:00), "@monthly" (the 1st, 00:00),
    #            "@yearly" = "@annually" (January 1, 00:00).
)
class MySource:
    description = "TODO: describe what this source reads."

    # Retries after a poll() failure, then terminate() + init() attempts.
    max_retry = 1
    max_reinit = 1

    def init(self):
        # Open connections and clients here, not at module top level.
        # Settings can live next to this file:
        #   config = kanary.load_toml("my_source", filename="config.toml")
        self.client = None

    def poll(self):
        # Always use finite timeouts for HTTP, database, and socket I/O:
        # poll() runs in this source's own thread, but a hang stops this source.
        #
        # Each input is (name, value) or (name, value, timestamp, metadata).
        # Rules select them by source and name, here "my.source:temperature".
        return kanary.inputs(
            ("temperature", 21.5),
            ("humidity", 40.0, None, {"unit": "%"}),
            metadata={"host": "TODO"},
        )

        # Other results:
        # return kanary.no_data(reason="query returned no rows")    # empty snapshot; rules run
        # return kanary.no_update(reason="nothing new since last")  # keep last snapshot; rules run
        # return kanary.skip(reason="maintenance window")           # ignore this poll; rules do not run

    def terminate(self):
        # Close what init() opened.
        self.client = None
```

[目次へ](#目次)

#### command

source · 手元の command を実行し、出力から数値を読む (CommandSource)。

```console
$ kanary new -t command my.command
```

```python
import kanary


@kanary.source(source_id="my.command", interval=1 * kanary.minute)
class MyCommandSource(kanary.CommandSource):
    description = "TODO: describe what the command measures."

    # A list is run as-is; a string is split like a shell would, but no shell is used
    # (no pipes or globs). For a pipeline, run ["sh", "-c", "... | ..."] explicitly.
    command = ["df", "-P", "/"]  # TODO
    timeout = 10.0  # seconds; a timeout counts as a poll failure (retry, then reinit)

    # How stdout becomes inputs (all values are converted to numbers):
    #   "number"   "42"                  -> value=42.0            (name: value_name)
    #   "keyvalue" "load=0.8 users=3"    -> load=0.8, users=3.0
    #   "json"     '{"a": {"b": 1}}'     -> a.b=1.0
    #   "auto"     picks one of the above from the output's shape
    # "85%" becomes 85.0, true/false become 1.0/0.0. Anything else that is not a number
    # is kept as text in the result metadata rather than as an input.
    output_format = "auto"
    value_name = "value"

    # The exit code is reported as the input "exit_code"; alert on it with a RangeRule
    # (high = 0). Set False to leave it out.
    report_exit_code = True

    # env = {"LC_ALL": "C"}  # added to the inherited environment
    # cwd = "/tmp"

    def parse(self, stdout):
        # Override when the output needs custom parsing; return (numbers, texts).
        # Here: `df -P /` prints a header, then "fs blocks used avail 42% /".
        lines = stdout.strip().splitlines()
        fields = lines[-1].split() if lines else []
        if len(fields) < 5:
            raise ValueError(f"unexpected df output: {stdout!r}")  # a poll failure when exit_code == 0
        return {"used_percent": kanary.to_number(fields[4])}, {}
```

[目次へ](#目次)

#### ping

source · host に ping し、往復時間を出す。応答が無ければ値なし (PingSource)。

```console
$ kanary new -t ping my.ping
```

```python
import kanary


@kanary.source(source_id="my.ping", interval=1 * kanary.minute)
class MyPingSource(kanary.PingSource):
    description = "TODO: describe the hosts that must answer."

    # Input name -> host. "gateway" below produces "gateway.rtt_ms", the round-trip time of one
    # ICMP echo (system ping command). A host that does not answer within timeout gets
    # an input with no value and the error text in its metadata.
    hosts = {
        "gateway": "192.0.2.1",  # TODO
        "daq01": "daq01.example.org",
    }
    timeout = 5.0  # seconds per host; hosts are pinged in parallel


# Alert when a host does not answer, or answers slowly:
#
#   @kanary.rule(rule_id="my.ping.slow_or_down", inputs="my.ping:*.rtt_ms",
#                severity=kanary.ERROR, tags=["network"])
#   class PingSlowOrDown(kanary.RangeRule):
#       high = 200.0          # ms
#       missing_fires = True  # no reply fires too
```

[目次へ](#目次)

#### tcp

source · host:port に接続し、接続にかかった時間を出す。拒否や timeout なら値なし (TCPSource)。

```console
$ kanary new -t tcp my.tcp
```

```python
import kanary


@kanary.source(source_id="my.tcp", interval=1 * kanary.minute)
class MyTcpSource(kanary.TCPSource):
    description = "TODO: describe the services that must accept connections."

    # Input name -> "host:port". "daq" below produces "daq.connect_ms", how long the TCP
    # connect took. A refused or timed-out connect gets an input with no value and
    # the error text in its metadata. Nothing is sent after connecting.
    targets = {
        "daq": "daq01.example.org:5000",  # TODO
        "db": "db.example.org:5432",
    }
    timeout = 5.0  # seconds per target; targets are checked in parallel


# Alert when a port does not accept connections:
#
#   @kanary.rule(rule_id="my.tcp.down", inputs="my.tcp:*.connect_ms",
#                severity=kanary.ERROR, tags=["network"])
#   class PortDown(kanary.RangeRule):
#       high = 1000.0         # ms; a very slow connect also fires
#       missing_fires = True  # refused or timed out fires
```

[目次へ](#目次)

#### http-status

source · URL に request し、HTTP status code と応答時間を出す。例: API の health check (HTTPStatusSource)。

```console
$ kanary new -t http-status my.http-status
```

```python
import kanary


@kanary.source(source_id="my.http-status", interval=1 * kanary.minute)
class MyHttpStatusSource(kanary.HTTPStatusSource):
    description = "TODO: describe the web services or APIs."

    # Input name -> URL. "web" below produces "web.status" (the HTTP status code, e.g.
    # 200 or 503) and "web.response_ms". When nothing answers at all (DNS failure,
    # refused, timeout), both have no value and the error text is in the metadata.
    # To read values out of a JSON response, use the "http-json" template instead.
    urls = {
        "web": "https://example.org/",  # TODO
        "api": "https://api.example.org/health",
    }
    # Extra request headers; a token from an environment variable, as in the
    # "http-json" template:
    #   headers = {"Authorization": f"Bearer {os.environ.get('API_TOKEN', '')}"}
    timeout = 5.0  # seconds per URL; URLs are requested in parallel


# Alert unless the status is 200, including when nothing answers:
#
#   @kanary.rule(rule_id="my.http-status.not_ok", inputs="my.http-status:*.status",
#                severity=kanary.ERROR, tags=["web"])
#   class NotOk(kanary.ExpectedValueRule):
#       expected = 200        # or [200, 204]
#       missing_fires = True  # no answer fires too
```

[目次へ](#目次)

#### file

source · file や glob pattern を見張る: ファイルの有無、最終更新からの時間、大きさ、個数、最終行の数値 (FileSource)。

```console
$ kanary new -t file my.file
```

```python
import kanary


@kanary.source(source_id="my.file", interval=1 * kanary.minute)
class MyFileSource(kanary.FileSource):
    description = "TODO: describe the files and who writes them."

    # Input name -> path or glob pattern (relative paths are next to this plugin file).
    # A path, such as "backup" below, produces:
    #   backup.exists       1 or 0
    #   backup.age_seconds  seconds since the last modification
    #   backup.size_bytes
    #   backup.value        the last line as a number (only with read_last_line)
    # A pattern such as "runs" ("/data/run_*.root", or "/data/**/*.root" for subfolders)
    # produces the same for the newest matching file, plus runs.count and
    # runs.total_size_bytes.
    paths = {
        "backup": "/var/backups/daily.tar.gz",   # TODO
        "runs": "/data/runs/*.root",
        "level": "/run/sensors/level.txt",
    }
    # A plain list also works; names come from the file names ("daily.tar.gz") or the
    # fixed part of a pattern ("*.root" -> "root"):
    #   paths = ["/var/backups/daily.tar.gz", "/data/runs/*.root"]
    read_last_line = True


# "The backup has not been written for 26 hours", "no new run file for 2 hours":
#
#   @kanary.rule(rule_id="my.file.backup.old", inputs="my.file:backup.age_seconds",
#                severity=kanary.ERROR, tags=["backup"])
#   class BackupTooOld(kanary.RangeRule):
#       high = 26 * kanary.hour
#
#   @kanary.rule(rule_id="my.file.runs.old", inputs="my.file:runs.age_seconds",
#                severity=kanary.WARN, tags=["daq"])
#   class NoNewRun(kanary.RangeRule):
#       high = 2 * kanary.hour
#       missing_fires = True  # no matching file at all fires too
```

[目次へ](#目次)

#### http-json

source · JSON の API を GET し、path で選んだ値を数値にして出す (HTTPJSONSource)。

```console
$ kanary new -t http-json my.http-json
```

```python
import os

import kanary


@kanary.source(source_id="my.http-json", interval=1 * kanary.minute)
class MyHttpJsonSource(kanary.HTTPJSONSource):
    description = "TODO: describe the API."

    url = "https://api.example.org/v1/status"  # TODO
    timeout = 5.0

    # Input name -> dotted path into the JSON; list items by index ("sensors.0.value").
    # Values are converted to numbers ("12.5" -> 12.5, true -> 1.0). Missing paths and
    # non-numeric values are listed in the result metadata. Leave fields empty to
    # publish every numeric value under its dotted path.
    fields = {
        "temperature": "data.temperature",
        "pump_running": "data.pump.running",
        "first_sensor": "data.sensors.0.value",
    }

    # A token is a secret: keep it out of this file, for example in an environment
    # variable. Without it the API answers 401 and the poll fails with that.
    headers = {"Authorization": f"Bearer {os.environ.get('API_TOKEN', '')}"}  # TODO
    # Or from secrets.json next to this file (chmod 600, not in git), read when this
    # file is loaded: {"api_token": "..."}
    # headers = {"Authorization": f"Bearer {kanary.load_json('api_token', filename='secrets.json')}"}

    # Override select(document) for documents that need more than paths:
    # def select(self, document):
    #     return {item["name"]: item["value"] for item in document["sensors"]}
```

[目次へ](#目次)

#### prometheus

source · Prometheus の /metrics page から series を読む。例: node_exporter や application の metrics (PrometheusSource)。

```console
$ kanary new -t prometheus my.prometheus
```

```python
import kanary


@kanary.source(source_id="my.prometheus", interval=30 * kanary.second)
class MyPrometheusSource(kanary.PrometheusSource):
    description = "TODO: describe the exporter."

    url = "http://localhost:9100/metrics"  # TODO (9100: node_exporter)
    timeout = 5.0
    # A token, when the page needs one (from an environment variable; import os):
    # headers = {"Authorization": f"Bearer {os.environ.get('METRICS_TOKEN', '')}"}

    # Input name -> series, written as in Prometheus. The labels written must match
    # exactly; the others may be anything. When a series selector matches several series, name
    # the label that varies: {sensor} below becomes each series' sensor label
    # (temp.temp1, temp.temp2, ...).
    metrics = {
        "load1": "node_load1",
        "root_free_bytes": 'node_filesystem_avail_bytes{mountpoint="/"}',
        "temp.{sensor}": 'node_hwmon_temp_celsius{chip="platform_coretemp_0"}',
        "requests_5xx": 'http_requests_total{code="500"}',
    }
    # A series not on the page is listed in the result's metadata["missing"]; NaN
    # has no value. The page itself not answering is a poll failure.


# A counter (..._total) only grows; a RateRule gives its rate per second:
#
#   @kanary.rule(rule_id="my.prometheus.errors", inputs="my.prometheus:requests_5xx",
#                severity=kanary.WARN, tags=["web"])
#   class ServerErrors(kanary.RateRule):
#       high = 5 / kanary.minute
#
# Less than 10 GiB free: a RangeRule with low = 10 * 1024**3 on root_free_bytes.
```

[目次へ](#目次)

#### sql-wide

source · wide 形式（量ごとに column）の table から最新の行を SQL で読む (SQLWideSource)。

```console
$ kanary new -t sql-wide my.sql-wide
```

```python
import kanary


@kanary.source(source_id="my.sql-wide", interval=30 * kanary.second)
class MySqlWideSource(kanary.SQLWideSource):
    description = "TODO: describe the table."

    # Connection. "sqlite" and "postgresql" (psycopg comes with KANARY) need nothing
    # extra; "mysql" (also MariaDB) needs the pymysql package in the Python environment
    # that runs KANARY (docs/deployment.md, "When A Plugin Needs Additional Python Modules").
    database_type = "sqlite"
    database = "environment.db"  # sqlite: file path (relative = next to this plugin); others: database name
    # host = "db.example.org"
    # port = 5432                  # default 5432 (postgresql) / 3306 (mysql)
    # user = "kanary"
    # The password is a secret: keep it out of this file, for example in an
    # environment variable (import os), or in secrets.json next to this file
    # (chmod 600, not in git) as {"db_password": "..."}:
    # password = os.environ.get("DB_PASSWORD")
    # password = kanary.load_json("db_password", filename="secrets.json")
    timeout = 5.0                  # connect and statement timeout, seconds

    # A wide table: one row per time, one column per quantity (for name/value rows, use
    # the "sql-long" template; docs/plugins.md shows both shapes). The newest row is read:
    #   SELECT observed_at, temperature, humidity, pump_state FROM environment
    #   ORDER BY observed_at DESC LIMIT 1
    table = "environment"
    timestamp_column = "observed_at"  # each input gets this row's time
    # Zone of timestamps stored without one: an IANA name ("Asia/Tokyo"), an offset
    # ("+09:00", "+9"), or "UTC". The default is this host's local time.
    # timestamp_timezone = "UTC"

    # Input name -> column. Leave empty to use every column of the table (except the
    # timestamp) under its own name. Non-numeric values go to metadata["text"].
    columns = {
        "temperature": "temperature",
        "humidity": "humidity",
        "pump_on": "pump_state",
    }

    # To read anything else (a join, a view, a WHERE clause), write the SQL yourself.
    # When query is set, table and columns are not used. Use the driver's parameter
    # style for params (sqlite: ?, postgresql / mysql: %s), and put the newest row first.
    #   query = """
    #       SELECT observed_at, temperature, humidity, pump_state
    #       FROM environment WHERE station = ?
    #       ORDER BY observed_at DESC LIMIT 1
    #   """
    #   params = ("north",)
    #
    # For another driver or connection option, override connect() and return any
    # DB-API connection (with timeouts and autocommit). A failing poll is retried,
    # then terminate() + init() reconnect.
```

[目次へ](#目次)

#### sql-long

source · long 形式の table から、名前ごとに最新の名前/値の行を SQL で読む (SQLLongSource)。

```console
$ kanary new -t sql-long my.sql-long
```

```python
import kanary


@kanary.source(source_id="my.sql-long", interval=30 * kanary.second)
class MySqlLongSource(kanary.SQLLongSource):
    description = "TODO: describe the table."

    # Connection: see the sql-wide template for postgresql / mysql settings.
    database_type = "sqlite"
    database = "samples.db"  # sqlite: file path (relative = next to this plugin); others: database name
    timeout = 5.0

    # A long table: one row per quantity and time, with a name and a value column (for
    # one column per quantity, use the "sql-wide" template; docs/plugins.md shows both
    # shapes). The newest row of every name is read.
    table = "samples"
    name_column = "metric_name"
    value_column = "metric_value"
    timestamp_column = "observed_at"
    # Zone of timestamps stored without one: an IANA name ("Asia/Tokyo"), an offset
    # ("+09:00", "+9"), or "UTC". The default is this host's local time.
    # timestamp_timezone = "UTC"
    # Only scan rows of the last 10 minutes: keeps the query cheap on a large table.
    lookback = 10 * kanary.minute

    # Upstream name -> input name. When set, other names are ignored; when empty,
    # every name is used as-is. The upstream name is kept in each input's metadata.
    names = {
        "plant.room_a.temperature_c": "room_a.temperature",
        "plant.room_b.temperature_c": "room_b.temperature",
    }

    # To write the SQL yourself, set query (table and the column settings are then
    # not used). Return name, value, and timestamp columns, newest first; the first
    # row per name wins.
    #   query = """
    #       SELECT observed_at, metric_name, metric_value FROM samples
    #       WHERE observed_at > datetime('now', '-10 minutes')
    #       ORDER BY observed_at DESC
    #   """
```

[目次へ](#目次)

#### http-push

source · HTTP の POST で値を受け取り、届いた瞬間に評価する (HTTPPushSource)。

```console
$ kanary new -t http-push my.http-push
```

```python
import os

import kanary


# No interval or schedule here: HTTPPushSource runs with interval = 0, so poll() waits
# for values and the rules run as soon as one arrives. Any other makes init() fail.
@kanary.source(source_id="my.http-push")
class MyHttpPushSource(kanary.HTTPPushSource):
    description = "TODO: describe who sends values here."

    host = "127.0.0.1"  # "0.0.0.0" accepts from other hosts
    port = 8911
    path = "/"
    # With a token, senders need "Authorization: Bearer <token>". Keep it out of this
    # file, e.g. in an environment variable; None accepts everyone who can connect.
    token = os.environ.get("KANARY_PUSH_TOKEN")

    # Senders POST a JSON object of input name -> value:
    #   curl -X POST http://127.0.0.1:8911/ -H "Authorization: Bearer $KANARY_PUSH_TOKEN" \
    #        -d '{"temperature": 21.5, "humidity": 40}'
    # A value may carry its own time (unix time or ISO 8601):
    #   {"temperature": {"value": 21.5, "timestamp": "2026-11-20T10:00:00+09:00"}}
    # Values posted together are evaluated together; a burst is evaluated once, with
    # the newest value of each input. Inputs not in a request keep their last value.
    # The server is http.server: fine on a trusted network; from the internet, put a
    # reverse proxy with TLS in front.


# Nothing arriving is not an error for this source; a stale rule catches it:
#
#   @kanary.rule(rule_id="my.http-push.stale", inputs="my.http-push:*",
#                severity=kanary.WARN, evaluation_interval=30 * kanary.second)
#   class PushStale(kanary.StaleRule):
#       timeout = 5 * kanary.minute
```

[目次へ](#目次)

#### mqtt

source · MQTT の topic を subscribe し、message が届いた瞬間に評価する (MQTTSource。paho-mqtt が必要)。

```console
$ kanary new -t mqtt my.mqtt
```

```python
import os

import kanary


# No interval or schedule here: MQTTSource runs with interval = 0, so poll() waits
# for messages and the rules run as soon as one arrives. Any other makes init() fail.
@kanary.source(source_id="my.mqtt")
class MyMqttSource(kanary.MQTTSource):
    description = "TODO: describe the broker and the devices."

    host = "mqtt.example.org"  # TODO
    port = 1883                # usually 8883 with tls = True
    # tls = True
    # Keep the login out of this file, e.g. in environment variables.
    username = os.environ.get("MQTT_USERNAME")
    password = os.environ.get("MQTT_PASSWORD")
    qos = 1

    # Topic filter -> input name. None names the input after the topic ("/" -> "."),
    # as every filter with a wildcard must: "+" matches one level, "#" the rest.
    topics = {
        "plant/room_a/temperature": "room_a.temperature",  # TODO
        "plant/pumps/#": None,  # plant/pumps/p1/flow -> plant.pumps.p1.flow
    }

    # A payload is a number ("21.5"), a state ("ON" / "true" -> 1.0, "OFF" -> 0.0),
    # other text ("RUNNING", kept as it is), or JSON. A JSON object gives one input
    # per key: {"t": 21.5, "rh": 40} on "room_a" -> room_a.t, room_a.rh.
    # "mqtt.connected" is 1 while connected; the client reconnects by itself and
    # subscribes again.
    # Needs the paho-mqtt package in the Python environment that runs KANARY: see
    # docs/deployment.md, "When A Plugin Needs Additional Python Modules".


# Alert when the broker is unreachable, and when values stop coming:
#
#   @kanary.rule(rule_id="my.mqtt.disconnected", inputs="my.mqtt:mqtt.connected",
#                severity=kanary.ERROR)
#   class MqttDisconnected(kanary.ExpectedValueRule):
#       expected = 1
#
#   @kanary.rule(rule_id="my.mqtt.stale", inputs="my.mqtt:room_a.temperature",
#                severity=kanary.WARN, evaluation_interval=30 * kanary.second)
#   class MqttStale(kanary.StaleRule):
#       timeout = 5 * kanary.minute
```

[目次へ](#目次)

#### plugin-health

source · オススメ! この KANARY 自身の plugin を見張る: source、rule、output の失敗や、output が event を捨てたときに発報する (PluginHealthSource)。

```console
$ kanary new -t plugin-health my.plugin-health
```

```python
import kanary


@kanary.source(source_id="my.plugin-health", interval=30 * kanary.second)
class MyPluginHealthSource(kanary.PluginHealthSource):
    description = "The plugins of this KANARY: failures, slow rules, and output backlog."
    # It reads the engine it runs in, so it needs no settings. For every other plugin:
    #   source.daq.failed / rule.oil.high.failed / output.slack.failed
    #                           1 while FAILED (the error is in the metadata), else 0
    #   output.slack.queued     events waiting to be sent
    #   output.slack.dropped    events dropped since the previous poll (queue full)
    #   rule.oil.high.slow      1 while evaluate() is slow


@kanary.rule(
    rule_id="my.plugin-health.failed",
    inputs="my.plugin-health:*.failed",
    severity=kanary.ERROR,
    tags=["kanary"],
)
class PluginFailed:
    description = "A plugin of this KANARY failed; KANARY retries it or waits for a fix."
    runbook = "kanaryctl plugins PLUGIN_ID --traceback shows the whole error. Fix the plugin or its settings, then reload it."

    def evaluate(self, ctx):
        failed = [item for item in ctx.inputs() if item.value]
        if not failed:
            return kanary.ok(f"all {len(ctx.inputs())} plugins are working")
        names = [item.input_name.removesuffix(".failed") for item in failed]
        details = [f"{name}: {item.metadata.get('error') or 'failed'}" for name, item in zip(names, failed)]
        return kanary.firing(f"{len(failed)} plugin(s) failed - " + "; ".join(details), extra={"failed": names})


@kanary.rule(
    rule_id="my.plugin-health.dropped",
    inputs="my.plugin-health:output.*.dropped",
    severity=kanary.WARN,
    tags=["kanary"],
)
class OutputDropping(kanary.RangeRule):
    description = "An output dropped alert events: its destination is down or too slow."
    high = 0


# An alert about a failed output goes out through the other outputs; with a single
# output, only the viewer shows it. KANARY itself stopping cannot be seen from inside:
# for that, watch it from another KANARY (the "peer" template) or from outside (the
# "heartbeat" template).
```

[目次へ](#目次)

#### heartbeat

source · この KANARY が生きていることを外部の service に伝える（healthchecks.io、Uptime Kuma の push など）。ping が止まると、その service が知らせてくれる。

```console
$ kanary new -t heartbeat my.heartbeat
```

```python
import kanary


@kanary.source(source_id="my.heartbeat", interval=1 * kanary.minute)
class MyHeartbeatSource(kanary.HTTPStatusSource):
    description = "Pings an outside dead man's switch: when the pings stop, it alerts (KANARY, its host, or its network is down)."

    # The ping URL of the check; set the check's period a little longer than the
    # interval above (and a grace time), so one late ping does not alert.
    urls = {"ping": "https://hc-ping.com/TODO-check-uuid"}  # TODO
    timeout = 10.0


@kanary.rule(
    rule_id="my.heartbeat.not_delivered",
    inputs="my.heartbeat:ping.status",
    severity=kanary.WARN,
    tags=["kanary"],
)
class HeartbeatNotDelivered(kanary.ExpectedValueRule):
    description = "The heartbeat did not reach the outside service, which will soon alert the people it notifies."
    runbook = "TODO: check this host's network to the outside, and the service's status page."
    expected = 200
    missing_fires = True  # no answer at all fires too


# This is the watch KANARY cannot do for itself: from inside, it cannot report that
# it stopped. To ping only while its own plugins work, read them in poll() first
# (the "plugin-health" template) and skip the request while one fails.
```

[目次へ](#目次)

#### peer

source · 別の KANARY を見張る: 応答しない、plugin が失敗している、で発報する（source と rule が 1 file）。両方の node に置けば相互監視になる。

```console
$ kanary new -t peer my.peer
```

```python
import kanary


@kanary.source(source_id="my.peer", interval=30 * kanary.second)
class MyPeerSource(kanary.HTTPJSONSource):
    description = "TODO: which KANARY this watches."

    url = "http://peer-kanary.example:8000/peer-status"  # TODO: the other node's API
    timeout = 5.0

    def poll(self):
        # No answer is what this source reports (up = 0), not a failure of its own.
        try:
            status = self.fetch_json()
        except Exception as exc:
            return kanary.inputs(("up", 0, None, {"error": f"{type(exc).__name__}: {exc}"}))
        counts = status.get("counts") or {}
        firing = (status.get("alert_states") or {}).get("FIRING")
        return kanary.inputs(("up", 1), ("failed_plugins", counts.get("failed_plugins")), ("firing", firing))


@kanary.rule(
    rule_id="my.peer.down",
    inputs="my.peer:up",
    severity=kanary.ERROR,
    tags=["kanary"],
)
class PeerDown(kanary.ExpectedValueRule):
    description = "The other KANARY does not answer: it, its host, or the network between is down."
    runbook = "TODO: how to reach the other host; kanaryctl --base-url http://peer-kanary.example:8000 plugins"
    expected = 1


@kanary.rule(
    rule_id="my.peer.plugins_failed",
    inputs="my.peer:failed_plugins",
    severity=kanary.WARN,
    tags=["kanary"],
    suppressed_by="my.peer.down",  # no answer says it all
)
class PeerPluginsFailed(kanary.RangeRule):
    description = "Plugins of the other KANARY fail: it runs, but does not watch everything."
    high = 0


# Put the same file on the other node with this node's URL, and each watches the
# other. Both may stop together (one power line, one network): for that, add an
# outside watch as well (the "heartbeat" template).
```

[目次へ](#目次)

### Rule 系

#### rule

rule · evaluate(ctx) を自分で書く。input、履歴、severity をすべて使える。

```console
$ kanary new -t rule my.rule
```

```python
import kanary


@kanary.rule(
    rule_id="my.rule",
    # One selector, a list, or globs: "src:name", ["a:x", "b:y"], "src:*", "kernel_*:temp".
    inputs="example.source:temperature",  # TODO
    severity=kanary.WARN,  # default severity for kanary.firing(); INFO / WARN / ERROR / CRITICAL
    tags=["TODO"],  # Outputs route on tags
    # owner="detector-team",
    # While any of these fire, this rule is SUPPRESSED (not notified):
    # suppressed_by=["daq.link"],  # still evaluated: the viewer shows what it would say
    # depends_on=["daq.link"],     # not evaluated at all (it keeps history, or cannot work without them)
    # evaluation_interval=30 * kanary.second,  # also evaluate on a timer, not only on source updates
)
class MyRule:
    description = "TODO: describe when this rule fires."
    # runbook = "TODO: what to check first, or a link"  # also in the notifications

    # Your own setting: any class attribute works. KANARY does not read this name;
    # only evaluate() below uses it.
    high_temperature = 80.0

    def evaluate(self, ctx):
        # Keep evaluate() fast and free of I/O: rules are evaluated one at a time, so
        # every other evaluation waits for this one (over 100 ms shows as SLOW).
        # Fetch everything you need in a Source instead.
        value = ctx.value()  # the single selected input; ctx.value("src:name") picks one
        if value is None:
            # No value to judge. OK here is "not judged", as the helper rules do by
            # default (missing_fires = False): data that stops coming is the job of a
            # StaleRule on the same input, so one cause does not alert twice. Return
            # kanary.firing(...) instead when a missing value is itself the problem.
            return kanary.ok("temperature has no value")

        previous = ctx.prev_value()  # value from the previous snapshot, or None
        # Also: ctx.timestamp(), ctx.metadata(), ctx.inputs(), ctx.values(),
        #       ctx.age()     seconds since the input's timestamp
        #       ctx.delta()   value - previous value (None unless both are numbers)
        #       ctx.changed() True when the value differs from the previous one
        #       ctx.missing() declared inputs that are absent or have no value
        #       ctx.previous_alert (last state of this rule), ctx.now.
        high = self.high_temperature
        extra = {"previous": previous, "high_temperature": high}

        # Return the first helper that matches; *_if() returns None when the condition is false.
        return (
            kanary.critical_if(value > high + 20, f"temperature={value} is far over {high}", extra=extra)
            or kanary.error_if(value > high + 10, f"temperature={value} is over {high + 10}", extra=extra)
            or kanary.warn_if(value > high, f"temperature={value} is over {high}", extra=extra)
            or kanary.ok(f"temperature={value} is normal", extra=extra)
        )
        # kanary.firing(message) fires with the decorator severity;
        # kanary.firing(message, severity=kanary.ERROR) overrides it for this evaluation.
```

[目次へ](#目次)

#### range

rule · 値が [low, high] を外れたとき、または禁止帯に入ったときに発報する (RangeRule)。

```console
$ kanary new -t range my.range
```

```python
import kanary


@kanary.rule(
    rule_id="my.range",
    # With a glob such as "example.source:*", each input is checked independently
    # and any out-of-range input fires the rule.
    inputs="example.source:temperature",  # TODO
    severity=kanary.WARN,
    tags=["TODO"],
    # owner="detector-team",
    # depends_on=[...], suppressed_by=[...], evaluation_interval=...  (see the "rule" template)
)
class MyRangeRule(kanary.RangeRule):
    description = "TODO: describe the normal range."
    # runbook = "TODO: what to check first, or a link"  # also in the notifications

    # The range [low, high]. Set either one to None for a one-sided check; never both.
    low = 10.0
    high = 30.0

    # "outside" (default): [low, high] is normal; leaving it fires.
    # "inside":            [low, high] is forbidden; entering it fires
    #                      (e.g. a resonance band 48-52 Hz that must be passed quickly).
    fire_when = "outside"

    # Whether the bound itself belongs to [low, high].
    #   True  (default): value == low / high is inside   -> [low, high]
    #   False:           value == low / high is outside  -> (low, high)
    # With fire_when="outside" an inside value is normal; with "inside" it fires.
    lower_inclusive = True
    upper_inclusive = True

    # After firing, the value must move this far back before the alert clears:
    #   outside: 0.5 with high = 30.0 clears only at 29.5 or below.
    #   inside:  0.5 with [48, 52] clears only below 47.5 or above 52.5.
    hysteresis = 0.0

    # An input with no value (a probe without an answer, an empty column) is not
    # judged and leaves the rule OK. True: it fires too. For data that stops coming,
    # a StaleRule on the same input is the usual check (it also sees a dead source).
    missing_fires = False
```

[目次へ](#目次)

#### threshold

rule · 値が複数の閾値を越えるたびに severity を段階的に上げる (ThresholdRule)。

```console
$ kanary new -t threshold my.threshold
```

```python
import kanary


@kanary.rule(
    rule_id="my.threshold",
    # With several inputs, the highest matched severity among them wins.
    inputs="example.source:temperature",  # TODO
    severity=kanary.WARN,  # rule default; each evaluation uses the matched threshold's severity
    tags=["TODO"],
    # owner="detector-team",
)
class MyThresholdRule(kanary.ThresholdRule):
    description = "TODO: describe what each level means."
    # runbook = "TODO: what to check first, or a link"  # also in the notifications

    # "high": fire when value >= threshold.  "low": fire when value <= threshold.
    direction = "high"

    # (threshold, severity) pairs; the most severe matched pair wins.
    thresholds = [
        (40.0, kanary.WARN),
        (50.0, kanary.ERROR),
        (60.0, kanary.CRITICAL),
    ]

    # Margin needed before severity drops or the rule clears (e.g. back below 38.0 for WARN).
    hysteresis = 2.0

    # An input with no value (a probe without an answer, an empty column) is not
    # judged and leaves the rule OK. True: it fires too. For data that stops coming,
    # a StaleRule on the same input is the usual check (it also sees a dead source).
    missing_fires = False
```

[目次へ](#目次)

#### expected

rule · 値が期待どおりでないときに発報する。例: status code、0/1 の flag、状態 (ExpectedValueRule)。

```console
$ kanary new -t expected my.expected
```

```python
import kanary


@kanary.rule(
    rule_id="my.expected",
    # With a glob such as "example.source:*.status", each input is checked independently.
    inputs="example.source:pump_on",  # TODO
    severity=kanary.WARN,
    tags=["TODO"],
    # owner="detector-team",
)
class MyExpectedRule(kanary.ExpectedValueRule):
    description = "TODO: describe the expected value."

    # One value or a list of allowed values. Numbers compare by value (1 == 1.0 == True),
    # and text values such as "RUNNING" work too.
    expected = 1

    # "different" (default): any other value fires.
    # "equal":               the listed values fire instead (e.g. error codes).
    fire_when = "different"

    # True: an input without a value (e.g. a probe that got no answer) fires too.
    missing_fires = False
```

[目次へ](#目次)

#### stale

rule · input が timeout より長く更新されなければ発報する (StaleRule)。

```console
$ kanary new -t stale my.stale
```

```python
import kanary


@kanary.rule(
    rule_id="my.stale",
    # The input's timestamp decides its age. With a glob, any stale input fires the rule.
    inputs="example.source:temperature",  # TODO
    severity=kanary.ERROR,
    tags=["TODO"],
    # Without a timer, a source that stops polling also stops this rule from running,
    # so stale detection needs evaluation_interval.
    evaluation_interval=30 * kanary.second,
)
class MyStaleRule(kanary.StaleRule):
    description = "TODO: describe what stopped when this fires."
    # runbook = "TODO: e.g. check that the DAQ writer is running."

    # Maximum age in seconds of the newest measurement.
    timeout = 5 * kanary.minute
```

[目次へ](#目次)

### Output 系

#### output

output · alert event をどこかへ届ける。routing の filter をすべて示す。

```console
$ kanary new -t output my.output
```

```python
import kanary


@kanary.output(
    output_id="my.output",
    # Routing: an event reaches emit() only when it passes every filter below.
    # include_tags=["detector"],           # alert must have one of these tags (globs allowed)
    # exclude_tags=["debug", "test-*"],    # alert must have none of these tags
    # minimum_severity=kanary.WARN,        # kanary.INFO / WARN / ERROR / CRITICAL
    # exclude_states=[],                   # default ["SUPPRESSED", "SILENCED"]; [] receives every state
    # exclude_states=kanary.Output.exclude_states + ["OK"],  # also skip recoveries (no "back to OK" message)
    # exclude_transitions=["UNACK"],       # also "ESCALATED", "DEESCALATED"
)
class MyOutput:
    description = "TODO: describe where this output sends alerts."

    # Retries after an emit() failure, then terminate() + init() attempts.
    max_retry = 1
    max_reinit = 1

    def init(self):
        # Create clients here, not at module top level.
        #   config = kanary.load_toml("my_output", filename="config.toml")
        pass

    def emit(self, event):
        # emit() runs on this output's own thread, one event at a time; a slow call
        # delays only this output's queue. Use finite timeouts for external I/O.
        # Raise to trigger retries.
        transition = f" ({event.transition.value})" if event.transition else ""
        previous = event.previous_state.value if event.previous_state else "-"
        print(
            f"{event.occurred_at.isoformat()} {event.rule_id} "
            f"{previous} -> {event.current_state.value}{transition} "
            f"{kanary.severity_label(event.effective_severity)} {event.message or ''}"
        )
        # Other fields: event.owner, event.tags, event.payload, event.active_since,
        # event.acked_by, event.ack_reason, event.active_silence_ids,
        # event.previous_severity, event.current_severity.
        # effective_severity keeps the firing severity on the event that returns to OK.

    def terminate(self):
        pass
```

[目次へ](#目次)

#### webhook

output · 各 alert event を JSON で HTTP の endpoint に POST する。body の形は payload() で変える (WebhookOutput)。

```console
$ kanary new -t webhook my.webhook
```

```python
import os

import kanary


@kanary.output(
    output_id="my.webhook",
    # include_tags=["detector"],
    minimum_severity=kanary.WARN,
)
class MyWebhookOutput(kanary.WebhookOutput):
    description = "TODO: describe the receiving service."

    # headers = {"X-Source": "kanary"}  # extra request headers (a secret one: see http-json)
    timeout = 5.0  # a hung server holds this output's queue for up to ~3x this (with retries)
    max_retry = 2

    # The URL is often a secret: keep it out of this file. Here it comes from an
    # environment variable (set it in the systemd unit, an EnvironmentFile, or the shell
    # that starts kanary); without it url stays None and this output fails its init()
    # with "url is not set".
    url = os.environ.get("WEBHOOK_URL")  # TODO
    # Or from a JSON file next to this one (chmod 600, not in git), read when this
    # file is loaded (a URL that is not secret can be a plain string): {"webhook_url": "https://hooks.example/..."}
    # url = kanary.load_json("webhook_url", filename="secrets.json")

    def payload(self, event):
        # The JSON body. The default (kept here) sends rule_id, state, previous_state,
        # transition, severity, message, owner, tags, occurred_at, active_since, acked_by,
        # and runbook.
        body = super().payload(event)
        body["source"] = "kanary"
        # self.text(event) gives a one-line summary such as
        #   "[ERROR] FIRING oil.temperature: value=95 out of range [10, 80]"
        body["summary"] = self.text(event)
        return body
```

[目次へ](#目次)

#### slack

output · incoming webhook で Slack の channel に alert を投稿する (SlackOutput)。

```console
$ kanary new -t slack my.slack
```

```python
import os

import kanary


@kanary.output(
    output_id="my.slack",
    minimum_severity=kanary.WARN,
    # include_tags=["detector"],
)
class MySlackOutput(kanary.SlackOutput):
    description = "TODO: name the channel."

    timeout = 5.0
    max_retry = 2

    # The webhook URL (Slack app > Incoming Webhooks) is a secret: keep it out of this
    # file. Here it comes from an environment variable (set it in the systemd unit, an
    # EnvironmentFile, or the shell that starts kanary); without it url stays None and
    # this output fails its init() with "url is not set".
    url = os.environ.get("SLACK_WEBHOOK_URL")  # TODO
    # Or from a JSON file next to this one (chmod 600, not in git), read when this
    # file is loaded: {"slack_webhook_url": "https://hooks.slack.com/services/..."}
    # url = kanary.load_json("slack_webhook_url", filename="secrets.json")

    # Each event is a message with a colored bar (green OK, grey silenced, else by
    # severity), "🔴 FIRING ERROR oil.temperature", the rule's message, and fields
    # (state, severity, since, ack, owner, tags, the judged values). Notifications show
    # the one line self.text(event). Link the title to the viewer:
    # viewer_url = "http://kanary.example.org:8000/"

    # To change it, override payload(); kanary.describe(event) gives the parts. For
    # example, mention the channel for ERROR and above:
    # def payload(self, event):
    #     body = super().payload(event)
    #     if event.current_state == kanary.FIRING and event.effective_severity >= kanary.ERROR:
    #         body["text"] = "<!channel> " + body["text"]
    #     return body
```

[目次へ](#目次)

#### mattermost

output · incoming webhook で Mattermost に alert を投稿する (MattermostOutput)。

```console
$ kanary new -t mattermost my.mattermost
```

```python
import os

import kanary


@kanary.output(
    output_id="my.mattermost",
    minimum_severity=kanary.WARN,
)
class MyMattermostOutput(kanary.MattermostOutput):
    description = "TODO: name the channel."

    username = "kanary"  # shown as the sender when the server allows overriding it
    timeout = 5.0
    max_retry = 2

    # The webhook URL (Integrations > Incoming Webhooks) is a secret: keep it out of
    # this file. Here it comes from an environment variable (set it in the systemd unit,
    # an EnvironmentFile, or the shell that starts kanary); without it url stays None
    # and this output fails its init() with "url is not set".
    url = os.environ.get("MATTERMOST_WEBHOOK_URL")  # TODO
    # Or from a JSON file next to this one (chmod 600, not in git), read when this
    # file is loaded: {"mattermost_webhook_url": "https://mattermost.example.org/hooks/..."}
    # url = kanary.load_json("mattermost_webhook_url", filename="secrets.json")

    # icon_url = "https://example.org/kanary.png"
    # Each event is an attachment with a colored bar, "🔴 FIRING ERROR oil.temperature",
    # the rule's message, and fields; link the title to the viewer:
    # viewer_url = "http://kanary.example.org:8000/"
    # To change it, override payload(); kanary.describe(event) gives the parts.
```

[目次へ](#目次)

#### discord

output · webhook で Discord の channel に alert を投稿する (DiscordOutput)。

```console
$ kanary new -t discord my.discord
```

```python
import os

import kanary


@kanary.output(
    output_id="my.discord",
    minimum_severity=kanary.WARN,
)
class MyDiscordOutput(kanary.DiscordOutput):
    description = "TODO: name the channel."

    username = "kanary"
    timeout = 5.0
    max_retry = 2

    # The webhook URL (Channel settings > Integrations > Webhooks) is a secret: keep it
    # out of this file. Here it comes from an environment variable (set it in the
    # systemd unit, an EnvironmentFile, or the shell that starts kanary); without it url
    # stays None and this output fails its init() with "url is not set".
    url = os.environ.get("DISCORD_WEBHOOK_URL")  # TODO
    # Or from a JSON file next to this one (chmod 600, not in git), read when this
    # file is loaded: {"discord_webhook_url": "https://discord.com/api/webhooks/..."}
    # url = kanary.load_json("discord_webhook_url", filename="secrets.json")

    # Each event is an embed with a colored bar, "🔴 FIRING ERROR oil.temperature",
    # the rule's message, fields, and the time (in each reader's own time zone);
    # link the title to the viewer:
    # viewer_url = "http://kanary.example.org:8000/"
    # To change it, override payload(); kanary.describe(event) gives the parts.
```

[目次へ](#目次)

#### file-log

output · すべての alert event を JSON Lines の file に追記する (FileOutput)。

```console
$ kanary new -t file-log my.file-log
```

```python
import kanary


@kanary.output(
    output_id="my.file-log",
    # Record everything, including silenced and suppressed alerts.
    exclude_states=[],
)
class MyFileLogOutput(kanary.FileOutput):
    description = "TODO: say who reads this file."

    # Relative paths are next to this plugin file. Rotate with logrotate (copytruncate).
    path = "alerts.jsonl"

    # One JSON object per line: rule_id, state, previous_state, transition, severity,
    # message, owner, tags, occurred_at, active_since, acked_by. True adds the rule's
    # payload (the judged values and extra) as "payload", for later analysis.
    include_payload = True

    # To reshape a line, override record(); kanary.event_record(event) is the default:
    # def record(self, event):
    #     return {**kanary.event_record(event), "host": "daq01"}
```

[目次へ](#目次)

#### mail

output · SMTP で alert event を mail で送る (MailOutput)。

```console
$ kanary new -t mail my.mail
```

```python
import kanary


@kanary.output(
    output_id="my.mail",
    # include_tags=["detector"],
    minimum_severity=kanary.ERROR,
)
class MyMailOutput(kanary.MailOutput):
    description = "TODO: describe who receives these mails."

    smtp_host = "smtp.example.org"  # TODO
    smtp_port = 587
    use_starttls = True
    smtp_timeout = 10.0
    smtp_username = None  # None: send without logging in

    sender = "kanary@example.org"  # TODO
    recipients = ["operator@example.org"]  # TODO
    subject_prefix = "[KANARY]"

    # Gmail: send through Google's SMTP server with an app password.
    #   smtp_host = "smtp.gmail.com"
    #   smtp_port = 587               # STARTTLS (use_starttls = True)
    #   smtp_username = "you@gmail.com"
    #   sender = "you@gmail.com"      # Gmail rewrites other From addresses to the login
    #   smtp_password: a 16-character app password, not the account password
    #     (Google Account > Security > 2-Step Verification must be on > App passwords).
    #     Workspace admins can disable app passwords; ask them or use your own SMTP relay.
    # Gmail limits sending (about 500 recipients a day for a personal account), so keep
    # minimum_severity high or route frequent events to a chat output instead.

    # With a login, the password is a secret: keep it out of this file, for example
    # in an environment variable (import os), or in secrets.json next to this file
    # (chmod 600, not in git) as {"smtp_password": "..."}:
    # smtp_password = os.environ.get("SMTP_PASSWORD")
    # smtp_password = kanary.load_json("smtp_password", filename="secrets.json")

    # Each mail has a plain-text part and an HTML part: a colored header
    # ("🔴 FIRING ERROR oil.temperature"), the rule's message, and a table of state,
    # severity, since, ack, owner, tags, and the judged values. Link it to the viewer:
    # viewer_url = "http://kanary.example.org:8000/"

    # To change it, override subject(), body() (plain text), or html() (None: text only);
    # kanary.describe(event) gives the parts:
    # def subject(self, event):
    #     return f"{self.subject_prefix} {kanary.describe(event).title}"
```

[目次へ](#目次)

#### startup

output · KANARY の起動時にメッセージを 1 つ送る。例: 停電の後に監視が戻ったことを知る（ここでは Slack。どの chat output でもよい）。

```console
$ kanary new -t startup my.startup
```

```python
from datetime import datetime
import os
import socket

import kanary


@kanary.output(
    output_id="my.startup",
    # It sends no alerts, only the message below.
    exclude_states=["OK", "FIRING", "ACKED", "SILENCED", "SUPPRESSED"],
)
class MyStartupOutput(kanary.SlackOutput):
    description = "Says once that KANARY started (or this output was reloaded)."

    url = os.environ.get("SLACK_WEBHOOK_URL")  # TODO: or the webhook of your channel

    def init(self):
        super().init()
        # init() runs again when the output recovers from a failed send: say it once.
        # If the destination cannot be reached yet, init() fails and is tried again.
        if not getattr(self, "announced", False):
            started = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
            self.post_json({"text": f"KANARY started on {socket.gethostname()} at {started}"})
            self.announced = True


# For Discord, subclass kanary.DiscordOutput and post {"content": ...}; for
# Mattermost, kanary.MattermostOutput and {"text": ...}; for mail, kanary.MailOutput.
```

[目次へ](#目次)

## ADVANCED

### Source 系

#### ssh

source · ssh で別の host の command を実行する。例: disk、load、GPU の状態 (SSHCommandSource)。

```console
$ kanary new -t ssh my.ssh
```

```python
import kanary


@kanary.source(source_id="my.ssh", interval=5 * kanary.minute)
class MySshSource(kanary.SSHCommandSource):
    description = "TODO: describe the remote host and what is measured."

    host = "daq01.example.org"  # TODO
    # user = "monitor"
    # port = 22
    # identity_file = "~/.ssh/kanary_monitor"   # a key without a passphrase, or an ssh-agent
    connect_timeout = 5.0
    # ssh_options = ["-o", "StrictHostKeyChecking=yes"]
    #
    # ssh runs with BatchMode=yes, so it never prompts. Exit code 255 (ssh itself
    # failed: unreachable, auth) is a poll failure, not the command's exit code.

    # Run on the remote host. A string goes to the remote shell as-is, so pipes work.
    # Print "name=value" pairs and they become inputs.
    command = (
        "printf 'load1=%s ' $(cut -d' ' -f1 /proc/loadavg); "
        "df -P / | awk 'NR==2 {print \"root_used_percent=\" $5}'"
    )
    timeout = 20.0  # covers connecting plus running
    output_format = "keyvalue"
```

[目次へ](#目次)

#### log-count

source · 前回の poll 以降に増えた log の行のうち、正規表現に一致するものを数える (LogCountSource)。

```console
$ kanary new -t log-count my.log-count
```

```python
import kanary


@kanary.source(source_id="my.log-count", interval=1 * kanary.minute)
class MyLogCountSource(kanary.LogCountSource):
    description = "TODO: describe the log and what the patterns mean."

    path = "/var/log/daq/run.log"  # TODO
    # Input name -> regular expression (Python re, searched anywhere in the line).
    # Each poll reports how many new lines matched, so `interval` is the counting window.
    patterns = {
        "errors": r"\bERROR\b",
        "timeouts": r"timed? ?out",
    }
    # True: ignore what is already in the file at startup.
    start_at_end = True
    # Rotation (new file, or truncated) restarts from the top of the new file.
    # The last matching line is kept in each input's metadata.


# "More than 10 errors per minute":
#
#   @kanary.rule(rule_id="my.log-count.errors", inputs="my.log-count:errors",
#                severity=kanary.WARN, tags=["daq"])
#   class TooManyErrors(kanary.RangeRule):
#       high = 10
```

[目次へ](#目次)

#### push

source · 自分で書いた受け口（装置の callback、socket など）に届いた値を、届いた瞬間に評価する: kanary.Inbox。

```console
$ kanary new -t push my.push
```

```python
import socket
import threading

import kanary


@kanary.source(
    source_id="my.push",
    # 0: call poll() again as soon as it returns. Only for a poll() that waits,
    # like the one below; a poll() that returns at once would spin the CPU.
    interval=0,
)
class MyPushSource(kanary.Source):
    description = "TODO: describe who sends values to this source."

    # For HTTP or MQTT, the "http-push" and "mqtt" templates need only settings.
    # This one is for any other listener; here, UDP datagrams such as
    #   echo "temperature 21.5" | nc -u -w1 127.0.0.1 8912
    listen_host = "127.0.0.1"
    listen_port = 8912

    def init(self):
        # Values arrive on the listener's thread and wait in the inbox until poll()
        # takes them, so none is lost while rules are being evaluated.
        self.inbox = kanary.Inbox()
        self.stopping = threading.Event()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((self.listen_host, self.listen_port))
        self.sock.settimeout(0.5)  # so that listen() sees stopping
        self.thread = threading.Thread(target=self.listen, daemon=True)
        self.thread.start()

    def listen(self):
        # Your listener, on its own thread. Anything that calls self.inbox.put() works,
        # e.g. the callback of a device library. put() takes the same items as
        # kanary.inputs(), and a value without a time gets the time it arrived.
        while not self.stopping.is_set():
            try:
                data, _sender = self.sock.recvfrom(4096)
                name, value = data.decode().split()
                self.inbox.put((name, float(value)))
            except socket.timeout:
                continue
            except ValueError:
                continue  # not "name number": ignore it

    def poll(self):
        # Waits up to 1 second for something to arrive, then returns every input with
        # its newest value; a burst is evaluated once. Nothing: skip() (rules do not
        # run). KANARY can stop or reload this source only between polls.
        return self.inbox.wait(timeout=1.0)
        # To see every value of a burst (a short spike), take them all instead:
        #   self.buffer.add(*self.inbox.take(timeout=1.0))   # see the "buffered" template

    def terminate(self):
        self.stopping.set()
        self.thread.join(timeout=2.0)
        self.sock.close()


# Nothing arriving is not an error for this source; a stale rule catches it:
#
#   @kanary.rule(rule_id="my.push.stale", inputs="my.push:*",
#                severity=kanary.WARN, evaluation_interval=30 * kanary.second)
#   class PushStale(kanary.StaleRule):
#       timeout = 5 * kanary.minute
```

[目次へ](#目次)

#### buffered

source · 最近の履歴を kanary.Buffer に貯め、window の平均や最小値を出す。

```console
$ kanary new -t buffered my.buffered
```

```python
import kanary


@kanary.source(source_id="my.buffered", interval=10 * kanary.second)
class MyBufferedSource(kanary.Source):
    description = "TODO: describe the raw value and the derived inputs."

    def init(self):
        # Per input: at most size samples, none older than max_age (counted back from
        # the newest sample); the first limit reached drops the oldest. Keep enough for
        # the longest window below.
        self.buffer = kanary.Buffer(size=1024, max_age=1 * kanary.hour)

    def poll(self):
        reading = 1.0  # TODO: read the raw value (a device, a file, an API)
        # Added the same way as for kanary.inputs(). When the reading carries its own
        # time, give it - ("pressure", reading, observed_at) - so the windows are right.
        self.buffer.add(("pressure", reading))

        # buffer["pressure"] is a kanary.Series: the samples of one input, oldest first.
        pressure = self.buffer["pressure"]
        # Right after start the window is only partly filled. This source waits until
        # 10 minutes are buffered; skip() means "no new snapshot, do not evaluate".
        # Decide as fits: by count (len(pressure) < 60), or publish partial results.
        if pressure.span < 10 * kanary.minute:
            return kanary.skip(reason="filling the buffer")
        return kanary.inputs(
            ("pressure", reading),
            ("pressure_avg_10m", pressure.window(10 * kanary.minute).mean()),
            # The lowest value of the last 5 minutes is over a limit only when every
            # sample was: a range rule on it fires after 5 minutes over (a fire_after).
            ("pressure_min_5m", pressure.window(5 * kanary.minute).min()),
        )

    # A Series also has first, last (.value, .timestamp), values, timestamps, len(),
    # span, tail(n), median(), max(), sum(), stddev(); a statistic is None without data.
    # The "trend", "spike", and "frozen" templates show the others.
    # Several inputs: self.buffer.select("temp_*") -> {name: Series}. print(self.buffer)
    # shows what is kept. kanaryctl test-poll runs poll() too; what it adds is dropped.
    # To keep what a helper class reads, subclass it: self.buffer.add(super().poll()).


# A rule on the result, as in the "range" template:
#
#   @kanary.rule(rule_id="my.buffered.high", inputs="my.buffered:pressure_min_5m",
#                severity=kanary.WARN, tags=["vacuum"])
#   class PressureHigh(kanary.RangeRule):
#       high = 2.0
```

[目次へ](#目次)

#### trend

source · 値の傾向と、上限に届くまでの時間を、最近の履歴から出す (kanary.Buffer)。

```console
$ kanary new -t trend my.trend
```

```python
import kanary


@kanary.source(source_id="my.trend", interval=30 * kanary.second)
class MyTrendSource(kanary.Source):
    description = "TODO: describe the value and the limit it must not reach."

    def init(self):
        # Per input: the samples of the last 30 minutes (see the "buffered" template).
        self.buffer = kanary.Buffer(max_age=30 * kanary.minute)

    def poll(self):
        reading = 50.0  # TODO: read the value, e.g. a tank level or a disk usage in %
        self.buffer.add(("level", reading))
        level = self.buffer["level"]
        if level.span < 10 * kanary.minute:
            return kanary.skip(reason="filling the buffer")
        last_10m = level.window(10 * kanary.minute)
        return kanary.inputs(
            ("level", reading),
            # Least-squares trend over every sample of the window, per second; times
            # kanary.minute gives % per minute. Steadier than rate(), which uses only
            # the first and the last sample.
            ("level_slope_per_min", last_10m.slope() * kanary.minute),
            # Seconds until the trend reaches 95; None when flat or moving away (and
            # once the value is past 95, so check the value itself as well).
            ("level_eta_seconds", last_10m.eta(95.0)),
        )


# Warn an hour before the limit is reached, and raise it 10 minutes before:
#
#   @kanary.rule(rule_id="my.trend.soon_full", inputs="my.trend:level_eta_seconds",
#                severity=kanary.WARN, tags=["tank"])
#   class SoonFull(kanary.ThresholdRule):
#       direction = "low"
#       thresholds = [(1 * kanary.hour, kanary.WARN), (10 * kanary.minute, kanary.ERROR)]
```

[目次へ](#目次)

#### spike

source · 最新の値が最近の履歴に比べてどれだけ外れているかを出し、spike や落ち込みを捉える (kanary.Buffer)。

```console
$ kanary new -t spike my.spike
```

```python
import kanary


@kanary.source(source_id="my.spike", interval=10 * kanary.second)
class MySpikeSource(kanary.Source):
    description = "TODO: describe the value and what a spike means."

    def init(self):
        # Per input: the samples of the last hour (see the "buffered" template).
        self.buffer = kanary.Buffer(max_age=1 * kanary.hour)

    def poll(self):
        reading = 12.0  # TODO: read the value, e.g. a current or a trigger rate
        self.buffer.add(("current", reading))
        current = self.buffer["current"]
        if len(current) < 30:  # a spread from a few samples means little
            return kanary.skip(reason="filling the buffer")
        return kanary.inputs(
            ("current", reading),
            # Standard deviations of the newest value from the mean of the hour: a spike
            # (or a dip) against the usual noise, without a fixed limit.
            ("current_zscore", current.zscore()),
            # The jump from the previous sample, in the value's own unit.
            ("current_jump", current.tail(2).delta()),
        )


# Fire on more than 5 standard deviations either way:
#
#   @kanary.rule(rule_id="my.spike.current", inputs="my.spike:current_zscore",
#                severity=kanary.WARN, tags=["hv"])
#   class CurrentSpike(kanary.RangeRule):
#       low = -5.0
#       high = 5.0
```

[目次へ](#目次)

#### frozen

source · 値が変わらない時間（固まった sensor）と変わる頻度（flapping）を、最近の履歴から出す (kanary.Buffer)。

```console
$ kanary new -t frozen my.frozen
```

```python
import kanary


@kanary.source(source_id="my.frozen", interval=10 * kanary.second)
class MyFrozenSource(kanary.Source):
    description = "TODO: describe the value and how often it normally changes."

    def init(self):
        # Per input: the samples of the last hour (see the "buffered" template).
        self.buffer = kanary.Buffer(max_age=1 * kanary.hour)

    def poll(self):
        flow = 3.2   # TODO: read a value that always moves a little, e.g. a flow
        valve = 1    # TODO: read a state that changes now and then, e.g. a valve (0/1)
        self.buffer.add(("flow", flow), ("valve", valve))
        return kanary.inputs(
            ("flow", flow),
            ("valve", valve),
            # Seconds the value has stayed within 0.001: a frozen sensor or a stuck
            # reader keeps sending new timestamps with the same value, which a stale
            # rule cannot see.
            ("flow_unchanged_seconds", self.buffer["flow"].unchanged_seconds(tolerance=0.001)),
            # How often the state changed in the last 10 minutes (toggling, flapping).
            # Only sampled values are compared: changes between two polls are not seen.
            ("valve_changes_10m", self.buffer["valve"].window(10 * kanary.minute).changes()),
        )


# Rules on the results, as in the "range" template:
#
#   @kanary.rule(rule_id="my.frozen.flow", inputs="my.frozen:flow_unchanged_seconds",
#                severity=kanary.WARN, tags=["cooling"])
#   class FlowFrozen(kanary.RangeRule):
#       high = 10 * kanary.minute
#
#   @kanary.rule(rule_id="my.frozen.valve", inputs="my.frozen:valve_changes_10m",
#                severity=kanary.WARN, tags=["cooling"])
#   class ValveFlapping(kanary.RangeRule):
#       high = 6
```

[目次へ](#目次)

#### remote

source · 別の Kanary node の alert を映す (RemoteKanarySource + import_remote_alarms)。

```console
$ kanary new -t remote my.remote
```

```python
import kanary


@kanary.source(source_id="my.remote", interval=30 * kanary.second)
class MyRemoteSource(kanary.RemoteKanarySource):
    description = "TODO: describe the remote Kanary node."

    base_url = "http://remote-kanary.example:8000"  # TODO
    timeout = 5.0
    # url = "http://remote-kanary.example:8000/export-alerts"  # full URL instead of base_url


# Create one local RemoteAlarm rule per remote alert. The ids are written out, so this
# file loads even when the remote node is down.
# kanary.import_remote_alarms(
#     source="my.remote",
#     remote_alarm_ids=["daq.link", "hv.crate1.range"],  # TODO: the remote rule ids
#     prefix="remote.",              # local rule_id = prefix + remote rule_id + suffix
#     add_tags=["remote"],
#     severity=kanary.ERROR,         # until the remote alert gives its own
#     propagate_ack=True,            # local ack/unack is forwarded to the remote node
#     propagate_silence=True,        # local silences are forwarded too
# )
```

[目次へ](#目次)

### Rule 系

#### rate

rule · 2 つの snapshot の間で値が速く変わりすぎたら発報する (RateRule)。

```console
$ kanary new -t rate my.rate
```

```python
import kanary


@kanary.rule(
    rule_id="my.rate",
    # The rate compares the current and previous snapshot of each input. When the
    # source returns the same sample again, the last rate is kept. Two-sample rates
    # are noisy; for a smoother trend use slope() of a kanary.Buffer series and a range rule.
    inputs="example.source:pressure",  # TODO
    severity=kanary.WARN,
    tags=["TODO"],
)
class MyRateRule(kanary.RateRule):
    description = "TODO: describe the allowed rate of change."
    # runbook = "TODO: what to check first, or a link"  # also in the notifications

    # Allowed rate range, in value units per second; works like the "range" template.
    # Write it with the time units: 1.0 / kanary.minute is "1 per minute".
    # low = -1.0 / kanary.minute also catches falling too fast.
    low = None
    high = 1.0 / kanary.minute
    lower_inclusive = True
    upper_inclusive = True
    hysteresis = 0.0

    # An input with no value (or no previous sample to compare with) is not
    # judged and leaves the rule OK. True: it fires too. For data that stops coming,
    # a StaleRule on the same input is the usual check (it also sees a dead source).
    missing_fires = False
```

[目次へ](#目次)

#### difference

rule · 冗長な input どうしの差が上限を超えたら発報する (DifferenceRule)。

```console
$ kanary new -t difference my.difference
```

```python
import kanary


@kanary.rule(
    rule_id="my.difference",
    # Two or more inputs that should read the same; globs and lists both work.
    inputs=["example.source:thermometer_a", "example.source:thermometer_b"],  # TODO
    severity=kanary.WARN,
    tags=["TODO"],
)
class MyDifferenceRule(kanary.DifferenceRule):
    description = "TODO: describe why these inputs should agree."
    # runbook = "TODO: which one to trust, and how to recalibrate."

    # Fires when highest - lowest > max_difference.
    max_difference = 0.5
    # Below this many inputs with values the rule stays OK (pair it with a StaleRule).
    min_inputs = 2
```

[目次へ](#目次)

#### k-of-n

rule · 多数の input のうち k 個以上が範囲外なら発報する。例: 「3 channel が死んでいる」 (KOfNRule)。

```console
$ kanary new -t k-of-n my.k-of-n
```

```python
import kanary


@kanary.rule(
    rule_id="my.k-of-n",
    inputs="example.source:ch*.current",  # TODO: one input per channel
    severity=kanary.ERROR,
    tags=["TODO"],
)
class MyKOfNRule(kanary.KOfNRule):
    description = "TODO: describe when this many bad channels matters."

    # Fire when k or more inputs break the range.
    k = 3
    # The range per input, exactly as in RangeRule (fire_when, *_inclusive also work).
    low = 0.5
    high = 10.0
    # Count inputs that are present but have no value as bad too.
    count_missing = False
    # The bad input names are listed in the alert message and payload["bad_inputs"].
```

[目次へ](#目次)

#### aggregate

rule · 多数の input の合計や平均をまとめて範囲で判定する。例: 全 channel の電流の合計 (AggregateRule)。

```console
$ kanary new -t aggregate my.aggregate
```

```python
import kanary


@kanary.rule(
    rule_id="my.aggregate",
    inputs="example.source:ch*.current",  # TODO
    severity=kanary.WARN,
    tags=["TODO"],
)
class MyAggregateRule(kanary.AggregateRule):
    description = "TODO: describe the total that must stay in range."

    # The inputs are combined at one moment (the current snapshot), across inputs;
    # for one input over time (an average of the last 10 minutes), use kanary.Buffer.

    # "sum", "mean", "median", "min", or "max" of the inputs that have numeric values.
    aggregate = "sum"
    # The range for the aggregate, exactly as in RangeRule.
    low = None
    high = 120.0
    hysteresis = 2.0
```

[目次へ](#目次)

#### count

rule · 値を返す input が期待より少ない（または多い）ときに発報する (CountRule)。

```console
$ kanary new -t count my.count
```

```python
import kanary


@kanary.rule(
    rule_id="my.count",
    inputs="example.source:ch*.current",  # TODO
    severity=kanary.ERROR,
    tags=["TODO"],
)
class MyCountRule(kanary.CountRule):
    description = "TODO: describe how many inputs should report."

    # Fire when the number of inputs with a value is below min_count or above max_count.
    # Either may be None.
    min_count = 64
    max_count = None
    # Missing names are listed in payload["missing_inputs"].
```

[目次へ](#目次)

#### multi-source

rule · 複数の source の input を 1 つの rule で組み合わせる（例: 2 つの sensor を比べる）。

```console
$ kanary new -t multi-source my.multi-source
```

```python
import kanary


@kanary.rule(
    rule_id="my.multi-source",
    # Any source update re-evaluates the rule with the latest snapshot of every source.
    inputs=[
        "example.primary:temperature",  # TODO
        "example.secondary:temperature",  # TODO
    ],
    severity=kanary.WARN,
    tags=["TODO"],
    # Re-check even when neither source updates (uses saved snapshots).
    # evaluation_interval=1 * kanary.minute,
)
class MyMultiSourceRule:
    description = "TODO: describe what disagreement between the sources means."
    # runbook = "TODO: what to check first, or a link"  # also in the notifications

    max_difference = 2.0
    max_age = 5 * kanary.minute

    def evaluate(self, ctx):
        # With several sources, always name the input; ctx.value() alone is ambiguous.
        primary = ctx.value("example.primary:temperature")
        secondary = ctx.value("example.secondary:temperature")

        # Declared inputs that matched nothing or have no value (or pass selectors explicitly).
        missing = ctx.missing()
        if missing:
            return kanary.warn(f"missing input: {', '.join(missing)}")

        # Every selected input, across sources: InputView(name, source_id, input_name, value, timestamp, metadata).
        # ctx.age(name) is the seconds since that input's timestamp.
        too_old = [item.name for item in ctx.inputs() if (ctx.age(item.name) or 0) > self.max_age]
        if too_old:
            return kanary.warn(f"inputs too old to compare: {', '.join(too_old)}")

        # Whole snapshot of one source (status, metadata, channels): ctx.source_payload("example.primary")
        difference = abs(primary - secondary)
        return kanary.warn_if(
            difference > self.max_difference,
            f"primary={primary} and secondary={secondary} differ by {difference:.2f}",
            extra={"difference": difference},
        ) or kanary.ok(f"sensors agree within {difference:.2f}")
```

[目次へ](#目次)

### Output 系

#### command-output

output · event ごとに script を実行する。例: 電話の自動発信や警告灯 (CommandOutput)。

```console
$ kanary new -t command-output my.command-output
```

```python
import kanary


@kanary.output(
    output_id="my.command-output",
    minimum_severity=kanary.ERROR,
)
class MyCommandOutput(kanary.CommandOutput):
    description = "TODO: describe what the script does."

    # A list runs as-is (no shell). The script receives:
    #   stdin: the event as JSON (see record())
    #   env:   KANARY_RULE_ID, KANARY_STATE, KANARY_PREVIOUS_STATE, KANARY_TRANSITION,
    #          KANARY_SEVERITY, KANARY_MESSAGE, KANARY_OWNER, KANARY_TAGS
    # A non-zero exit raises, so the event is retried (max_retry).
    command = ["/usr/local/bin/notify-oncall"]  # TODO
    timeout = 10.0  # this output's next event waits while the script runs; keep it short
    # env = {"NOTIFY_PROFILE": "detector"}

    # def args(self, event):
    #     # Build the command per event, e.g. pass the rule id as an argument.
    #     return [*self.command, event.rule_id]
```

[目次へ](#目次)

#### followups

output · 時間とともに通知を広げる: すぐ通知し、時間を置いて再通知し、ack で取り消す (OutputFollowups)。

```console
$ kanary new -t followups my.followups
```

```python
import kanary


@kanary.output(
    output_id="my.followups",
    minimum_severity=kanary.WARN,
    # Receive SILENCED / SUPPRESSED too, so pending followups can be cancelled.
    exclude_states=[],
)
class MyFollowupsOutput:
    description = "TODO: describe the escalation steps."

    def init(self):
        # In memory: pending followups are lost on reload and restart.
        self.followups = kanary.OutputFollowups()

    def terminate(self):
        self.followups.close()

    def emit(self, event):
        followups = self.followups.for_event(event)

        if event.current_state == kanary.FIRING and event.previous_state != kanary.FIRING:
            followups.now(self.notify_owner)  # runs right away, inside emit()
            # Delays count from the first registration of this alert episode. The callback
            # receives the latest event of the rule at the time it runs.
            followups.after(30 * kanary.minute, self.notify_group)
            followups.after(2 * kanary.hour, self.notify_mailing_list)
            return

        if event.current_state in {kanary.ACKED, kanary.SILENCED, kanary.SUPPRESSED, kanary.OK}:
            followups.cancel()  # cancel everything pending for this rule
            # followups.cancel(self.notify_group)  # or only one callback
            return

        if event.transition == kanary.ESCALATED:
            followups.cancel()
            followups.now(self.notify_mailing_list)

    # Callbacks run on this output's thread, between emit() calls (now() runs inside
    # emit()), so they never overlap with emit() and need no locks. Use timeouts: a slow
    # callback delays this output's next event. An exception from now() propagates to
    # emit() and triggers retries; one from a later callback is logged and shown on the
    # Outputs page.
    def notify_owner(self, event):
        print(f"owner: {event.rule_id} {event.message}")

    def notify_group(self, event):
        print(f"group: {event.rule_id} still {event.current_state.value}")

    def notify_mailing_list(self, event):
        print(f"mailing list: {event.rule_id} {kanary.severity_label(event.effective_severity)}")
```

[目次へ](#目次)

#### delayed

output · 一定時間たっても alert が続いているときだけ通知する。"for" / fire_after に相当 (OutputFollowups)。

```console
$ kanary new -t delayed my.delayed
```

```python
import kanary


@kanary.output(
    output_id="my.delayed",
    minimum_severity=kanary.WARN,
    # Receive SILENCED / SUPPRESSED too, so a pending notification can be cancelled.
    exclude_states=[],
)
class MyDelayedOutput:
    description = "TODO: describe who is notified and after how long."

    # The alert is visible in the UI and other outputs right away; only this output waits.
    # To decide that a short spike is not an alert at all, do it in the source instead
    # (see the "buffered" template: a range rule on the window minimum).
    delay = 5 * kanary.minute

    def init(self):
        self.followups = kanary.OutputFollowups()
        # Rules whose FIRING was sent, so their recovery is sent too. No lock is needed:
        # emit() and the followup callbacks all run on this output's thread, one at a time.
        self.notified = set()

    def terminate(self):
        self.followups.close()

    def emit(self, event):
        followups = self.followups.for_event(event)

        if event.current_state == kanary.FIRING and event.previous_state != kanary.FIRING:
            # Runs after delay (between emit() calls), with the latest event of the rule.
            followups.after(self.delay, self.send_firing)
            return

        if event.current_state in {kanary.ACKED, kanary.SILENCED, kanary.SUPPRESSED, kanary.OK}:
            followups.cancel()  # still pending: nothing was sent, so nothing to take back
            was_notified = event.rule_id in self.notified
            self.notified.discard(event.rule_id)
            if was_notified and event.current_state == kanary.OK:
                self.send(event)

    def send_firing(self, event):
        self.notified.add(event.rule_id)
        self.send(event)

    def send(self, event):
        # TODO: replace with mail, chat, or pager delivery. Use a timeout.
        print(f"{event.rule_id} {event.current_state.value}: {event.message}")
```

[目次へ](#目次)

#### quiet-hours

output · 急ぎでない alert を夜間は保留し、朝にまとめて送る (QuietHours)。

```console
$ kanary new -t quiet-hours my.quiet-hours
```

```python
import os

import kanary


@kanary.output(
    output_id="my.quiet-hours",
    minimum_severity=kanary.WARN,
)
class MyQuietHoursOutput(kanary.SlackOutput):
    description = "TODO: describe the channel and the quiet hours."

    timeout = 5.0

    url = os.environ.get("SLACK_WEBHOOK_URL")  # TODO: see the "slack" template

    def init(self):
        super().init()  # checks that url is set
        # Inside these windows only events at minimum_severity or above go out at once;
        # the rest are held until the quiet period ends (back-to-back windows count as
        # one period; windows may wrap past midnight). timezone is an IANA name
        # ("Asia/Tokyo", "Europe/Berlin"), an offset ("+09:00", "+9", "UTC+9"), or "UTC";
        # without it, the host's local time.
        self.quiet = kanary.QuietHours(
            [("22:00", "07:00"), ("12:00", "13:00")],  # TODO
            timezone="Asia/Tokyo",
            minimum_severity=kanary.CRITICAL,
            # What goes out when the quiet period ends:
            #   "all"      every held event, in order (one message each)
            #   "latest"   only the newest event of each rule
            #   "summary"  one message listing each rule's newest state and how many
            #              events it had; summary= sends it (emit_summary of the output)
            release="summary",
            summary=self.emit_summary,
            # defer=False drops the held events instead
        )
        # Held events are kept in memory: they are lost on reload, stop, and restart.

    def terminate(self):
        self.quiet.close()

    def emit(self, event):
        # super().emit posts to Slack: now (a failure propagates, so the engine retries as
        # usual), or later for a held event, on this output's thread either way; a failure
        # for a held event is logged and not retried. When an urgent event of a rule goes
        # out at night, that rule's held events are dropped as older ("all": sent first).
        self.quiet.send(event, super().emit)

    # Other calls:
    #   self.quiet.applies(event)  True when event would be held (or dropped) now
    #   self.quiet.held()          [(release_at, event), ...] still waiting
```

[目次へ](#目次)

#### oncall

output · 当番表の file から今の当番に mail し、夜間は mail を保留する (MailOutput + OnCallSchedule + QuietHours)。

```console
$ kanary new -t oncall my.oncall
```

```python
import kanary


@kanary.output(
    output_id="my.oncall",
    minimum_severity=kanary.WARN,
)
class MyOncallOutput(kanary.MailOutput):
    description = "TODO: describe the shift arrangement."

    # SMTP settings as in the "mail" template; recipients come from the schedule.
    smtp_host = "smtp.example.org"  # TODO
    sender = "kanary@example.org"   # TODO
    subject_prefix = "[KANARY]"

    def init(self):
        super().init()
        # oncall.toml next to this file, for example:
        #
        #   timezone = "Asia/Tokyo"                  # or "+09:00"; default: host time
        #   default = ["control-room@example.org"]   # when no shift matches
        #
        #   [[shift]]                                # weekly
        #   weekdays = ["mon", "tue", "wed", "thu", "fri"]
        #   from = "09:00"
        #   to = "18:00"
        #   recipients = ["day-shift@example.org"]
        #
        #   [[shift]]                                # overnight; wraps past midnight
        #   weekdays = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
        #   from = "18:00"
        #   to = "09:00"
        #   recipients = ["night-shift@example.org"]
        #
        #   [[shift]]                                # dated; wins over weekly shifts
        #   start = 2026-12-28T09:00:00
        #   end = 2027-01-04T09:00:00
        #   recipients = ["holiday@example.org"]
        #
        # The file is read in init(), so reload the plugin after editing it.
        config = kanary.load_toml(filename="oncall.toml")
        self.schedule = kanary.OnCallSchedule(
            config.get("shift", []),
            timezone=config.get("timezone"),
            default=config.get("default", []),
        )
        # At night only CRITICAL is mailed at once; the rest is held and mailed at 07:00,
        # in order (see the "quiet-hours" template for the details).
        self.quiet = kanary.QuietHours(
            [("23:00", "07:00")],
            timezone=config.get("timezone"),
            minimum_severity=kanary.CRITICAL,
        )

    def terminate(self):
        self.quiet.close()  # mail still held is dropped (and logged)
        super().terminate()

    def emit(self, event):
        self.quiet.send(event, super().emit)

    def recipients_for(self, event):
        # Held mail goes to whoever is on shift when the event happened.
        return self.schedule.recipients(event.occurred_at)
```

[目次へ](#目次)
