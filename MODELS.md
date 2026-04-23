# Broker Models

This document describes the current domain models and contracts implemented by the repository.

## Environment

Represents one ephemeral Liferay environment managed by the broker. It is created from `CreateEnvironmentRequest`, persisted in `registry.json`, and returned by the API.

Main persisted fields:

| Field | Type | Description |
| --- | --- | --- |
| `id` | string | Short UUID-based environment identifier. |
| `user` | string | Environment owner. Must match the authenticated token user on creation. |
| `image` | string | Requested Liferay Docker image. |
| `profile` | string | Resource profile used for memory, CPU, and extra Docker args. |
| `container_name` | string | Sanitized Docker container name. |
| `container_id` | string | ID returned by `docker run` when startup succeeds. |
| `host_port` | integer | Host port where the environment proxy listens. |
| `url` | string | URL built from `base_url_template`. |
| `status` | string | Lifecycle status. |
| `created_at` | datetime ISO | Creation timestamp in UTC. |
| `updated_at` | datetime ISO | Last registry update timestamp. |
| `ready_at` | datetime ISO | Timestamp when readiness passed, when applicable. |
| `expires_at` | datetime ISO | Max TTL expiration timestamp. |
| `deleted_at` | datetime ISO | Destruction timestamp, when applicable. |
| `last_access_at` | datetime ISO/null | Last request seen by the proxy or manual touch endpoint. |
| `last_access_reason` | string | Access update reason, such as `http_request` or `manual_touch`. |
| `idle_timeout_minutes` | integer | Inactivity timeout applied to the environment. |
| `target_ip` | string | Docker-internal container IP used by the proxy. |
| `properties_file` | string/null | Generated `portal-ext.properties` path. |
| `env` | object | Extra environment variables passed to the container. |
| `db_mode` | string | `none` or `external`. |
| `db_env` | object | External database variables when `db_mode=external`. |
| `error` | string/null | Last startup, readiness, or proxy error. |

Valid statuses:

- `starting`
- `ready`
- `failed`
- `stopped`
- `expired`
- `deleted`

Expected transitions:

```text
create -> starting
starting -> ready
starting -> failed
ready -> stopped
ready -> expired
ready -> deleted
starting -> deleted
```

Notes:

- `stopped`, `expired`, and `deleted` destroy the container with `docker rm -f`.
- Cleanup marks an active environment as `failed` if its container is no longer running.
- On restart, the broker tries to restore proxies for `starting` or `ready` records whose containers are still running.

## CreateEnvironmentRequest

Pydantic model accepted by `POST /v1/environments`.

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `image` | string | yes | - | Docker image to run. |
| `profile` | string | no | `standard` | Resource profile from config. |
| `name` | string/null | no | `null` | Optional logical container name. |
| `user` | string | yes | - | Requesting user. |
| `portal_properties` | string/null | no | `null` | Complete `portal-ext.properties` content. |
| `host_port` | integer/null | no | `null` | Requested host port. If omitted, the broker assigns a free one. |
| `env` | object | no | `{}` | Additional environment variables. |
| `ttl_hours` | integer/null | no | `null` | Requested TTL, capped by `max_ttl_hours`. |
| `db_mode` | string | no | `none` | `none` or `external`. |
| `db_env` | object | no | `{}` | External DB variables. Required when `db_mode=external`. |

Current validations:

- `user` must match the Bearer token user.
- `image` must match one pattern from `allowed_image_patterns`.
- `profile` must exist in `profiles`.
- `db_mode` only accepts `none` or `external`.
- `db_mode=external` requires `db_env`.
- The user cannot exceed the active environment count quota for active `starting` or `ready` environments.
- The request cannot exceed global or per-user capacity units.
- Available RAM must remain above `ram_buffer_mb` after reserving `profile.memory_mb`.
- A requested port must be inside `port_range`, unassigned, and bindable on `proxy_bind_host`.

## Profile

Defines resources and Docker options for one environment class.

