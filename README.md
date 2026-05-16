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
| `GITOPS_ROOT` | no | `~/git` | Root directory for local repo clones |
| `GITOPS_NODE_NAME` | no | `hostname` | Node name, must match `nodes/<name>` in control repo |
| `AGENT_CONTAINER_NAME` | no | — | Container name of the agent itself, required for self-update |
| `AGENT_IMAGE` | no | — | Full image name including tag, required for self-update |

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
env_file: /opt/gitops/arr.env           # absolute path on node, ~ for none
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
