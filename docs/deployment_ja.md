# Deployment

## 標準のインストール方法

通常のインストール方法は PyPI です。

```bash
pip install kanary
```

`uv` を使う場合:

```bash
uv tool install kanary
```

インストール後は次のコマンドが使える想定です。

- `kanary`
- `kanaryctl`

## Docker

Kanary は Docker image としても実行できます。この image には `kanary` と
`kanaryctl` の両方が入ります。

image は Kanary 本体の runtime を提供するためのものです。site-specific な
plugin 定義は host 側から `/etc/kanary/plugins` に mount します。SQLite を
使う場合は `/var/lib/kanary` も書き込み可能な directory として mount します。

repository root で image を build します。

```bash
docker build -t kanary:local .
```

GHCR に公開済みの build 済み image を使いたい場合は、次のように pull できます。

```bash
docker pull ghcr.io/mzks/kanary:latest
```

local の plugin directory と state directory を使って起動する例です。

```bash
mkdir -p plugins state
docker run --rm \
  -p 8000:8000 \
  -v "$PWD/plugins:/etc/kanary/plugins" \
  -v "$PWD/state:/var/lib/kanary" \
  kanary:local
```

公開済み image を使う場合は、`kanary:local` の代わりに
`ghcr.io/mzks/kanary:latest` や `ghcr.io/mzks/kanary:0.2.3` のような
version tag を指定します。

container の既定 command は次です。

```bash
kanary /etc/kanary/plugins --state-db /var/lib/kanary/kanary.db --api-port 8000
```

Docker Compose を使う場合は repository に [`compose.yaml`](../compose.yaml) が
あります。

```bash
mkdir -p plugins state
docker compose up
```

`kanaryctl` は host 側から公開された API に対して使うのが自然です。

```bash
kanaryctl --base-url http://127.0.0.1:8000 alerts
kanaryctl --base-url http://127.0.0.1:8000 reload
```

mount した plugin directory に対して container 内で lint を実行することも
できます。

```bash
docker run --rm \
  -v "$PWD/plugins:/etc/kanary/plugins" \
  kanary:local \
  kanary lint /etc/kanary/plugins
```

## 開発用のインストール方法

source checkout からの実行もできますが、こちらは開発用の扱いです。

```bash
git clone https://github.com/mzks/kanary
cd kanary
uv sync
uv run python -m kanary ./demo
```

Kanary は Python `3.13` 以上を前提とします。

## 推奨構成

```text
/etc/kanary/
  plugins/
  kanary.env

/var/lib/kanary/
  kanary.db
```

- `/etc/kanary/plugins/`
  site-specific な `Source`, `Rule`, `Output`
- `/etc/kanary/kanary.env`
  DSN や webhook URL などの環境変数
- `/var/lib/kanary/kanary.db`
  SQLite の履歴と runtime state

## systemd 例

```ini
[Unit]
Description=Kanary monitoring engine
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/kanary
EnvironmentFile=/etc/kanary/kanary.env
ExecStart=/usr/local/bin/kanary /etc/kanary/plugins --state-db /var/lib/kanary/kanary.db --api-port 8000
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

## runtime option

主な option:

- `--api-host`
  API と Web viewer の bind host を指定します。既定は `0.0.0.0` です。
- `--api-port`
  API と Web viewer の port を指定します。文書中の例は `8000` を使っています。
- `--state-db`
  SQLite に history を保存する path を指定します。
- `--log-level`
  runtime logging level を指定します。
- `--disable-default-viewer`
  組み込み Web viewer だけを無効化します。HTTP API は引き続き有効です。

主な環境変数:

- `KANARY_SQLITE_PATH`
  `ExecStart` に書かずに SQLite path を指定したいときに使います。
- `KANARY_API_URL`
  `kanaryctl` が使う既定の API base URL です。
- `KANARY_API_HOST`
  API と Web viewer の bind host です。`--api-host` と同じ意味です。
- `KANARY_NODE_ID`
  peer export/import に使う node identifier です。未指定時は hostname を使います。

PostgreSQL の DSN や Discord webhook URL のような監視対象ごとの接続情報は、deploy する plugin 定義側で管理します。
