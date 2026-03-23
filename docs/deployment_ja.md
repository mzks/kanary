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
  rules/
  kanary.env

/var/lib/kanary/
  kanary.db
```

- `/etc/kanary/rules/`
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
ExecStart=/usr/local/bin/kanary /etc/kanary/rules --state-db /var/lib/kanary/kanary.db --api-port 8000
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

## runtime option

主な option:

- `--api-host`
- `--api-port`
- `--state-db`
- `--log-level`

主な環境変数:

- `KANARY_SQLITE_PATH`
- `KANARY_API_URL`
- `KANARY_API_HOST`
- `KANARY_NODE_ID`
