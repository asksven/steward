# steward

A lightweight, per-node GitOps reconciler for Docker Compose stacks, inspired by ArgoCD's reconciliation model.

## Concept

One container runs on each node. It watches a central **control repo** for app manifests, and per-app **stack repos** for compose file changes. When drift is detected it runs `docker compose up -d --remove-orphans`. The agent also checks for updates to its own image and restarts itself when a new version is pushed.

```
GitHub: homelab-gitops/          ← app manifests (what runs where)
GitHub: arr-stack/               ← docker compose files for arr
GitHub: prometheus-stack/        ← docker compose files for prometheus

On each node:
  steward (container)
    crond
      every minute  → reconcile stacks
      every hour    → self-update agent image
```

---

## Repository structure

```
steward/
  steward.py          # Main steward logic
  Dockerfile             # Agent container image
  docker-compose.yml     # Deploy the agent on a node
  crontab                # Cron schedule (reconcile + self-update)
  entrypoint.sh          # Container entrypoint
  examples.yml           # Example app manifests
  .github/
    workflows/
      build.yml          # Build and push image to GHCR on push to main
```

---

## Prerequisites

- Docker with the `docker compose` plugin (v2) on each node
- A GitHub or GitLab account for your repos
- A container registry (GHCR recommended, free with GitHub)

---

## Quick start

### 1. Build and push the agent image

Update `.github/workflows/build.yml` with your registry path, then push to `main`. The workflow builds and pushes to `ghcr.io/<you>/steward:latest`.

Or build manually:

```bash
docker build -t ghcr.io/<you>/steward:latest .
docker push ghcr.io/<you>/steward:latest
```

### 2. Set up your control repo

See the companion **homelab-gitops** README for the control repo structure. Create a directory for your node under `nodes/` and add app manifests.

### 3. Deploy the agent on a node

Create a `.env` file next to `docker-compose.yml` with at minimum:

```env
CONTROL_REPO_URL=https://oauth2:<token>@github.com/you/homelab-gitops
GITOPS_NODE_NAME=node1.lan
```

`GITOPS_NODE_NAME` must match the directory name under `nodes/` in the control repo. Then:

```bash
docker compose up -d
```

The agent will immediately reconcile on startup, then run on the cron schedule.

---

## Configuration

All configuration is via environment variables.

| Variable | Required | Default | Description |
|---|---|---|---|
| `CONTROL_REPO_URL` | yes | — | URL of the control repo (embed token for private repos) |
| `CONTROL_REPO_BRANCH` | no | `main` | Branch to track on the control repo |
| `STEWARD_DATA_DIR` | no | `./steward-data` | Host path where git repos are cloned (outside the container) |
| `STEWARD_UID` | no | `0` (root) | UID to run steward as — set to your host user's UID so files in `STEWARD_DATA_DIR` are not root-owned |
| `STEWARD_GID` | no | `STEWARD_UID` | GID to run steward as — defaults to `STEWARD_UID` if not set |
| `GITOPS_ROOT` | no | `/git` | Repo root inside the container — change only if you remap the volume |
| `GITOPS_NODE_NAME` | no | `hostname` | Node name, must match `nodes/<name>` in control repo |
| `AGENT_CONTAINER_NAME` | no | — | Container name of the agent itself, required for self-update |
| `AGENT_IMAGE` | yes | — | Full image name including tag, used for deployment and self-update |
| `LOGLEVEL` | no | `INFO` | Log level (`DEBUG` enables per-path inside/outside diagnostics) |
| `METRICS_PORT` | no | — (disabled) | Port for the Prometheus `/metrics` scrape endpoint; unset to disable |

---

## App manifest schema

Each `.yml` file in `nodes/<hostname>/` in the control repo describes one application.

```yaml
version: 1                              # required, must be 1
name: arr                               # required, used as local repo directory name
repo: https://github.com/you/arr-stack  # required, HTTPS or SSH URL
ref:
  branch: main                          # mutually exclusive with tag
  # tag: v1.2.3                         # pin to a specific release
path: .                                 # path within repo to compose file, default: .
compose_file: docker-compose.yml        # compose filename, default: docker-compose.yml
env_file: /git/envs/arr.env             # path inside the container; /git maps to STEWARD_DATA_DIR on the host
enabled: true                           # required, explicit — use false to disable without deleting
```

### Validation rules

- `version`, `name`, `repo`, `ref`, and `enabled` are all required — missing any is a hard error
- `ref.branch` and `ref.tag` are mutually exclusive — specifying both is a hard error
- `enabled` has no default — must be explicitly set

---

## Private repos

### HTTPS with token (simplest)

Embed the token in the URL:

```yaml
# GitHub
CONTROL_REPO_URL: https://oauth2:<token>@github.com/you/homelab-gitops

# GitLab
CONTROL_REPO_URL: https://oauth2:<token>@gitlab.com/you/homelab-gitops
```

Store the token in a `.env` file next to `docker-compose.yml` and reference it:

```yaml
environment:
  CONTROL_REPO_URL: https://oauth2:${GITHUB_TOKEN}@github.com/you/homelab-gitops
```

### SSH key

Create an SSH config file on the host that maps each Git host to its key:

```
# ~/.ssh/config
Host gitlab.com
  IdentityFile ~/.ssh/id_rsa-gitlab
  StrictHostKeyChecking yes

Host github.com
  IdentityFile ~/.ssh/id_rsa-github
  StrictHostKeyChecking yes
```

