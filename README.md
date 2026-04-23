# Liferay Environment Broker

Internal Environment-as-a-Service broker for ephemeral Liferay development environments on a local Docker host. The broker receives environment requests, applies platform guardrails, starts Liferay containers, exposes them through unique ports, tracks lifecycle state, and automatically cleans up unused environments.

The intended deployment is an internal network or VPN. The broker manages Docker containers on one host; it does not orchestrate Proxmox, Kubernetes, DNS, or TLS.

## Components

- `broker.py`: FastAPI broker service and lightweight per-environment HTTP proxy.
- `client.py`: developer CLI.
- `config.yaml`: sanitized default configuration with placeholders.
- `install.sh`: installs the broker under `/opt/liferay-env-broker`.
- `liferay-broker.service`: systemd unit.
- `MODELS.md`: domain and API model reference.

## How It Works

1. A developer requests a Liferay environment with an allowed image and resource profile.
2. The broker authenticates the Bearer token and maps it to a user.
3. The requested image is checked against `allowed_image_patterns`.
4. The broker checks per-user quota, available RAM, and port availability.
5. Docker pulls the image if it is not already present locally.
6. The broker starts the container with memory and CPU limits.
7. A small HTTP proxy listens on the assigned host port and forwards traffic to `container:8080`.
8. The broker waits for Liferay to answer on `/c/portal/login`.
9. The environment moves to `ready` or `failed`.
10. The cleanup loop stops environments after max TTL, and only applies inactivity cleanup to environments created with the TTL field left blank.

## Lifecycle States

- `starting`: the request was accepted and the container is starting.
- `ready`: Liferay answered the readiness check.
- `failed`: startup, readiness, or container execution failed.
- `stopped`: stopped after inactivity.
- `expired`: stopped after max TTL.
- `deleted`: manually deleted.

## Guardrails

- Bearer tokens configured in `api_tokens`.
- Users can only operate their own environments, except `admin_user`.
- Image allowlist through regex patterns:

```yaml
allowed_image_patterns:
  - ^liferay/dxp:.*$
  - ^liferay/portal:.*$
```

- Resource profiles with memory and CPU limits.
- Configurable max active environments per user.
- Configurable host port range.
- Configurable max TTL and inactivity timeout.
- No direct Docker access for clients.

## Install On The Linux Docker Host

Copy and unpack the broker package on the Linux host that runs Docker:

```bash
scp liferay_env_broker_v3.zip USER@BROKER_HOST:/tmp/
ssh USER@BROKER_HOST

cd /tmp
unzip liferay_env_broker_v3.zip
cd liferay_env_broker_v3
```

Install OS dependencies:

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip docker.io unzip
sudo systemctl enable --now docker
```

Install the broker:

```bash
chmod +x install.sh
./install.sh
```

The service files are installed under:

```text
/opt/liferay-env-broker
```

## Configure

Edit the installed configuration:

```bash
sudo nano /opt/liferay-env-broker/config.yaml
```

Change at least:

- `api_tokens`: replace placeholder values with real per-user tokens.
- `base_url_template`: set the URL users should receive, for example `http://BROKER_HOST:{port}` using a hostname, DNS name, or internal address reachable from developer machines.

You do not need to commit any real host address. The only people who need the actual broker URL are the users running the CLI or opening the UI.

Useful settings:

```yaml
profiles:
  small:
    memory_mb: 4096
    cpus: 1.5
    capacity_units: 1
  standard:
    memory_mb: 6144
    cpus: 2
    capacity_units: 1
  large:
    memory_mb: 8192
    cpus: 3
    capacity_units: 2
port_range:
  start: 18080
  end: 18150
default_ttl_hours: 8
max_ttl_hours: 120
idle_timeout_minutes: 60
capacity:
  total_units: 12
  per_user_units:
    default: 1
    developer: 3
    product: 2
    design: 2
    admin: 12
image_cleanup:
  enabled: true
  interval_hours: 24
  max_unused_age_hours: 168
```

Capacity units are a simple scheduling guardrail. In this default model, `small` and `standard` each cost 1 unit, `large` costs 2 units, and the shared machine has 12 units. That means the platform can run up to 12 standard environments, or a bounded mix of standard and large environments. `per_user_units` limits how much capacity each user can consume independently; regular users get 1 unit by default, while admin can consume the full shared pool.

Leave `ttl_hours` empty for the default ephemeral behavior: the environment uses `default_ttl_hours` and can also be stopped after `idle_timeout_minutes` without access. Set `ttl_hours` to a value from `1` to `max_ttl_hours` for a fixed maximum lifetime without inactivity cleanup. Set `ttl_hours` to `0` to keep the environment until it is deleted manually.

Image cleanup prunes Docker images that are not used by any container and are older than the configured age. It does not remove images used by active or stopped containers.

Restart the service:

```bash
sudo systemctl daemon-reload
sudo systemctl restart liferay-broker.service
sudo systemctl status liferay-broker.service
```

Follow logs:

```bash
sudo journalctl -u liferay-broker.service -f
```

Open firewall ports if needed:

```bash
sudo ufw allow 8899/tcp
sudo ufw allow 18080:18150/tcp
```

## Verify

API healthcheck:

```bash
curl http://BROKER_HOST:8899/health
```

Web UI:

```text
http://BROKER_HOST:8899/ui
```

The UI asks for a Bearer token and lets users list, create, touch, and delete environments. The healthcheck is intentionally plain JSON, usually:

```json
{"status":"ok"}
```

## Web UI Workflow

