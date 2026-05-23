# steward

A lightweight, per-node GitOps reconciler for Docker Compose stacks, inspired by ArgoCD's reconciliation model.

## Concept

One container runs on each node. It watches a central **control repo** for app manifests, and per-app **stack repos** for compose file changes. When drift is detected it runs `docker compose up -d --remove-orphans`. Steward can reconcile its own stack as part of normal GitOps flow.

```
GitHub: homelab-gitops/          ← app manifests (what runs where)
GitHub: arr-stack/               ← docker compose files for arr
GitHub: prometheus-stack/        ← docker compose files for prometheus

On each node:
  steward (container)
    crond
      every minute  → reconcile stacks
```

---

## Repository structure

```
steward/
  steward.py          # Main steward logic
  Dockerfile             # Agent container image
  docker-compose.yml     # Deploy the agent on a node
  crontab                # Cron schedule (reconcile)
  entrypoint.sh          # Container entrypoint
  examples.yml           # Example app manifests
  .github/
    workflows/
      build.yml          # Lint + test + build and push image to GHCR
```

---

## Prerequisites

- Docker with the `docker compose` plugin (v2) on each node
- A GitHub or GitLab account for your repos
- A container registry (GHCR recommended, free with GitHub)

---

## Quick start

### 1. Build and push the agent image

Update `.github/workflows/build.yml` with your registry path (the `IMAGE_NAME` env var), then push to `main`. The workflow runs lint and tests first, then builds and pushes to GHCR only if both pass.

