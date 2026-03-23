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

## Docker

Kanary can also run as a container image. The image includes both `kanary` and
`kanaryctl`.

The image is intended to provide the runtime itself. Site-specific plugins are
mounted from the host into `/etc/kanary/plugins`. If you use SQLite persistence,
mount a writable directory for `/var/lib/kanary` as well.

Build the image from the repository root:

```bash
docker build -t kanary:local .
```

If you prefer a prebuilt image after it has been published to GHCR:

```bash
docker pull ghcr.io/mzks/kanary:latest
```

Run Kanary with a local plugin directory and a local state directory:

```bash
mkdir -p plugins state
docker run --rm \
  -p 8000:8000 \
  -v "$PWD/plugins:/etc/kanary/plugins" \
  -v "$PWD/state:/var/lib/kanary" \
  kanary:local
```

When using the published image, replace `kanary:local` with
`ghcr.io/mzks/kanary:latest` or a version tag such as
`ghcr.io/mzks/kanary:0.2.0`.

The default container command is:

```bash
kanary /etc/kanary/plugins --state-db /var/lib/kanary/kanary.db --api-port 8000
```

If you prefer Docker Compose, the repository includes [`compose.yaml`](../compose.yaml):

```bash
mkdir -p plugins state
docker compose up
```

Use `kanaryctl` from the host against the published API:

```bash
kanaryctl --base-url http://127.0.0.1:8000 alerts
kanaryctl --base-url http://127.0.0.1:8000 reload
```

You can also run lint inside the container against the mounted plugin
directory:

```bash
docker run --rm \
  -v "$PWD/plugins:/etc/kanary/plugins" \
  kanary:local \
  kanary lint /etc/kanary/plugins
```

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
  plugins/
  kanary.env

/var/lib/kanary/
  kanary.db
```

- `/etc/kanary/plugins/`
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
ExecStart=/usr/local/bin/kanary /etc/kanary/plugins --state-db /var/lib/kanary/kanary.db --api-port 8000
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
- `--disable-default-viewer`
  Disables only the built-in Web viewer. The HTTP API stays enabled.

Common environment variables:

- `KANARY_SQLITE_PATH`
  Alternative way to set the SQLite path without putting it in `ExecStart`.
- `KANARY_API_URL`
  Default API base URL used by `kanaryctl`.
- `KANARY_API_HOST`
  Bind host for the local API and Web viewer. This is equivalent to `--api-host`.
- `KANARY_NODE_ID`
  Optional node identifier used by peer export and import. If unset, Kanary uses the hostname.

Source-specific connection settings such as PostgreSQL DSNs or Discord webhooks are defined by the deployed monitoring definitions.
