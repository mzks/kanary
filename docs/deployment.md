# Deployment

## Standard Installation

The normal installation method is PyPI:

```bash
pip install kanary
```

If you prefer `uv`:

```bash
uv tool install kanary
```

After installation, these commands should be available:

- `kanary`
- `kanaryctl`

## Development Installation

Installing from a source checkout is still supported, but it should be treated as a development workflow:

```bash
git clone https://github.com/mzks/kanary
cd kanary
uv sync
uv run python -m kanary ./demo
```

Kanary currently requires Python `3.13` or newer.

## Recommended Layout

It is usually cleaner to separate the installed package from site-specific monitoring definitions.

```text
/etc/kanary/
  rules/
  kanary.env

/var/lib/kanary/
  kanary.db
```

- `/etc/kanary/rules/`
  Site-specific `Source`, `Rule`, and `Output` definitions.
- `/etc/kanary/kanary.env`
  Environment variables such as DSNs or webhook URLs.
- `/var/lib/kanary/kanary.db`
  SQLite history and runtime state.

## systemd Example

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

Adjust `ExecStart` if your installation path is different.

## Runtime Options

Common runtime options:

- `--api-host`
  Bind host for the local API and Web viewer. Default: `0.0.0.0`
- `--api-port`
  API and Web viewer port. Examples in the documentation use `8000`.
- `--state-db`
  SQLite path for persisted history.
- `--log-level`
  Runtime logging level.

Common environment variables:

- `KANARY_SQLITE_PATH`
- `KANARY_API_URL`
- `KANARY_API_HOST`
- `KANARY_NODE_ID`

Source-specific connection settings such as PostgreSQL DSNs or Discord webhooks are defined by the deployed monitoring definitions.

## Upgrade Strategy

One simple deployment model is:

1. Upgrade the installed package.
2. Keep monitoring definitions in `/etc/kanary/rules/` under separate version control.
3. Restart the `kanary` service after package upgrades.

If you use the source checkout workflow instead, the equivalent is:

1. Update the checked-out repository.
2. Run `uv sync` again.
3. Restart the `kanary` service.

This keeps engine upgrades and site-specific rule changes independent.
