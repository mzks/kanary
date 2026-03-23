# Deployment

## 現状

Kanary は installable CLI entry point を持っていますが、まだ PyPI には公開していません。

- `pip install kanary` はまだ使えません
- checkout からの local install は使えます
- 当面の推奨は `git clone + uv sync` です

## 推奨構成

```text
/etc/kanary/
  rules/
  kanary.env

/var/lib/kanary/
  kanary.db
```

## systemd 例

```ini
[Unit]
Description=Kanary monitoring engine
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/kanary
EnvironmentFile=/etc/kanary/kanary.env
ExecStart=/opt/kanary/.venv/bin/kanary run /etc/kanary/rules --state-db /var/lib/kanary/kanary.db
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```