The web UI is the recommended workflow for product, design, QA, and other non-CLI users. It avoids local Python setup and keeps the common environment lifecycle actions in the browser.

Dashboard behavior:

- Dark mode is enabled by default and can be toggled from the icon in the top-right corner.
- The dashboard derives the broker URL from the current `/ui` page, so users only enter a `Bearer token`.
- After connecting, the token card is hidden and a compact session card shows the connected user and broker endpoint.
- `Bearer token`, user, theme, and history preference are saved in browser `localStorage`.
- After connecting, the `User` field is filled from the Bearer token to avoid mismatches.
- The image field starts with `liferay/dxp:7.4.13.nightly`, offers recent `liferay/dxp` tags from Docker Hub, and links to the Liferay Docker tag catalog.
- Environment variables JSON is collapsed by default, and external DB variables are shown only when `External DB` is selected.
- `portal-ext.properties` is available in the advanced section and uses normal `.properties` syntax.
- Summary cards show available RAM and environments accessible to the current token.
- Machine capacity is shown visually as used/total units, active environment count, and profile costs.
- `Show history` lives in the `Visible Environments` card and controls whether deleted, failed, stopped, and expired records are shown.
- Newer environments are shown first.
- On small screens, the environment table has its own horizontal scroll area.

Environment actions:

- `Touch` updates `last_access_at` manually. Use it to keep a default ephemeral environment from being stopped by the inactivity timeout when it is still needed.
- `Delete` stops and removes the Docker container, shuts down its proxy, and removes the environment from the visible registry list.
- Action tooltips appear after a short hover or keyboard focus delay.

Suggested non-technical flow:

1. Open `http://BROKER_HOST:8899/ui`.
2. Enter the Bearer token once.
3. Choose an image and profile.
4. Click `Create`.
5. Open the returned environment URL from the table.

## Use From A Developer Machine

The CLI is intended for developers and automation. Product/design users should generally use the web UI instead.

Install the Python client dependency:

```bash
python3 -m pip install requests
```

Configure the CLI:

```bash
export LIFERAY_BROKER_URL="http://BROKER_HOST:8899"
export LIFERAY_BROKER_TOKEN="YOUR_TOKEN"
export LIFERAY_BROKER_USER="developer"
```

Create an environment:

```bash
python3 client.py create \
  --image liferay/dxp:7.4.13.nightly-slim-d10.0.29-20260421050955 \
  --profile standard
```

Example response:

```json
{
  "id": "abc123def456",
  "status": "starting",
  "url": "http://BROKER_HOST:18087"
}
```

The environment moves to `ready` once Liferay answers the readiness check.

Other CLI commands:

```bash
python3 client.py list
python3 client.py status ENVIRONMENT_ID
python3 client.py touch ENVIRONMENT_ID
python3 client.py delete ENVIRONMENT_ID
```

Create with a specific host port:

```bash
python3 client.py create \
  --image liferay/portal:7.4.3.132-ga132 \
  --profile small \
  --port 18090
```

Create with `portal-ext.properties`:

```bash
python3 client.py create \
  --image liferay/dxp:2026.q2.1 \
  --profile large \
  --properties-file ./portal-ext.properties
```

Create with an external database:

```bash
python3 client.py create \
  --image liferay/dxp:2026.q2.1 \
  --profile standard \
  --db-mode external \
  --db-env LIFERAY_JDBC_PERIOD_DEFAULT_PERIOD_DRIVERCLASSNAME=org.postgresql.Driver \
  --db-env LIFERAY_JDBC_PERIOD_DEFAULT_PERIOD_URL=jdbc:postgresql://DB_HOST:5432/lportal \
  --db-env LIFERAY_JDBC_PERIOD_DEFAULT_PERIOD_USERNAME=DB_USER \
  --db-env LIFERAY_JDBC_PERIOD_DEFAULT_PERIOD_PASSWORD=DB_PASSWORD
```

## API

All `/v1` endpoints require:

```http
Authorization: Bearer TOKEN
```

Endpoints:

- `GET /health`: public healthcheck.
- `GET /v1/profiles`: available resource profiles.
- `GET /v1/environments`: environments visible to the authenticated user.
- `GET /v1/environments/{environment_id}`: one environment.
- `POST /v1/environments`: create an environment.
- `POST /v1/environments/{environment_id}/touch`: manually mark usage.
- `DELETE /v1/environments/{environment_id}`: delete an environment.
- `GET /v1/dashboard`: UI summary.
- `GET /ui`: web dashboard.

Minimal creation payload:

```json
{
  "image": "liferay/dxp:2026.q2.1",
  "profile": "standard",
  "user": "developer"
}
```

## Local Runtime Files

The broker writes runtime state locally:

- `registry.json`: environment inventory and metadata.
- `portal_properties/`: generated `portal-ext.properties` files when provided.

These paths are ignored by Git because they can contain user names, internal URLs, environment variables, database settings, or other deployment-specific data.

## Repository Hygiene

The repository should only contain placeholders:

- No real tokens.
- No real broker URL.
- No generated `registry.json`.
- No generated `portal_properties/`.
- No real database credentials in examples.

## Current Limitations

- The proxy is intentionally simple and does not fully support WebSocket traffic.
- Readiness is checked through `/c/portal/login`.
- TLS, DNS, and advanced reverse proxying are out of scope.
- CPU availability is not calculated; Docker CPU limits are applied at runtime.
- External databases are passed through environment variables but not provisioned.
- `registry.json` is suitable for a small single-host MVP, not for multi-process or multi-node scheduling.
