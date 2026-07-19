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

## plugin が追加 module を要求する場合

Kanary 自体は plugin ごとの依存を自動 install しません。  
site-specific な plugin が新しい Python module を必要とする場合は、Kanary を起動する
admin が Kanary の実行環境にその module を追加で install します。

基本方針:

- install 先は Kanary 本体を実行している Python 環境です
- plugin 作者は必要な dependency を admin に依頼します
- package 追加や upgrade の後は Kanary process を restart します
- これは hot-reload の対象ではありません

`uv tool` で入れる場合は、追加 dependency も install 時に一緒に指定できます。

```bash
uv tool install --with 'psycopg[binary]' --with 'psutil' kanary
```

複数の dependency がある場合:

```bash
uv tool install \
  --with 'psycopg[binary]' \
  --with 'psutil' \
  --with 'httpx' \
  kanary
```

plugin directory に `requirements.txt` を置くなら、次の形も使えます。

```bash
uv tool install \
  --with-requirements /etc/kanary/plugins/requirements.txt \
  kanary
```

すでに `uv tool install kanary` 済みで、あとから dependency を足したい場合は、`--force`
付きで再 install するのがわかりやすいです。

```bash
uv tool install --force \
  --with-requirements /etc/kanary/plugins/requirements.txt \
  kanary
```

`uv tool` を使う場合は、「どの user の tool 環境に入れたか」と「`systemd` がどの user で
起動するか」を揃えてください。

たとえば bare metal / `systemd` では、Kanary 専用の venv を使うのが扱いやすいです。

```bash
/opt/kanary/.venv/bin/python -m pip install 'psycopg[binary]' psutil
systemctl restart kanary
```

plugin directory 側に `requirements.txt` のような file を置いて、admin が review して
install する運用にしても構いません。

```bash
/opt/kanary/.venv/bin/python -m pip install -r /etc/kanary/plugins/requirements.txt
systemctl restart kanary
```

module が入っていない状態で plugin を load すると import/load error になります。  
追加 install の後に `kanary lint ...` で確認してから restart するのが安全です。

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
`ghcr.io/mzks/kanary:latest` を指定できます。production deployment では、
再現性のため `ghcr.io/mzks/kanary:0.6.0` のような release tag で pin するのが
無難です。

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

追加 module が必要な plugin を container で使う場合は、base image に対して
site-specific な派生 image を作るのが自然です。container 起動後に手で `pip install`
するのではなく、Dockerfile に明示するほうがよりよいです。

```dockerfile
FROM ghcr.io/mzks/kanary:0.6.0

RUN python -m pip install 'psycopg[binary]' psutil
```

その上で通常どおり plugin directory を mount します。module の追加は image の更新として
扱い、container を作り直します。これも hot-reload の対象ではありません。

たとえば repository root とは別の deployment directory で、次のように build と起動を
行えます。

```bash
mkdir -p docker-deploy/plugins docker-deploy/state
cd docker-deploy
```

`Dockerfile`:

```dockerfile
FROM ghcr.io/mzks/kanary:0.6.0

RUN python -m pip install 'psycopg[binary]' psutil
```

build:

```bash
docker build -t kanary-site:local .
```

run:

```bash
docker run --rm \
  -p 8000:8000 \
  -v "$PWD/plugins:/etc/kanary/plugins" \
  -v "$PWD/state:/var/lib/kanary" \
  kanary-site:local
```

plugin 側 dependency をまとめた `requirements.txt` があるなら、次のようにも書けます。

```dockerfile
FROM ghcr.io/mzks/kanary:0.6.0

COPY requirements.txt /tmp/requirements.txt
RUN python -m pip install -r /tmp/requirements.txt
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

`systemd` 運用では、`ExecStart` が指している `kanary` がどの Python 環境に属しているかを
固定しておくのが重要です。追加 module はその実行環境に install します。  
system Python に直接入れるより、Kanary 専用 venv を作ってその中の `kanary` を
`ExecStart` に書く方が管理しやすくなります。

例:

```ini
ExecStart=/opt/kanary/.venv/bin/kanary /etc/kanary/plugins --state-db /var/lib/kanary/kanary.db --api-port 8000
```

この場合、plugin が新しい module を要求したら `/opt/kanary/.venv` に install してから
`systemctl restart kanary` を実行します。

## Ubuntu + uv + systemd の例

ここでは、Ubuntu に `uv` が入っている前提で、Kanary 専用 user、plugin directory、
state directory、`systemd` service まで作る例を示します。

1. service 用 user と directory を作る

```bash
sudo useradd --system --home /opt/kanary --shell /usr/sbin/nologin kanary
sudo mkdir -p /opt/kanary /etc/kanary/plugins /var/lib/kanary
sudo chown -R kanary:kanary /opt/kanary /etc/kanary/plugins /var/lib/kanary
```

2. Kanary を `uv tool` で install する  
`uv tool` は実行 user ごとの環境なので、service user で install します。

```bash
sudo -u kanary -H uv tool install kanary
```

plugin が追加 dependency を要求するなら、この時点で一緒に入れます。

```bash
sudo -u kanary -H uv tool install --force \
  --with 'psycopg[binary]' \
  --with 'psutil' \
  kanary
```

3. `kanary` binary の場所を確認する

```bash
sudo -u kanary -H sh -lc 'command -v kanary'
```

多くの場合は `~/.local/bin/kanary` に入ります。service file の `ExecStart` には、
ここで確認した absolute path を書きます。

4. 環境変数 file を置く  
必要なら `/etc/kanary/kanary.env` を作ります 

```bash
sudo tee /etc/kanary/kanary.env >/dev/null <<'EOF'
KANARY_API_URL=http://127.0.0.1:8000
EOF
```

5. service file を作る

```bash
sudo tee /etc/systemd/system/kanary.service >/dev/null <<'EOF'
[Unit]
Description=Kanary monitoring engine
After=network.target

[Service]
Type=simple
User=kanary
Group=kanary
WorkingDirectory=/opt/kanary
EnvironmentFile=/etc/kanary/kanary.env
ExecStart=/opt/kanary/.local/bin/kanary /etc/kanary/plugins --state-db /var/lib/kanary/kanary.db --api-port 8000
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
```

ここでは `uv tool` の install 先が `/opt/kanary/.local/bin/kanary` になる想定です。  
`ExecStart` の path は、3 で確認した actual path に合わせてください。  
`User=kanary` を付けるなら、その user が `/etc/kanary/plugins` と `/var/lib/kanary` を
読める/書けるように owner や permission を揃えてください。

6. 有効化して起動する

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now kanary
```

7. 状態を確認する

```bash
sudo systemctl status kanary
kanaryctl --base-url http://127.0.0.1:8000 health
```

8. plugin が新しい module を要求したとき

```bash
sudo -u kanary -H uv tool install --force \
  --with-requirements /etc/kanary/plugins/requirements.txt \
  kanary
sudo systemctl restart kanary
```

これは runtime reload ではなく deployment operation です。Python package の追加・更新後は
process restart を行ってください。

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
