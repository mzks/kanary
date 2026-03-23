# Deployment

## Current Status

Kanary already has package metadata and installable CLI entry points, but it is not published on PyPI yet.

This means:

- `pip install kanary` does not work yet
- local installation from a checkout does work
- the recommended deployment method for now is `git clone + uv sync`

Kanary currently requires Python `3.13` or newer.

## Recommended Temporary Installation

Clone the repository on the target machine and create a local environment:

```bash
git clone <your-kanary-repo-url>
cd kanary
uv sync
```

After `uv sync`, the project virtual environment contains:

- `.venv/bin/kanary`
- `.venv/bin/kanaryctl`

Check the available CLI options with:

```bash
.venv/bin/kanary --help
.venv/bin/kanaryctl help
```

You can also run them through `uv run`:

```bash
uv run kanary --help
uv run kanaryctl help
```

## Local pip Installation

If the target machine already has Python `3.13+`, you can install from a checkout:

```bash
python3.13 -m pip install --no-build-isolation .
```

Editable install:

```bash
python3.13 -m pip install --no-build-isolation -e .
```

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
ExecStart=/opt/kanary/.venv/bin/kanary run /etc/kanary/rules --state-db /var/lib/kanary/kanary.db
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

Adjust paths if your installation layout is different.

## Runtime Options

Common runtime options:

- `--api-host`
  Bind host for the local API and Web viewer. Default: `0.0.0.0`
- `--api-port`
  API and Web viewer port.
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

1. Update the checked-out `kanary` repository.
2. Run `uv sync` again.
3. Keep monitoring definitions in `/etc/kanary/rules/` under separate version control.
4. Restart the `kanary` service after package upgrades.

This keeps engine upgrades and site-specific rule changes independent.
