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

## When A Plugin Needs Additional Python Modules

Kanary does not install plugin-specific dependencies automatically.
If a site-specific plugin requires a new Python module, the admin who operates
Kanary installs that module into the Python environment that runs Kanary.

Basic policy:

- install into the Python environment that runs Kanary itself
- plugin authors request dependencies from the admin
- restart the Kanary process after package additions or upgrades
- this is not hot-reload

If you install Kanary with `uv tool`, you can add extra dependencies at install
time:

```bash
uv tool install --with 'psycopg[binary]' --with 'psutil' kanary
```

For multiple dependencies:

```bash
uv tool install \
  --with 'psycopg[binary]' \
  --with 'psutil' \
  --with 'httpx' \
  kanary
```

If the plugin directory contains a `requirements.txt`, you can use:

```bash
uv tool install \
  --with-requirements /etc/kanary/plugins/requirements.txt \
  kanary
```

If `kanary` is already installed via `uv tool install kanary`, reinstalling with
`--force` is the simplest way to add dependencies later:

```bash
uv tool install --force \
  --with-requirements /etc/kanary/plugins/requirements.txt \
  kanary
```

When using `uv tool`, make sure the user that owns the tool environment matches
the user that actually runs Kanary under `systemd`.

For bare metal / `systemd`, a dedicated virtual environment is often easier to
manage:

```bash
/opt/kanary/.venv/bin/python -m pip install 'psycopg[binary]' psutil
systemctl restart kanary
```

It is also fine to place a `requirements.txt` file in the plugin directory and
have the admin review and install it:

```bash
/opt/kanary/.venv/bin/python -m pip install -r /etc/kanary/plugins/requirements.txt
systemctl restart kanary
```

If a required module is missing, the plugin will fail with an import/load error.
It is safer to run `kanary lint ...` after installation and then restart the
service.

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
`ghcr.io/mzks/kanary:0.3.1`.

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

If a plugin needs additional modules in container deployments, build a
site-specific derived image from the Kanary base image. Do not rely on manually
running `pip install` inside an already-running container.

```dockerfile
FROM ghcr.io/mzks/kanary:0.3.1

RUN python -m pip install 'psycopg[binary]' psutil
```

Then mount the plugin directory as usual. Treat module additions as an image
change and recreate the container. This is also not hot-reload.

For example, in a deployment directory separate from the repository root:

```bash
mkdir -p docker-deploy/plugins docker-deploy/state
cd docker-deploy
```

`Dockerfile`:

```dockerfile
FROM ghcr.io/mzks/kanary:0.3.1

RUN python -m pip install 'psycopg[binary]' psutil
```

Build:

```bash
docker build -t kanary-site:local .
```

Run:

```bash
docker run --rm \
  -p 8000:8000 \
  -v "$PWD/plugins:/etc/kanary/plugins" \
  -v "$PWD/state:/var/lib/kanary" \
  kanary-site:local
```

If plugin dependencies are collected in `requirements.txt`, you can also write:

```dockerfile
FROM ghcr.io/mzks/kanary:0.3.1

COPY requirements.txt /tmp/requirements.txt
RUN python -m pip install -r /tmp/requirements.txt
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

In `systemd` deployments, it is important to pin the exact Python environment
that provides `kanary`. Install any extra modules into that same environment.
Using a dedicated Kanary virtual environment is usually easier to manage than
installing directly into the system Python.

Example:

```ini
ExecStart=/opt/kanary/.venv/bin/kanary /etc/kanary/plugins --state-db /var/lib/kanary/kanary.db --api-port 8000
```

In that setup, if a plugin requires a new module, install it into
`/opt/kanary/.venv` and then run `systemctl restart kanary`.

## Ubuntu + uv + systemd Example

This example assumes `uv` is already installed on Ubuntu and shows a complete
path from creating a service user to starting Kanary with `systemd`.

1. Create the service user and directories

```bash
sudo useradd --system --home /opt/kanary --shell /usr/sbin/nologin kanary
sudo mkdir -p /opt/kanary /etc/kanary/plugins /var/lib/kanary
sudo chown -R kanary:kanary /opt/kanary /etc/kanary/plugins /var/lib/kanary
```

2. Install Kanary with `uv tool`  
`uv tool` environments are user-local, so install as the service user.

```bash
sudo -u kanary -H uv tool install kanary
```

If a plugin already needs extra dependencies, add them now:

```bash
sudo -u kanary -H uv tool install --force \
  --with 'psycopg[binary]' \
  --with 'psutil' \
  kanary
```

3. Check where the `kanary` executable was installed

```bash
sudo -u kanary -H sh -lc 'command -v kanary'
```

In many cases it will be something like `~/.local/bin/kanary`. Use the absolute
path you confirm here in `ExecStart`.

4. Create the environment file  
If needed, create `/etc/kanary/kanary.env`.

```bash
sudo tee /etc/kanary/kanary.env >/dev/null <<'EOF'
KANARY_API_URL=http://127.0.0.1:8000
EOF
```

5. Create the service file

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

This example assumes the `uv tool` install ended up at
`/opt/kanary/.local/bin/kanary`. Replace `ExecStart` with the actual path you
confirmed in step 3.  
If you set `User=kanary`, make sure that user can read `/etc/kanary/plugins`
and read/write `/var/lib/kanary`.

6. Reload `systemd`, enable, and start the service

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now kanary
```

7. Check status

```bash
sudo systemctl status kanary
kanaryctl --base-url http://127.0.0.1:8000 health
```

8. When a plugin later needs a new module

```bash
sudo -u kanary -H uv tool install --force \
  --with-requirements /etc/kanary/plugins/requirements.txt \
  kanary
sudo systemctl restart kanary
```

This is a deployment operation, not a runtime reload. Restart the process after
adding or updating Python packages.

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
