# Deployment

## Installation

KANARY is intended to be installed as a normal Python package.

```bash
pip install kanary
```

After installation, the following commands are available:

- `kanary`
- `kanaryctl`

You can inspect the installed CLI options with:

```bash
kanary --help
kanaryctl help
```

## Recommended layout

It is usually better to separate the installed package from site-specific monitoring definitions.

Example layout:

```text
/etc/kanary/
  rules/
  kanary.env

/var/lib/kanary/
  kanary.db
```

- `/etc/kanary/rules/`
  - site-specific `Source`, `Rule`, and `Output` definitions
- `/etc/kanary/kanary.env`
  - environment variables such as DSNs or webhook URLs
- `/var/lib/kanary/kanary.db`
  - SQLite history and runtime state

## systemd example

```ini
[Unit]
Description=KANARY monitoring engine
After=network.target

[Service]
Type=simple
WorkingDirectory=/etc/kanary
EnvironmentFile=/etc/kanary/kanary.env
ExecStart=/usr/bin/kanary run /etc/kanary/rules --state-db /var/lib/kanary/kanary.db
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

Adjust the command path if your Python environment installs `kanary` somewhere else.

## Runtime options

Common runtime options:

- `--api-port`
  - local API and Web viewer port
- `--state-db`
  - SQLite path for persisted history
- `--log-level`
  - runtime logging level

Common environment variables:

- `KANARY_SQLITE_PATH`
  - alternative way to specify the SQLite path
- `KANARY_API_URL`
  - used by `kanaryctl`

Source-specific connection settings such as PostgreSQL DSNs or Discord webhooks are defined by the deployed monitoring definitions.

## Upgrade strategy

A simple deployment model is:

1. Upgrade the installed `kanary` package.
2. Keep monitoring definitions in `/etc/kanary/rules/` under separate version control.
3. Restart the `kanary` service after package upgrades.

This keeps engine upgrades and site-specific rule changes independent.