Create a container-specific SSH config on the host referencing the container paths (not `~/.ssh/`):

```
# ~/steward-ssh-config
Host gitlab.com
  IdentityFile /root/.ssh/id_rsa-gitlab
  StrictHostKeyChecking yes

Host github.com
  IdentityFile /root/.ssh/id_rsa-github
  StrictHostKeyChecking yes
```

Then create a `docker-compose.override.yml` mounting everything into the staging directory `/root/.ssh-host/`:

```yaml
services:
  steward:
    volumes:
      - ~/.ssh/id_rsa-gitlab:/root/.ssh-host/id_rsa-gitlab:ro
      - ~/.ssh/id_rsa-github:/root/.ssh-host/id_rsa-github:ro
      - ~/steward-ssh-config:/root/.ssh-host/config:ro
      - ~/.ssh/known_hosts:/root/.ssh-host/known_hosts:ro
```

Docker Compose merges this automatically. The entrypoint copies the files to `/root/.ssh/` with correct root ownership and permissions on startup — bind-mounted files retain the host user's UID which SSH rejects. Use SSH URLs in your manifests:

```yaml
repo: git@github.com:you/arr-stack.git
```

---

## Self-update

When `AGENT_CONTAINER_NAME` and `AGENT_IMAGE` are set, the agent checks hourly whether a newer image has been pushed to the registry. If the digest of the pulled image differs from the running container's image, it runs `docker restart <container>`. Since crond is the PID 1 driver, the restart picks up the new image without breaking the schedule.

---

## Logs

```bash
docker logs -f steward
```

All output (cron + steward) is forwarded to the container's stdout via `/proc/1/fd/1`.

Set `LOGLEVEL=DEBUG` in your `.env` to enable verbose path diagnostics. At DEBUG level, steward logs both the container-internal path and the corresponding host path for every file it touches (repos, compose files, env files), plus a full mount map at startup. This is useful for diagnosing `env_file` or repo path mismatches:

```env
LOGLEVEL=DEBUG
```

---

## Metrics

Set `METRICS_PORT` in your `.env` to start a Prometheus scrape endpoint inside the container:

```env
METRICS_PORT=9101
```

Expose the port via `docker-compose.override.yml`:

```yaml
services:
  steward:
    ports:
      - "9101:9101"
```

Then add a scrape config to your Prometheus:

```yaml
scrape_configs:
  - job_name: steward
    static_configs:
      - targets:
          - media-1:9101   # one entry per node
          - media-2:9101
```

### Exposed metrics

| Metric | Type | Description |
|---|---|---|
| `steward_reconcile_last_timestamp_seconds` | gauge | Unix timestamp of last completed reconciliation run |
| `steward_reconcile_duration_seconds` | gauge | Duration of last reconciliation run |
| `steward_reconcile_total{result}` | counter | Reconciliation runs by result (`success`, `partial_failure`, `fatal`) |
| `steward_self_update_total{result}` | counter | Self-update checks by result (`no_update`, `updated`, `failed`, `skipped`) |
| `steward_app_info{app,repo,ref,ref_type,enabled}` | gauge | Static app information (always 1) |
| `steward_app_last_reconcile_timestamp_seconds{app}` | gauge | Unix timestamp of last reconcile attempt per app |
| `steward_app_last_sync_timestamp_seconds{app}` | gauge | Unix timestamp of last `docker compose up` per app |
| `steward_app_reconcile_total{app,result}` | counter | Reconcile attempts per app by result (`success`, `failed`, `skipped`) |
| `steward_app_sync_total{app,result}` | counter | Compose runs per app by result (`success`, `failed`) |

All metrics carry a `node` label set to `GITOPS_NODE_NAME`.

### Grafana alerts

| Alert | Expression | Severity |
|---|---|---|
| Compose apply failed | `increase(steward_app_sync_total{result="failed"}[5m]) > 0` | critical |
| Repeated reconcile failures | `increase(steward_app_reconcile_total{result="failed"}[15m]) > 2` | warning |
| Node not reporting | `time() - steward_reconcile_last_timestamp_seconds > 300` | critical |
| App not reconciled | `time() - steward_app_last_reconcile_timestamp_seconds > 300` | warning |
| Self-update failed | `increase(steward_self_update_total{result="failed"}[1h]) > 0` | warning |

---

## Reconciler flow (per cron run)

```
1. git fetch control repo
   └── if changed: pull, re-read node manifests

2. for each app manifest:
   ├── enabled: false → skip
   ├── stack repo not cloned → clone
   ├── git fetch stack repo
   │   ├── up to date → skip
   │   └── changed → git pull/checkout → docker compose up -d --remove-orphans --pull always
   └── log result

3. (hourly) self-update:
   ├── docker pull <agent image>
   ├── compare digest of running container vs pulled image
   └── if different → docker restart <container name>
```

---

## Extending

Some natural next steps not yet implemented:

- **Failure notifications** - POST to a webhook (Gotify, ntfy, Slack) on reconciliation failure
- **Metrics** - Expose (push) prometheus metrics to a push gateway for dashboards/alerts
- **Dry-run mode** — `python3 steward.py --dry-run` to show what would change without applying
- **Status command** — print current deployed SHA vs remote SHA for each app
- **Schema version 2** — reserved for future manifest extensions
- **Multiple env files** — if ever needed, extend the `env_file` field to a list