See the [Releasing](#releasing) section for what image tags are produced and which one to use in your `.env`.

### 2. Set up your control repo

See the companion **homelab-gitops** README for the control repo structure. Create a directory for your node under `nodes/` and add app manifests.

### 3. Create the .env file

Create a `.env` file next to `docker-compose.yml`:

```env
CONTROL_REPO_URL=https://oauth2:<token>@github.com/you/homelab-gitops
GITOPS_NODE_NAME=node1.lan
AGENT_IMAGE=ghcr.io/<you>/steward:latest
STEWARD_DATA_DIR=/opt/steward-data
```

`GITOPS_NODE_NAME` must match the directory name under `nodes/` in the control repo.

### 4. Run as a non-root user (recommended)

By default steward runs as root, which means files written to `STEWARD_DATA_DIR` are owned by root. Add your host UID and GID so the files are owned by your normal user:

```bash
echo "STEWARD_UID=$(id -u)" >> .env
echo "STEWARD_GID=$(id -g)" >> .env
```

The entrypoint creates `/home/steward` inside the container owned by that UID. SSH key files are placed there automatically (see step 5).

### 5. Set up SSH authentication (if using SSH repo URLs)

Skip this step if all your repos use HTTPS URLs. If any repo uses an SSH URL (`git@github.com:...`), follow the [SSH key](#ssh-key) setup below before starting the container.

### 6. Start the agent

```bash
docker compose up -d
```

The agent reconciles immediately on startup, then runs on the cron schedule.

---

## Development and quality checks

Steward uses **uv** for local dependency management and developer commands.

```bash
uv venv
uv pip install -r requirements.txt pytest ruff
uv run pytest -v --tb=short
uv run ruff check steward.py metrics_server.py tests
```

Runtime dependencies are managed with:

- `requirements.in` for direct dependencies
- `requirements.txt` for pinned, reproducible installs

To refresh pinned versions:

```bash
uv pip compile requirements.in -o requirements.txt
```

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
| `AGENT_CONTAINER_NAME` | no | `steward` | Container name — used by `docker inspect` for debug path logging and to detect when reconciling steward's own stack (self-update via helper container) |
| `AGENT_IMAGE` | no | — | Overrides the image tag in `docker-compose.yml`; useful for testing a local build |
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

The container home directory for SSH keys depends on `STEWARD_UID`:

| `STEWARD_UID` | SSH home inside container |
|---|---|
| `0` (root, default) | `/root/.ssh/` |
| any other value | `/home/steward/.ssh/` |

**Step 1** — create a container-specific SSH config on the host. The `IdentityFile` paths must point to the container-internal SSH home, not `~/.ssh/`. Using the recommended non-root setup:

```
# ~/steward-ssh-config
Host gitlab.com
  IdentityFile /home/steward/.ssh/id_rsa-gitlab
  StrictHostKeyChecking yes

Host github.com
  IdentityFile /home/steward/.ssh/id_rsa-github
  StrictHostKeyChecking yes
```

If you are running as root (`STEWARD_UID=0`), use `/root/.ssh/` instead.

**Step 2** — create a `docker-compose.override.yml` that mounts your key files and the config into the staging directory `/root/.ssh-host/`:

```yaml
services:
  steward:
    volumes:
      - ~/.ssh/id_rsa-gitlab:/root/.ssh-host/id_rsa-gitlab:ro
      - ~/.ssh/id_rsa-github:/root/.ssh-host/id_rsa-github:ro
      - ~/steward-ssh-config:/root/.ssh-host/config:ro
      - ~/.ssh/known_hosts:/root/.ssh-host/known_hosts:ro
```

Docker Compose merges this file automatically. On startup the entrypoint copies everything from `/root/.ssh-host/` into the container user's SSH home (`/home/steward/.ssh/` or `/root/.ssh/`) with correct ownership and permissions — bind-mounted files retain the host user's UID, which SSH rejects if it does not match the running process.

**Step 3** — use SSH URLs in your manifests:

```yaml
repo: git@github.com:you/arr-stack.git
```

---

## Self-update

Steward updates itself the same way it updates any other app: through git. The image version is pinned directly in `docker-compose.yml` and **Dependabot** opens a PR whenever a new release is published to GHCR. Merging the PR causes steward to detect a change in its own stack repo and trigger an update.

### Why a helper container?

Running `docker compose up -d` from inside a container replaces that container — Docker sends SIGTERM to the old container before the new one starts, which kills the process that initiated the update. To avoid this, steward detects when it is reconciling its own stack (by comparing `app.name` to `AGENT_CONTAINER_NAME`) and spawns a short-lived **helper container** instead:

```
docker run --rm -d \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v <STEWARD_DATA_DIR>:<STEWARD_DATA_DIR> \
  -e HOME=/tmp \
  <current-steward-image> \
  sh -c "sleep 5 && docker compose --project-name <app.name> -f <compose-file> up -d --remove-orphans --pull always"
```

The helper is a peer container, independent of steward's process. When Docker Compose stops steward, the helper is unaffected and creates the new container cleanly. The 5-second delay lets the old container exit fully before `compose up` runs.

If host paths cannot be resolved (e.g. `AGENT_CONTAINER_NAME` is wrong or docker inspect fails), steward falls back to calling `docker compose --project-name <app.name> up -d` directly — which will kill itself, but `restart: unless-stopped` ensures the container comes back up on the next Docker restart cycle.

### Bootstrap

The first time you deploy steward on a node, clone the steward repo into `STEWARD_DATA_DIR/stacks/steward` manually, create a `.env` and `docker-compose.override.yml` there for node-specific settings (SSH mounts, ports, etc.), and start the container. Once steward is running and its own stack repo is in `STEWARD_DATA_DIR/stacks/steward`, it will manage its own updates from that point on.

Add a manifest for steward itself in the control repo under `nodes/<hostname>/`:

```yaml
version: 1
name: steward
repo: https://github.com/<you>/steward
ref:
  branch: main
path: .
compose_file: docker-compose.yml
enabled: true
```

### Testing a local build

To run a locally built image without changing `docker-compose.yml`, set `AGENT_IMAGE` in the `.env` file next to your deployment:

```env
AGENT_IMAGE=ghcr.io/<you>/steward:dev
```

This overrides the pinned tag for that node only and is not tracked by Dependabot.

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
| `steward_control_repo_sync_total{result}` | counter | Control repo sync attempts by result (`up_to_date`, `updated`, `failed`) |
| `steward_manifest_parse_errors_total` | counter | Total manifest parse errors encountered |
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

---

## Reconciler flow (per cron run)

```
1. git fetch control repo
   └── if changed: pull, re-read node manifests

2. for each app manifest (including steward itself, if present):
   ├── enabled: false → skip
   ├── stack repo not cloned → clone
   ├── git fetch stack repo
  │   ├── up to date → skip
  │   └── changed → git pull/checkout → docker compose --project-name <app.name> up -d --remove-orphans --pull always
   └── log result
```

---

## Migration (pre-2.5 to 2.5+)

Item 2.5 makes steward pass an explicit compose project name on all compose runs:

```bash
docker compose --project-name <app.name> ...
```

### Do you need migration steps?

In most deployments: no.

- steward has always cloned stacks to `STEWARD_DATA_DIR/stacks/<app.name>`
- older compose runs used the working directory name as project name
- that directory name is usually the same as `<app.name>`

So for the common case, project name stays unchanged and rollout is seamless.

### When manual cleanup may be needed

Cleanup is only needed if your old effective compose project name differs from `app.name`
(for example older manual compose runs from another directory).

Symptoms:

- `docker compose ls` shows both an old project and the new `app.name` project
- duplicate containers/networks/volumes for one app

### Safe cleanup procedure

1. Let steward run one reconcile cycle after upgrade.
2. Confirm the new project is healthy:

```bash
docker compose --project-name <app.name> -f <compose-file> ps
```

3. Stop and remove the old project once the new one is confirmed:

```bash
docker compose --project-name <old-project-name> -f <compose-file> down --remove-orphans
```

4. Verify only the expected project remains:

```bash
docker compose ls
```

Notes:

- no manifest schema changes are required for 2.5
- no env var changes are required for 2.5
- steward's own stack uses the same explicit project-name logic in helper mode

---

## Releasing

The GitHub Actions workflow (`build.yml`) triggers on two events:

| Trigger | Condition |
|---|---|
| Push to `main` | Only when files under `steward/` change |
| Push of a `v*` tag | Always |

### Image tags produced

| Event | Tags produced |
|---|---|
| Push to `main` | `main`, `sha-<sha>`, `latest` |
| Push of `v0.1.0` | `v0.1.0`, `0.1.0`, `0.1`, `0`, `sha-<sha>` |

`latest` is only added on `main` pushes, not on tag pushes.

### How to cut a release

```bash
# Ensure main is up to date and all changes are committed
git push origin main          # triggers build → updates :latest, :main

# Tag the release
git tag v0.1.0
git push origin v0.1.0        # triggers build → creates :v0.1.0, :0.1.0, :0.1, :0
```

Push both in the same command if you prefer:

```bash
git tag v0.1.0
git push origin main v0.1.0
```

### Which tag to use in .env

| Use case | `AGENT_IMAGE` tag |
|---|---|
| Auto-update on every `0.x.y` release (recommended) | `:0` |
| Auto-update only on `0.1.x` patch releases | `:0.1` |
| Pinned to a specific release, no auto-update | `:v0.1.0` |

The rolling major tag (`:0`) is the recommended choice. It moves with every release in the `0.x` series and stops moving when a breaking `v1.0.0` is cut — giving nodes an explicit opt-in moment for major version upgrades.

---

## Extending

Some natural next steps not yet implemented:

- **Failure notifications** — POST to a webhook (Gotify, ntfy, Slack) on reconciliation failure
- **Dry-run mode** — `python3 steward.py --dry-run` to show what would change without applying
- **Status command** — print current deployed SHA vs remote SHA for each app
- **Schema version 2** — reserved for future manifest extensions
- **Multiple env files** — if ever needed, extend the `env_file` field to a list