```yaml
profiles:
  standard:
    memory_mb: 6144
    cpus: 2
    capacity_units: 3
    docker_run_args: []
```

| Field | Type | Description |
| --- | --- | --- |
| `memory_mb` | integer | Memory limit passed to Docker as `--memory`. |
| `cpus` | number | CPU limit passed to Docker as `--cpus`. |
| `capacity_units` | integer | Scheduling weight used by broker capacity checks. |
| `docker_run_args` | array | Extra args inserted before the image name. |

Capacity units are intentionally coarser than RAM. They model the combined cost of memory and CPU so several small environments do not consume all processor headroom just because they fit in RAM.

## BrokerConfig

Loaded from `BROKER_CONFIG` or from `config.yaml` next to `broker.py`.

Required keys:

| Key | Description |
| --- | --- |
| `listen_host` | API listen host expected by config. The systemd unit also passes host to `uvicorn`. |
| `listen_port` | API port. |
| `proxy_bind_host` | Interface used by per-environment proxies. |
| `admin_user` | User allowed to view and delete all environments. |
| `api_tokens` | `user: token` map. |
| `allowed_image_patterns` | Allowed Docker image regexes. |
| `profiles` | Resource profiles. |
| `port_range` | Inclusive host port range for environment proxies. |
| `base_url_template` | Returned URL template. It may use `{port}`, `{container_name}`, and `{env_id}`. |
| `registry_file` | Local JSON registry path. |
| `properties_dir` | Directory for generated `portal-ext.properties` files. |
| `docker_network` | Docker network passed to `docker run --network`. |
| `default_ttl_hours` | Default TTL when the user does not request one. |
| `max_ttl_hours` | Upper bound for requested TTL. |
| `cleanup_interval_seconds` | Cleanup loop interval. |
| `ram_buffer_mb` | Minimum free RAM that must remain after creation. |
| `capacity` | Optional global capacity-unit guardrail configuration. |
| `image_cleanup` | Optional Docker image prune configuration. |
| `max_environments_per_user` | Legacy/default active environment count quota per user. |
| `max_environments_by_user` | Optional legacy user-specific active environment count quota map. |
| `ready_timeout_seconds` | Max time spent waiting for readiness. |
| `ready_check_interval_seconds` | Delay between readiness checks. |
| `idle_timeout_minutes` | Inactivity timeout before stopping the environment. |

Example capacity configuration:

```yaml
capacity:
  total_units: 12
  per_user_units:
    default: 3
    admin: 12
```

Example image cleanup configuration:

```yaml
image_cleanup:
  enabled: true
  interval_hours: 24
  max_unused_age_hours: 168
```

Image cleanup runs `docker image prune -a --force --filter until=<hours>h`. Docker only removes images that are not referenced by containers.

## Registry

`registry.json` is a JSON list of `Environment` objects. It is generated at runtime and ignored by Git.

Simplified example:

```json
[
  {
    "id": "abc123def456",
    "user": "developer",
    "image": "liferay/dxp:2026.q2.1",
    "profile": "standard",
    "container_name": "liferay-developer-a1b2c3",
    "host_port": 18080,
    "status": "ready",
    "url": "http://BROKER_HOST:18080",
    "created_at": "2026-04-21T10:00:00+00:00",
    "expires_at": "2026-04-21T18:00:00+00:00",
    "last_access_at": "2026-04-21T10:20:00+00:00",
    "env": {},
    "db_mode": "none",
    "db_env": {},
    "error": null
  }
]
```

Notes:

- The registry is rewritten as a whole through a temporary file.
- There is no multiprocess lock; the MVP expects one broker process.
- The registry may contain sensitive data if `env` or `db_env` includes credentials.

## EnvironmentProxy

Per-environment HTTP proxy. It listens on `proxy_bind_host:host_port` and forwards to `target_ip:8080`.

Responsibilities:

- Forward `GET`, `POST`, `PUT`, `DELETE`, `PATCH`, `HEAD`, and `OPTIONS`.
- Copy headers except `Host`, `Connection`, and `Content-Length`.
- Update `last_access_at` after successful upstream requests.
- Return `502` when upstream forwarding fails.

Limitations:

- No full WebSocket support.
- Not an advanced reverse proxy.
- No TLS management.

## Web Dashboard

The dashboard is served by `GET /ui` from the broker process. It is a lightweight HTML/CSS/JavaScript page embedded in `broker.py`; there is no separate frontend build step.

Primary users:

- Developers who prefer a browser workflow.
- Product, design, QA, and demo users who should not need Python or CLI commands.

Main UI areas:

| Area | Purpose |
| --- | --- |
| Header | Shows `Liferay Environment Broker` and the dark/light theme toggle. |
| Login/session card | Accepts a `Bearer token`, then collapses into a compact connected-session summary. |
| Metric cards | Show capacity usage, available RAM, and total visible environments. |
| Total card | Also contains the `Show history` toggle. |
| Create Environment card | Lets users create environments with image, Docker Hub tag suggestions, profile, DB mode, optional port, TTL, env vars, DB env vars, and portal properties. |
| Environment table | Lists environments newest-first with status, user, image, port, URL, access timestamps, created timestamp, and actions. |

Dashboard persistence:

- `liferayBroker.token`
- `liferayBroker.user`
- `liferayBroker.showHistory`
- `liferayBroker.theme`

These values are stored in browser `localStorage`. They are intentionally client-local and must not be committed to the repository.

Dashboard behavior:

- Dark mode is the default theme.
- The theme toggle is an icon button in the top-right corner.
- The broker URL is derived from the current `/ui` page with `window.location.origin`.
- The login controls hide after a successful connection.
- After `Connect`, the `User` field is filled from the Bearer token through `GET /v1/me`.
- Image suggestions are fetched from `GET /v1/images/liferay-dxp-tags`, which caches recent Docker Hub `liferay/dxp` tags briefly.
- Capacity is shown as used/total units, active environment count, and profile unit costs.
- `Show history` controls whether terminal records (`deleted`, `failed`, `stopped`, `expired`) are shown.
- The table is horizontally scrollable on small screens.
- The URL column has a minimum width to keep environment links readable.
- Created timestamps are rendered in smaller text.
- Action buttons include delayed hover/focus tooltips.

Dashboard actions:

| Action | Endpoint | Effect |
| --- | --- | --- |
| `Connect` | `GET /v1/me`, `GET /v1/dashboard`, and `GET /v1/environments` | Loads token identity, summary, and environment table with the configured token. |
| `Create` | `POST /v1/environments` | Creates a new environment using the form payload. |
| `Touch` | `POST /v1/environments/{environment_id}/touch` | Updates `last_access_at` with reason `manual_touch`, preventing idle cleanup while still within TTL. |
| `Delete` | `DELETE /v1/environments/{environment_id}` | Runs `docker rm -f`, stops the proxy, removes generated properties, and removes the registry record. |

## Auth Model

The MVP uses static Bearer tokens:

```yaml
api_tokens:
  admin: CHANGE_ME_ADMIN_TOKEN
  developer: CHANGE_ME_DEVELOPER_TOKEN
```

Rules:

- A token identifies one user.
- `CreateEnvironmentRequest.user` must match that user.
- `admin_user` can list, view, and delete other users' environments.
- There are no extra roles, token expiry, or OIDC integration in the MVP.

## Sensitive Runtime Data

The following values can become sensitive in real deployments:

- `BrokerConfig.api_tokens`
- `BrokerConfig.base_url_template`, if it contains a real internal host
- `CreateEnvironmentRequest.portal_properties`
- `Environment.properties_file`
- `Environment.env`
- `Environment.db_env`
- generated `registry.json`
- generated files under `portal_properties/`

Keep real deployment values out of commits. Use placeholders in the repository and configure the installed host copy.
